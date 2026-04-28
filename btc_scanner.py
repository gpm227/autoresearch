"""
btc_scanner.py — Discover and parse Kalshi BTC 15-minute up/down markets.

Finds the active 15-min market, extracts the target price and time remaining,
pulls the orderbook, and returns a structured opportunity.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from infra import KalshiClient
from scanner import _parse_orderbook
from config import BTC_SERIES_TICKER

log = logging.getLogger("btc_scanner")

# Known series tickers for BTC markets on Kalshi
BTC_SERIES_CANDIDATES = [
    BTC_SERIES_TICKER,  # 15-minute rolling markets — primary target
]


def is_btc15_market(market: dict) -> bool:
    """Check if a market is from the 15-minute BTC series."""
    return market.get("ticker", "").startswith("KXBTC15M")


@dataclass
class BTCMarket:
    """A single Kalshi BTC 15-min up/down market."""
    ticker: str
    event_ticker: str
    title: str
    target_price: float          # the "price to beat"
    close_time: datetime         # when the market settles
    seconds_remaining: float     # seconds until close
    yes_bid: Optional[float]     # best bid for YES (up) in [0, 1]
    yes_ask: Optional[float]     # best ask for YES (up) in [0, 1]
    no_bid: Optional[float]      # best bid for NO (down) in [0, 1]
    no_ask: Optional[float]      # best ask for NO (down) in [0, 1]
    spread: Optional[float]
    bid_depth: int
    ask_depth: int
    volume: int


def _iter_nested_values(value: Any) -> Iterable[Any]:
    """Yield primitive values from nested Kalshi strike payloads."""
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_nested_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_nested_values(nested)
    else:
        yield value


def _coerce_target_price(value: Any) -> Optional[float]:
    """Parse a BTC target price from numeric fields or strings."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value)
    if "TBD" in text.upper():
        return None
    for match in re.finditer(r'\$?([0-9][0-9,]*(?:\.\d+)?)', text):
        try:
            price = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        # Reject incidental numbers like "15" from "15 mins".
        if price >= 1000:
            return price
    return None


def _parse_target_price(title: str) -> Optional[float]:
    """
    Extract the target/reference price from a Kalshi BTC market title.

    Known patterns:
        "BTC 15 min · $71,197.82 target"
        "Will Bitcoin be above $71,250 at ..."
        "Bitcoin above $71,200.00"
    """
    return _coerce_target_price(title)


def _extract_target_price(market: dict) -> Optional[float]:
    """
    Extract the "price to beat" from live Kalshi market payloads.

    KXBTC15M markets are initialized with "Target price: TBD"; once the
    contract opens, Kalshi may expose the numeric target in a strike field,
    subtitle, title, or rules text. We try structured fields first and text last.
    """
    structured_values = [
        market.get("floor_strike"),
        market.get("functional_strike"),
        market.get("expiration_value"),
    ]
    for value in structured_values:
        price = _coerce_target_price(value)
        if price is not None:
            return price

    for value in _iter_nested_values(market.get("custom_strike")):
        price = _coerce_target_price(value)
        if price is not None:
            return price

    text_fields = [
        market.get("subtitle", ""),
        market.get("yes_sub_title", ""),
        market.get("no_sub_title", ""),
        market.get("title", ""),
        market.get("rules_primary", ""),
        market.get("rules_secondary", ""),
    ]
    return _parse_target_price(" ".join(str(field) for field in text_fields))


def _parse_close_time(close_time_str: str) -> Optional[datetime]:
    """Parse ISO-8601 close time from Kalshi."""
    if not close_time_str:
        return None
    try:
        s = close_time_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _is_btc_short_term(market: dict) -> bool:
    """
    Check if a market is a tradeable short-term BTC binary.

    Accepts:
    - "BTC 15 min · $71,197.82 target" (15-min up/down)
    - "Bitcoin price today at 5pm EDT? $71,250 or above" (hourly threshold)
    - Any BTC above/below binary

    Rejects:
    - Range/bracket markets ("$71,000 to 71,249.99")
    - Long-term milestone markets ("When will Bitcoin hit $150k?")
    - Non-BTC markets
    """
    ticker = market.get("ticker", "").upper()
    title = (market.get("title", "") + " " + market.get("subtitle", "")).lower()

    # Must reference BTC/Bitcoin
    if "btc" not in ticker.lower() and "bitcoin" not in title:
        return False

    # REJECT: range/bracket markets (KXBTC series, "Bitcoin price range")
    if "range" in title:
        return False

    # REJECT: long-term milestone markets
    skip_phrases = ["hit", "end of", "this year", "january", "before",
                    "how high", "how low", "cross", "$200k", "$250k",
                    "$150k", "$100k", "next year"]
    if any(p in title for p in skip_phrases):
        return False

    # ACCEPT: KXBTC15M (15-minute markets) or KXBTCD (threshold markets)
    if "KXBTC15M" in ticker or "KXBTCD" in ticker:
        return True

    # ACCEPT: 15-min / 5-min target markets
    if "15 min" in title or "5 min" in title or "target" in title:
        return True

    # ACCEPT: above/below/up/down threshold markets
    if any(w in title for w in ["above", "below", "up", "down"]):
        return True

    # ACCEPT: "bitcoin price" pattern (hourly/daily markets)
    if "bitcoin price" in title or "btc price" in title:
        return True

    return False


def scan_btc_markets(client: KalshiClient) -> list[BTCMarket]:
    """
    Find all active BTC 15-min up/down markets on Kalshi.

    Strategy:
    1. Fetch open markets filtering by known BTC series tickers
    2. Also do a broad scan for BTC-tagged markets
    3. Parse target price and time remaining
    4. Pull orderbook for each
    """
    now = datetime.now(timezone.utc)
    found_markets: list[BTCMarket] = []
    seen_tickers: set[str] = set()

    # Approach 1: Try series_ticker filter in a close-time window around now.
    # KXBTC15M has thousands of initialized future contracts; without the
    # timestamp window, the current contract is often buried many pages deep.
    for series in BTC_SERIES_CANDIDATES:
        try:
            if series == "KXBTC15M":
                min_close = int((now - timedelta(minutes=1)).timestamp())
                max_close = int((now + timedelta(minutes=25)).timestamp())
                resp = _fetch_markets_by_series(
                    client,
                    series,
                    min_close_ts=min_close,
                    max_close_ts=max_close,
                )
            else:
                resp = _fetch_markets_by_series(client, series, status="open", limit=200)
            markets = resp.get("markets", [])
            if series == "KXBTC15M":
                log.info("KXBTC15M time-window query returned %d markets", len(markets))
                for m in markets[:3]:
                    close = m.get("close_time", "")
                    strike = _extract_target_price(m) or "?"
                    title = m.get("title", "")[:60]
                    subtitle = m.get("subtitle", "")[:60]
                    log.info("  raw: %s | status=%s strike=%s | close=%s | title=%s | subtitle=%s",
                             m.get("ticker"), m.get("status"), strike, close, title, subtitle)
            for m in markets:
                t = m.get("ticker", "")
                if t not in seen_tickers:
                    seen_tickers.add(t)
                    parsed = _parse_btc_market(m, now, client)
                    if parsed:
                        found_markets.append(parsed)
                    elif is_btc15_market(m):
                        secs = ((_parse_close_time(m.get("close_time","")) or now) - now).total_seconds()
                        log.info("  KXBTC15M REJECTED: %s t=%.0fs", t, secs)
        except Exception as e:
            log.debug("Series %s fetch failed: %s", series, e)

    # Intentionally no hourly/daily fallback: this bot is only for BTC 15m
    # up/down markets.

    # Sort by time remaining (nearest first)
    found_markets.sort(key=lambda m: m.seconds_remaining)

    log.info(
        "BTC scan: %d markets found (%d total tickers checked)",
        len(found_markets), len(seen_tickers),
    )
    return found_markets


def _fetch_markets_by_series(
    client: KalshiClient,
    series_ticker: str,
    status: str = "",
    limit: int = 1000,
    min_close_ts: int | None = None,
    max_close_ts: int | None = None,
) -> dict:
    """Fetch markets for one series, paginating defensively."""
    markets: list[dict] = []
    cursor = ""
    pages = 0
    while True:
        resp = client.get_markets(
            status=status,
            limit=limit,
            cursor=cursor,
            series_ticker=series_ticker,
            min_close_ts=str(min_close_ts or ""),
            max_close_ts=str(max_close_ts or ""),
            mve_filter="" if (min_close_ts or max_close_ts) else "exclude",
        )
        markets.extend(resp.get("markets", []))
        cursor = resp.get("cursor", "")
        pages += 1
        if not cursor or pages >= 10:
            break
    return {"markets": markets, "cursor": cursor}


def _parse_btc_market(
    market: dict,
    now: datetime,
    client: KalshiClient,
) -> Optional[BTCMarket]:
    """Parse a raw Kalshi market dict into a BTCMarket, or None if not valid."""

    status = str(market.get("status", "")).lower()
    if status and status not in {"open", "active"}:
        return None

    if not _is_btc_short_term(market):
        log.debug(
            "Filter rejected: ticker=%s title=%r",
            market.get("ticker"), (market.get("title", "") + " " + market.get("subtitle", ""))[:80],
        )
        return None

    ticker = market["ticker"]
    title = market.get("title", "")
    subtitle = market.get("subtitle", "")
    full_title = f"{title} {subtitle}".strip()

    # Parse target price — live KXBTC15M starts as "Target price: TBD" and
    # only becomes priceable once Kalshi exposes the numeric target.
    target_price = _extract_target_price(market)
    if target_price is None or target_price <= 0:
        log.debug(
            "Could not parse target price from: ticker=%s status=%s title=%r subtitle=%r",
            ticker, market.get("status"), title, subtitle,
        )
        return None

    # Parse close time
    close_time = _parse_close_time(market.get("close_time", ""))
    if close_time is None:
        log.debug("Could not parse close time for %s", ticker)
        return None

    seconds_remaining = (close_time - now).total_seconds()
    if seconds_remaining <= 0:
        return None

    # Time filters depend on series
    if is_btc15_market(market):
        # 15-min markets: only trade 2-20 min window
        if seconds_remaining > 1200 or seconds_remaining < 120:
            return None
    else:
        # KXBTCD hourly/daily: skip if more than 2 hours out
        if seconds_remaining > 7200:
            return None

    # Fetch orderbook
    try:
        ob_data = client.get_orderbook(ticker)
        yes_bids, yes_asks = _parse_orderbook(ob_data)
    except Exception:
        yes_bids, yes_asks = [], []

    yes_bid = yes_bids[0][0] / 100.0 if yes_bids else None
    yes_ask = yes_asks[0][0] / 100.0 if yes_asks else None
    spread = (yes_ask - yes_bid) if (yes_bid is not None and yes_ask is not None) else None

    no_bid = (1.0 - yes_ask) if yes_ask is not None else None
    no_ask = (1.0 - yes_bid) if yes_bid is not None else None

    bid_depth = sum(s for _, s in yes_bids) if yes_bids else 0
    ask_depth = sum(s for _, s in yes_asks) if yes_asks else 0

    volume = market.get("volume", 0) or 0

    return BTCMarket(
        ticker=ticker,
        event_ticker=market.get("event_ticker", ""),
        title=full_title,
        target_price=target_price,
        close_time=close_time,
        seconds_remaining=seconds_remaining,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        spread=spread,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        volume=volume,
    )
