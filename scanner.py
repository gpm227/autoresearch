"""
scanner.py — Kalshi market scanner.

Polls Kalshi for all open markets, classifies them by category, pulls
orderbooks for weather markets, computes MM-likelihood metrics, and logs
every weather observation to market_observations.

Downstream filters (filter_neglected) pick the subset of markets that look
stale / inattentive — those are the ones worth hunting for edge on.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

from infra import KalshiClient
from execution import _get_db, DB_PATH
from weather_adapter_noaa import WeatherAdapterNOAA
from weather_adapter_ensemble import EnsembleAdapter
from weather_contracts import parse_weather_contract
from weather_model import WeatherModel, ProbabilityEstimate
from signals import generate_signal, TradeSignal
from config import (
    TARGET_STATION, TRADE_WINDOW_START_HOUR, TRADE_WINDOW_END_HOUR,
    MAX_TOTAL_OPEN_RISK_PCT, CITY_TIMEZONE,
)

log = logging.getLogger("scanner")


# ─── Orderbook parsing (new Kalshi dollar format) ─────────────────────────────

def _parse_orderbook(resp: dict) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """
    Parse a Kalshi /markets/{ticker}/orderbook response and return
    (yes_bids, yes_asks) as lists of (price_cents, size_contracts) tuples.

    Handles the current `orderbook_fp` format with stringified dollar prices:
        {"orderbook_fp": {
            "yes_dollars": [["0.0100", "2301.00"], ...],   # orders to BUY yes
            "no_dollars":  [["0.9600", "44.00"], ...]      # orders to BUY no
        }}

    no_dollars → yes_asks via yes_ask_price_cents = 100 - no_bid_price_cents
    (buying NO at 96¢ is equivalent to selling YES at 4¢, i.e. a yes ask at 4¢).

    Also handles the legacy `orderbook.yes / .no` integer-cent format as a
    fallback so tests / demo env keep working.
    """
    # New dollar format
    fp = resp.get("orderbook_fp")
    if isinstance(fp, dict):
        yes_dollars = fp.get("yes_dollars") or []
        no_dollars = fp.get("no_dollars") or []

        yes_bids_raw: list[tuple[int, int]] = []
        for entry in yes_dollars:
            try:
                price_cents = int(round(float(entry[0]) * 100))
                size = int(float(entry[1]))
                yes_bids_raw.append((price_cents, size))
            except (ValueError, IndexError, TypeError):
                continue

        no_bids_raw: list[tuple[int, int]] = []
        for entry in no_dollars:
            try:
                price_cents = int(round(float(entry[0]) * 100))
                size = int(float(entry[1]))
                no_bids_raw.append((price_cents, size))
            except (ValueError, IndexError, TypeError):
                continue

        yes_asks_raw = [(100 - p, s) for p, s in no_bids_raw]
        yes_bids = sorted(yes_bids_raw, key=lambda x: -x[0])
        yes_asks = sorted(yes_asks_raw, key=lambda x: x[0])
        return yes_bids, yes_asks

    # Legacy integer-cent format
    ob = resp.get("orderbook", resp)
    if isinstance(ob, dict):
        try:
            yes_bids_raw = [(int(e[0]), int(e[1])) for e in (ob.get("yes") or [])]
            no_bids_raw = [(int(e[0]), int(e[1])) for e in (ob.get("no") or [])]
            yes_asks_raw = [(100 - p, s) for p, s in no_bids_raw]
            yes_bids = sorted(yes_bids_raw, key=lambda x: -x[0])
            yes_asks = sorted(yes_asks_raw, key=lambda x: x[0])
            return yes_bids, yes_asks
        except (ValueError, IndexError, TypeError):
            pass

    return [], []


# ─── Classification ───────────────────────────────────────────────────────────

# Kalshi /series exposes a "category" field. Treat these as weather markets.
_WEATHER_CATEGORY = "climate and weather"


def fetch_weather_series(client: KalshiClient) -> set[str]:
    """
    Fetch Kalshi's series catalog and return the set of series tickers
    whose category is "Climate and Weather".

    This is the authoritative weather classifier — Kalshi tags each series
    directly, which is much more robust than substring-matching ticker names
    (weather tickers include KXHIGHLAX, KXLOWTSEA, KXMAXTEMP100, KXTEMPNYCH,
    none of which share a common prefix).
    """
    try:
        resp = client._request("GET", "/series?limit=1000")
    except Exception as e:
        log.warning("Failed to fetch /series: %s", e)
        return set()

    series_list = resp.get("series", [])
    weather = {
        s.get("ticker", "")
        for s in series_list
        if (s.get("category") or "").strip().lower() == _WEATHER_CATEGORY
        and s.get("ticker")
    }
    log.info("Loaded %d weather series from /series catalog", len(weather))
    return weather


def classify_market(ticker: str, title: str, weather_series: set[str] | None = None) -> str:
    """
    Classify a Kalshi market into a category.

    Preferred path: look up the market's series in a pre-fetched weather
    whitelist (from /series?category=Climate and Weather).

    Fallback: ticker/title substring match for weather and crypto.
    """
    t_upper = (ticker or "").upper()
    title_lower = (title or "").lower()

    # Weather: whitelist match on the series portion of the ticker
    if weather_series:
        series = t_upper.split("-", 1)[0]
        if series in weather_series:
            return "weather"

    # Fallback heuristics (keeps the demo env working + catches any series
    # we miss from the catalog)
    if "TEMP" in t_upper or "HIGHTEMP" in t_upper or "temperature" in title_lower:
        return "weather"
    if "BTC" in t_upper or "ETH" in t_upper or "SOL" in t_upper:
        return "crypto"
    return "other"


# ─── MM-likelihood score ──────────────────────────────────────────────────────

_SPREAD_WEIGHT = 0.6
_DEPTH_WEIGHT = 0.4
_SPREAD_TIGHT_CENTS = 2   # <= 2c spread → fully tight (score 1.0)
_SPREAD_WIDE_CENTS = 20   # >= 20c spread → fully wide (score 0.0)
_DEPTH_SATURATION = 200   # contracts at which depth component saturates at 1.0


def _normalize_spread(spread_cents: int) -> float:
    """Tighter spread → higher score. Linear between 2c and 20c."""
    if spread_cents <= _SPREAD_TIGHT_CENTS:
        return 1.0
    if spread_cents >= _SPREAD_WIDE_CENTS:
        return 0.0
    return 1.0 - (spread_cents - _SPREAD_TIGHT_CENTS) / (_SPREAD_WIDE_CENTS - _SPREAD_TIGHT_CENTS)


def _normalize_depth(total_depth_contracts: int) -> float:
    """Deeper book → higher score. Saturates at 200 contracts."""
    return min(total_depth_contracts / _DEPTH_SATURATION, 1.0)


def compute_mm_score(spread_cents: int, bid_depth: int, ask_depth: int) -> float:
    """
    Weighted combination of spread-tightness and book-depth.

    Returns a float in [0, 1] where higher = more likely professionally MM'd.
    """
    spread_component = _normalize_spread(spread_cents)
    depth_component = _normalize_depth(bid_depth + ask_depth)
    return _SPREAD_WEIGHT * spread_component + _DEPTH_WEIGHT * depth_component


# ─── Main scan ────────────────────────────────────────────────────────────────

def _fetch_all_open_markets(client: KalshiClient) -> list[dict]:
    """
    Paginate through /markets?status=open and return all markets.

    Uses mve_filter=exclude to strip MVE parlay markets (KXMVESPORTSMULTIGAME*,
    KXMVECROSSCATEGORY) which make up ~60% of Kalshi prod markets but are
    noise for arb hunting. limit=1000 minimizes round trips.
    """
    markets: list[dict] = []
    cursor = ""
    page = 0
    while True:
        resp = client.get_markets(
            status="open",
            limit=1000,
            cursor=cursor,
            mve_filter="exclude",
        )
        page_markets = resp.get("markets", [])
        markets.extend(page_markets)
        cursor = resp.get("cursor", "")
        page += 1
        if not cursor:
            break
        # Defensive cap — 200 pages × 1000 = 200k markets, well above prod size
        if page >= 200:
            log.warning("Pagination cap hit at page %d (%d markets so far)", page, len(markets))
            break
    return markets


def _parse_close_ts(close_time_str: str) -> Optional[int]:
    """Kalshi close_time is ISO-8601. Returns unix seconds or None."""
    if not close_time_str:
        return None
    try:
        from datetime import datetime
        # Handle both "2026-04-07T20:00:00Z" and "...+00:00" forms
        s = close_time_str.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def scan_markets(
    client: KalshiClient,
    db_path: Optional[str] = None,
) -> list[dict]:
    """
    Poll Kalshi for all open markets, classify, pull weather orderbooks,
    compute MM scores, log to market_observations.

    Args:
        client:   live KalshiClient
        db_path:  optional override of trades.db path (default: execution.DB_PATH)

    Returns:
        list of weather market dicts, each augmented with:
            category, best_bid, best_ask, spread_cents,
            bid_depth_contracts, ask_depth_contracts, mm_score
    """
    scan_start = time.time()

    # Fetch the weather series whitelist once per scan (one /series call)
    weather_series = fetch_weather_series(client)

    # Fetch all open markets with pagination (MVE parlays excluded)
    try:
        all_markets = _fetch_all_open_markets(client)
    except Exception as e:
        log.error("Failed to fetch markets: %s", e)
        return []

    total_count = len(all_markets)

    # Classify
    weather_markets: list[dict] = []
    category_counts = {"weather": 0, "crypto": 0, "other": 0}
    for m in all_markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "")
        cat = classify_market(ticker, title, weather_series=weather_series)
        category_counts[cat] = category_counts.get(cat, 0) + 1
        if cat == "weather":
            m["category"] = cat
            weather_markets.append(m)

    log.info(
        "Scan: %d total markets | weather=%d crypto=%d other=%d",
        total_count,
        category_counts.get("weather", 0),
        category_counts.get("crypto", 0),
        category_counts.get("other", 0),
    )

    # Pull orderbooks for weather markets, compute metrics, log observations
    db = _get_db() if db_path is None else _get_db_at(db_path)
    now_ts = int(time.time())
    enriched: list[dict] = []
    orderbook_failures = 0

    try:
        for m in weather_markets:
            ticker = m["ticker"]
            try:
                ob_data = client.get_orderbook(ticker)
                yes_bids, yes_asks = _parse_orderbook(ob_data)
            except Exception as e:
                orderbook_failures += 1
                log.debug("Orderbook fetch failed for %s: %s", ticker, e)
                continue

            if not yes_bids or not yes_asks:
                # Empty book — record the observation anyway with nulls
                _insert_observation(
                    db,
                    market_ticker=ticker,
                    timestamp=now_ts,
                    category="weather",
                    event_ticker=m.get("event_ticker"),
                    title=m.get("title"),
                    best_bid=None,
                    best_ask=None,
                    spread_cents=None,
                    bid_depth=None,
                    ask_depth=None,
                    mm_score=None,
                    close_time=_parse_close_ts(m.get("close_time", "")),
                )
                continue

            best_bid = yes_bids[0][0]
            best_ask = yes_asks[0][0]
            spread_cents = best_ask - best_bid
            bid_depth = sum(size for _, size in yes_bids)
            ask_depth = sum(size for _, size in yes_asks)
            mm_score = compute_mm_score(spread_cents, bid_depth, ask_depth)

            _insert_observation(
                db,
                market_ticker=ticker,
                timestamp=now_ts,
                category="weather",
                event_ticker=m.get("event_ticker"),
                title=m.get("title"),
                best_bid=best_bid,
                best_ask=best_ask,
                spread_cents=spread_cents,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                mm_score=mm_score,
                close_time=_parse_close_ts(m.get("close_time", "")),
            )

            m["best_bid"] = best_bid
            m["best_ask"] = best_ask
            m["spread_cents"] = spread_cents
            m["bid_depth_contracts"] = bid_depth
            m["ask_depth_contracts"] = ask_depth
            m["mm_score"] = mm_score
            enriched.append(m)

        db.commit()
    finally:
        db.close()

    elapsed = time.time() - scan_start
    log.info(
        "Scan complete: %d weather markets enriched (%d orderbook failures) in %.1fs",
        len(enriched),
        orderbook_failures,
        elapsed,
    )

    return enriched


def _get_db_at(path: str) -> sqlite3.Connection:
    """Mirror of execution._get_db but against an override path (for tests)."""
    from execution import _create_tables
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    _create_tables(db)
    return db


def _insert_observation(
    db: sqlite3.Connection,
    *,
    market_ticker: str,
    timestamp: int,
    category: str,
    event_ticker: Optional[str],
    title: Optional[str],
    best_bid: Optional[int],
    best_ask: Optional[int],
    spread_cents: Optional[int],
    bid_depth: Optional[int],
    ask_depth: Optional[int],
    mm_score: Optional[float],
    close_time: Optional[int],
) -> None:
    db.execute(
        """
        INSERT INTO market_observations (
            market_ticker, timestamp, category, event_ticker, title,
            best_bid, best_ask, spread_cents,
            bid_depth_contracts, ask_depth_contracts,
            mm_score, close_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market_ticker, timestamp, category, event_ticker, title,
            best_bid, best_ask, spread_cents,
            bid_depth, ask_depth,
            mm_score, close_time,
        ),
    )


# ─── Neglected-market filter ──────────────────────────────────────────────────

def filter_neglected(markets: list[dict], mm_threshold: float = 0.3) -> list[dict]:
    """
    Return only markets with mm_score < threshold — the stale, inattentive
    books that are actually worth hunting for arb edge.

    Markets missing an mm_score (e.g. empty orderbook) are excluded.
    """
    neglected = [
        m for m in markets
        if m.get("mm_score") is not None and m["mm_score"] < mm_threshold
    ]
    log.info(
        "Filter: %d/%d markets below mm_score threshold %.2f",
        len(neglected), len(markets), mm_threshold,
    )
    return neglected


# ─── Weather pipeline ─────────────────────────────────────────────────────────

def scan_weather_pipeline(
    client: KalshiClient,
    db: sqlite3.Connection,
    bankroll: float,
    held_contracts: set[str],
    open_exposure: float,
    day_locked: bool,
) -> list[TradeSignal]:
    """
    Full weather mispricing pipeline:
    1. Fetch weather markets from Kalshi
    2. Parse into WeatherContracts (Denver high-temp only)
    3. Fetch METAR from NOAA
    4. Price each contract with the intraday model
    5. Generate signals where edge exists
    6. Log enriched observations to DB

    Returns list of actionable TradeSignals.
    """
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    now = datetime.now(timezone.utc)
    local_tz = ZoneInfo(CITY_TIMEZONE)
    local_now = now.astimezone(local_tz)
    local_hour = local_now.hour + local_now.minute / 60.0

    in_window = TRADE_WINDOW_START_HOUR <= local_hour <= TRADE_WINDOW_END_HOUR
    if not in_window:
        log.info("Outside trading window (%.1f local) — scan-only mode", local_hour)

    risk_ok = (
        not day_locked
        and in_window
        and open_exposure < bankroll * MAX_TOTAL_OPEN_RISK_PCT
    )

    # 1. Fetch weather markets
    weather_series = fetch_weather_series(client)
    try:
        all_markets = _fetch_all_open_markets(client)
    except Exception as e:
        log.error("Market fetch failed: %s", e)
        return []

    weather_markets = []
    for m in all_markets:
        cat = classify_market(m.get("ticker", ""), m.get("title", ""), weather_series)
        if cat == "weather":
            weather_markets.append(m)

    # 2. Parse contracts
    adapter = WeatherAdapterNOAA()
    ensemble_adapter = EnsembleAdapter()
    model = WeatherModel()
    signals: list[TradeSignal] = []
    priceable_count = 0
    now_ts = int(time.time())

    # 3. Fetch METAR + ensemble once for the station
    metars = adapter.get_recent_metars(TARGET_STATION)

    # Fetch GFS ensemble (shadow mode — logged but doesn't drive signals)
    from datetime import date as _date
    ensemble = ensemble_adapter.get_forecast(target_date=_date.today())
    if ensemble:
        log.info(
            "Ensemble: mean=%.1fF sigma=%.1fF members=%d",
            ensemble.mean_high, ensemble.sigma, len(ensemble.member_highs),
        )
    else:
        log.warning("Ensemble fetch failed — V3 shadow mode disabled this tick")

    for m in weather_markets:
        ticker = m["ticker"]
        contract = parse_weather_contract(m)

        # Fetch orderbook
        try:
            ob_data = client.get_orderbook(ticker)
            yes_bids, yes_asks = _parse_orderbook(ob_data)
        except Exception:
            yes_bids, yes_asks = [], []

        best_bid = yes_bids[0][0] / 100.0 if yes_bids else None
        best_ask = yes_asks[0][0] / 100.0 if yes_asks else None
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        bid_depth = sum(s for _, s in yes_bids) if yes_bids else None
        ask_depth = sum(s for _, s in yes_asks) if yes_asks else None
        mm_score_val = compute_mm_score(
            int(spread * 100) if spread else 0,
            bid_depth or 0, ask_depth or 0
        ) if spread is not None else None

        # Model pricing (only for priceable Denver high-temp contracts)
        estimate = None
        edge_yes = None
        edge_no = None

        if contract.is_priceable and metars:
            priceable_count += 1
            estimate = model.price_contract(contract, metars, now=now)

            # V3 shadow: run ensemble model, log comparison, don't use for signals
            if ensemble:
                v3_est = model.price_contract_ensemble(contract, metars, ensemble, now=now)
                log.info(
                    "SHADOW %s: v2=%.3f v3=%.3f delta=%.3f (ens_mean=%.1f adj=%.1f sigma=%.1f)",
                    ticker, estimate.prob_yes, v3_est.prob_yes,
                    v3_est.prob_yes - estimate.prob_yes,
                    ensemble.mean_high, v3_est.adjusted_high_f, v3_est.sigma_f,
                )

            if best_bid is not None and best_ask is not None:
                market_yes = best_ask  # buy at ask
                market_no = 1.0 - best_bid  # buy no at 1 - best_bid
                edge_yes = estimate.prob_yes - market_yes
                edge_no = (1.0 - estimate.prob_yes) - market_no
                hours_to_peak = model._hours_to_peak(now)

                sig = generate_signal(
                    estimate=estimate,
                    market_yes_price=market_yes,
                    market_no_price=market_no,
                    spread=spread,
                    hours_to_peak=hours_to_peak,
                    held_contracts=held_contracts,
                    risk_ok=risk_ok,
                    threshold_f=contract.threshold_f,
                )
                if sig:
                    signals.append(sig)

        # Log observation with model data
        _insert_observation(
            db,
            market_ticker=ticker,
            timestamp=now_ts,
            category="weather",
            event_ticker=m.get("event_ticker"),
            title=m.get("title"),
            best_bid=int(best_bid * 100) if best_bid else None,
            best_ask=int(best_ask * 100) if best_ask else None,
            spread_cents=int(spread * 100) if spread else None,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            mm_score=mm_score_val,
            close_time=_parse_close_ts(m.get("close_time", "")),
        )
        # Update observation with model columns
        if estimate:
            import json as _json
            db.execute(
                """
                UPDATE market_observations
                SET station_id=?, metric=?, threshold_f=?,
                    model_prob_yes=?, model_confidence=?,
                    expected_high_f=?, adjusted_high_f=?, sigma_f=?,
                    current_temp_f=?, edge_yes=?, edge_no=?,
                    is_priceable=?, parse_status=?, model_version=?,
                    diagnostics_json=?
                WHERE market_ticker=? AND timestamp=?
                """,
                (
                    contract.station_id, contract.metric, contract.threshold_f,
                    estimate.prob_yes, estimate.confidence,
                    estimate.expected_high_f, estimate.adjusted_high_f, estimate.sigma_f,
                    estimate.current_temp_f, edge_yes, edge_no,
                    1, contract.parse_status, estimate.model_version,
                    _json.dumps(estimate.diagnostics),
                    ticker, now_ts,
                ),
            )
        elif not contract.is_priceable:
            db.execute(
                """
                UPDATE market_observations
                SET is_priceable=0, parse_status=?
                WHERE market_ticker=? AND timestamp=?
                """,
                (contract.parse_status, ticker, now_ts),
            )

    db.commit()
    log.info(
        "Weather pipeline: %d markets, %d priceable, %d signals",
        len(weather_markets), priceable_count, len(signals),
    )
    return signals
