"""
execution.py — Kalshi MLB execution engine.

Handles: market resolution, order placement, trade logging (SQLite),
outcome tracking, CLV computation, and daily analytics.

Kirk picks → structured trade → Kalshi order → logged with full snapshot.
No prediction models. No signal stacking. Execution and measurement only.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    from backports.zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
from pathlib import Path
from typing import Optional

from infra import Env, KalshiClient
from file_io import read_json, ACTIVE_MARKETS_FILE

# ─── Constants ────────────────────────────────────────────────────────────────

DB_PATH = Path(__file__).parent / "trades.db"

BET_SIZE_DOLLARS = 5
MAX_BET_DOLLARS = 20
DAILY_BANKROLL_CAP_PCT = 0.25
MIN_ASK_CENTS = 10
MAX_ASK_CENTS = 90

# Kalshi MLB team abbreviations used in tickers
# Maps common abbreviations → Kalshi ticker abbreviations
_TEAM_TICKER_MAP = {
    # Standard
    "ARI": "AZ", "ATL": "ATL", "BAL": "BAL", "BOS": "BOS",
    "CHC": "CHC", "CIN": "CIN", "CLE": "CLE", "COL": "COL",
    "CWS": "CWS", "DET": "DET", "HOU": "HOU", "KC":  "KC",
    "LAA": "LAA", "LAD": "LAD", "MIA": "MIA", "MIL": "MIL",
    "MIN": "MIN", "NYM": "NYM", "NYY": "NYY", "OAK": "OAK",
    "PHI": "PHI", "PIT": "PIT", "SD":  "SD",  "SEA": "SEA",
    "SF":  "SF",  "STL": "STL", "TB":  "TB",  "TEX": "TEX",
    "TOR": "TOR", "WSH": "WSH",
    # Aliases — Kalshi uses "AZ" for Arizona in tickers
    "WAS": "WSH",
    # Note: Kalshi uses "ATH" for Athletics, same as our game_key. Do NOT map ATH→OAK.
}


# ─── Database ─────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    """Get or create the trades database."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    _create_tables(db)
    return db


def _create_tables(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            game_key TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            bet_type TEXT NOT NULL DEFAULT 'moneyline',
            entry_price INTEGER NOT NULL,
            best_bid_at_entry INTEGER NOT NULL,
            best_ask_at_entry INTEGER NOT NULL,
            contracts INTEGER NOT NULL,
            stake_cents INTEGER NOT NULL,
            bankroll_before_cents INTEGER NOT NULL,
            bankroll_after_cents INTEGER NOT NULL,
            order_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            kirk_pick TEXT,
            discord_user_id TEXT,
            discord_username TEXT
        );

        CREATE TABLE IF NOT EXISTS outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL REFERENCES trades(id),
            result TEXT NOT NULL,
            settlement_price INTEGER,
            pnl_cents INTEGER NOT NULL,
            closing_price INTEGER,
            clv_cents INTEGER,
            resolved_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_ticker TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            bid INTEGER NOT NULL,
            ask INTEGER NOT NULL,
            mid_price INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_spend (
            date TEXT PRIMARY KEY,
            total_staked_cents INTEGER NOT NULL DEFAULT 0,
            bet_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS market_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_ticker TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            category TEXT NOT NULL,
            event_ticker TEXT,
            title TEXT,
            best_bid INTEGER,
            best_ask INTEGER,
            spread_cents INTEGER,
            bid_depth_contracts INTEGER,
            ask_depth_contracts INTEGER,
            mm_score REAL,
            close_time INTEGER
        );

        CREATE TABLE IF NOT EXISTS arb_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            market_ticker TEXT NOT NULL,
            category TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price INTEGER NOT NULL,
            fair_price INTEGER NOT NULL,
            edge_cents INTEGER NOT NULL,
            oracle_source TEXT NOT NULL,
            best_bid_at_entry INTEGER NOT NULL,
            best_ask_at_entry INTEGER NOT NULL,
            depth_at_entry INTEGER NOT NULL,
            mm_score_at_entry REAL NOT NULL,
            contracts INTEGER NOT NULL,
            stake_cents INTEGER NOT NULL,
            bankroll_before_cents INTEGER NOT NULL,
            order_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            settlement_price INTEGER,
            pnl_cents INTEGER,
            resolved_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            market_ticker TEXT NOT NULL,
            category TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price INTEGER NOT NULL,
            fair_price INTEGER NOT NULL,
            edge_cents INTEGER NOT NULL,
            oracle_source TEXT NOT NULL,
            best_bid_at_entry INTEGER NOT NULL,
            best_ask_at_entry INTEGER NOT NULL,
            depth_at_entry INTEGER NOT NULL,
            mm_score_at_entry REAL NOT NULL,
            contracts INTEGER NOT NULL,
            stake_cents INTEGER NOT NULL,
            bankroll_before_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            settlement_price INTEGER,
            pnl_cents INTEGER,
            resolved_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id TEXT NOT NULL,
            market_id TEXT,
            side TEXT NOT NULL,
            quantity REAL NOT NULL,
            entry_price REAL NOT NULL,
            max_loss REAL NOT NULL,
            model_prob REAL,
            edge REAL,
            confidence REAL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            settlement_price REAL,
            realized_pnl REAL,
            status TEXT NOT NULL DEFAULT 'open'
        );

        CREATE TABLE IF NOT EXISTS daily_pnl (
            date TEXT PRIMARY KEY,
            starting_balance REAL NOT NULL,
            ending_balance REAL NOT NULL,
            realized_pnl REAL NOT NULL,
            aggregate_pnl REAL NOT NULL,
            trades_count INTEGER NOT NULL,
            wins INTEGER NOT NULL,
            losses INTEGER NOT NULL,
            max_drawdown REAL NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS risk_state (
            date TEXT PRIMARY KEY,
            start_balance REAL NOT NULL,
            daily_realized_pnl REAL NOT NULL,
            trades_count INTEGER NOT NULL,
            consecutive_losses INTEGER NOT NULL,
            day_locked INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
    """)
    db.commit()


def _migrate_market_observations(db: sqlite3.Connection) -> None:
    """Add model-output columns to market_observations. Idempotent."""
    new_cols = [
        ("station_id", "TEXT"),
        ("metric", "TEXT"),
        ("threshold_f", "REAL"),
        ("model_prob_yes", "REAL"),
        ("model_confidence", "REAL"),
        ("expected_high_f", "REAL"),
        ("adjusted_high_f", "REAL"),
        ("sigma_f", "REAL"),
        ("current_temp_f", "REAL"),
        ("edge_yes", "REAL"),
        ("edge_no", "REAL"),
        ("is_priceable", "INTEGER"),
        ("parse_status", "TEXT"),
        ("model_version", "TEXT"),
        ("diagnostics_json", "TEXT"),
    ]
    for col_name, col_type in new_cols:
        try:
            db.execute(f"ALTER TABLE market_observations ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
    db.commit()


# ─── Market Resolver ──────────────────────────────────────────────────────────

@dataclass
class ResolvedMarket:
    ticker: str
    title: str
    side: str           # "yes" or "no"
    best_bid: int       # cents
    best_ask: int       # cents
    team_abbr: str      # the team Kirk picked


def resolve_market(game_key: str, pick_abbr: str, client: KalshiClient) -> Optional[ResolvedMarket]:
    """
    Map a game pick to a Kalshi market via the events API.
    game_key: "NYY@BOS"
    pick_abbr: "NYY" (team Kirk picked to win)
    Returns ResolvedMarket or None if not found.
    """
    away, home = game_key.split("@")
    kalshi_away = _TEAM_TICKER_MAP.get(away, away)
    kalshi_home = _TEAM_TICKER_MAP.get(home, home)
    pick_kalshi = _TEAM_TICKER_MAP.get(pick_abbr, pick_abbr)

    # Use events API — the markets API doesn't reliably return same-day KXMLBGAME markets
    try:
        resp = client._request(
            "GET",
            "/events?status=open&limit=50&with_nested_markets=true&series_ticker=KXMLBGAME"
        )
        events = resp.get("events", [])
    except Exception as e:
        print(f"[execution] Events API failed: {e}", flush=True)
        return None

    # Find the event matching this game's teams
    for event in events:
        event_ticker = event.get("event_ticker", "").upper()
        if kalshi_away.upper() in event_ticker and kalshi_home.upper() in event_ticker:
            # Found the event — now find the right market side
            for m in event.get("markets", []):
                ticker = m.get("ticker", "")
                suffix = ticker.rsplit("-", 1)[-1].upper()
                if suffix == pick_kalshi.upper():
                    ob = _fetch_orderbook(ticker, client)
                    if ob is None:
                        continue
                    return ResolvedMarket(
                        ticker=ticker,
                        title=event.get("title", ""),
                        side="yes",
                        best_bid=ob["bid"],
                        best_ask=ob["ask"],
                        team_abbr=pick_abbr,
                    )

            # Kirk's team might not match the suffix — bet NO on opponent's ticker
            for m in event.get("markets", []):
                ticker = m.get("ticker", "")
                suffix = ticker.rsplit("-", 1)[-1].upper()
                if suffix != pick_kalshi.upper():
                    ob = _fetch_orderbook(ticker, client)
                    if ob is None:
                        continue
                    return ResolvedMarket(
                        ticker=ticker,
                        title=event.get("title", ""),
                        side="no",
                        best_bid=ob["bid"],
                        best_ask=ob["ask"],
                        team_abbr=pick_abbr,
                    )

    return None


@dataclass
class ResolvedTotalMarket:
    ticker: str
    title: str
    side: str           # "yes" = over, "no" = under
    strike: float       # e.g. 7.5
    best_bid: int       # cents
    best_ask: int       # cents
    game_key: str


def resolve_total_market(
    game_key: str, pick: str, client: KalshiClient, total_line: Optional[float] = None
) -> Optional[ResolvedTotalMarket]:
    """
    Map an over/under pick to a Kalshi total runs market.
    game_key: "NYY@BOS"
    pick: "over" or "under"
    total_line: if known (e.g. 7.5), find that exact line. Otherwise pick the main line.
    Returns ResolvedTotalMarket or None.
    """
    away, home = game_key.split("@")
    kalshi_away = _TEAM_TICKER_MAP.get(away, away)
    kalshi_home = _TEAM_TICKER_MAP.get(home, home)

    try:
        resp = client._request(
            "GET",
            "/events?status=open&limit=50&with_nested_markets=true&series_ticker=KXMLBTOTAL"
        )
        events = resp.get("events", [])
    except Exception as e:
        print(f"[execution] Total events API failed: {e}", flush=True)
        return None

    for event in events:
        event_ticker = event.get("event_ticker", "").upper()
        if kalshi_away.upper() in event_ticker and kalshi_home.upper() in event_ticker:
            markets = event.get("markets", [])
            if not markets:
                continue

            # Find the right line
            best_market = None
            best_distance = 999.0

            for m in markets:
                ticker = m.get("ticker", "")
                # Strike from ticker: suffix -8 means over 7.5, -7 means over 6.5
                suffix = ticker.rsplit("-", 1)[-1]
                try:
                    strike = float(suffix) - 0.5
                except ValueError:
                    continue

                if total_line is not None:
                    distance = abs(strike - total_line)
                else:
                    # No line from RotoWire — pick the line closest to 50/50
                    ybd = m.get("yes_bid_dollars", "0")
                    yad = m.get("yes_ask_dollars", "0")
                    mid = (float(ybd) + float(yad)) / 2
                    # Only consider markets with some liquidity
                    if float(ybd) == 0 and float(yad) == 0:
                        continue
                    distance = abs(mid - 0.50)

                if distance < best_distance:
                    best_distance = distance
                    best_market = (ticker, strike, m)

            if best_market is None:
                continue

            ticker, strike, m_data = best_market
            ob = _fetch_orderbook(ticker, client)
            if ob is None:
                continue

            # "over" = YES on the total market, "under" = NO
            side = "yes" if pick.lower() == "over" else "no"

            return ResolvedTotalMarket(
                ticker=ticker,
                title=event.get("title", ""),
                side=side,
                strike=strike,
                best_bid=ob["bid"],
                best_ask=ob["ask"],
                game_key=game_key,
            )

    return None


def execute_total_pick(
    game_key: str,
    pick: str,  # "over" or "under"
    total_line: Optional[float] = None,
    discord_user_id: str = "",
    discord_username: str = "",
) -> TradeResult:
    """Execute Kirk's O/U pick as a real Kalshi order."""
    # Kill switch — touch PAUSED file in repo root to halt all betting
    if os.path.exists(os.path.join(os.path.dirname(__file__), "PAUSED")):
        return TradeResult(False, "Betting paused (PAUSED file present)")
    client = KalshiClient(env=Env.LIVE)
    db = _get_db()

    # 1. Check daily spend cap
    today_str = datetime.now(tz=_ET).date().isoformat()
    row = db.execute(
        "SELECT total_staked_cents, bet_count FROM daily_spend WHERE date = ?",
        (today_str,)
    ).fetchone()
    daily_staked = row["total_staked_cents"] if row else 0

    try:
        bal_resp = client.get_balance()
        balance_cents = bal_resp.get("balance", 0)
    except Exception as e:
        db.close()
        return TradeResult(False, f"Failed to get balance: {e}")

    daily_cap_cents = int(balance_cents * DAILY_BANKROLL_CAP_PCT)
    if daily_staked >= daily_cap_cents:
        db.close()
        return TradeResult(
            False,
            f"Daily cap reached: ${daily_staked/100:.2f} of ${daily_cap_cents/100:.2f}"
        )

    # 2. Resolve market
    market = resolve_total_market(game_key, pick, client, total_line)
    if market is None:
        db.close()
        return TradeResult(False, f"No Kalshi O/U market found for {game_key}")

    # 3. Price — for YES (over), use yes_ask. For NO (under), use 100-yes_bid
    if market.side == "yes":
        price = market.best_ask
    else:
        price = 100 - market.best_bid

    if price < MIN_ASK_CENTS:
        db.close()
        return TradeResult(False, f"Price too low ({price}c)")
    if price > MAX_ASK_CENTS:
        db.close()
        return TradeResult(False, f"Price too high ({price}c)")
    if price <= 0:
        db.close()
        return TradeResult(False, "No liquidity")

    # 4. Size — $5 flat
    stake_cents = BET_SIZE_DOLLARS * 100
    remaining_cap = daily_cap_cents - daily_staked
    if stake_cents > remaining_cap:
        stake_cents = remaining_cap
    if stake_cents > MAX_BET_DOLLARS * 100:
        stake_cents = MAX_BET_DOLLARS * 100
    if stake_cents <= 0:
        db.close()
        return TradeResult(False, "Daily cap exhausted")

    contracts = stake_cents // price
    if contracts <= 0:
        db.close()
        return TradeResult(False, f"Can't afford contracts at {price}c")

    actual_stake = contracts * price

    # 5. Snapshot
    db.execute(
        "INSERT INTO market_snapshots (market_ticker, timestamp, bid, ask, mid_price) VALUES (?, ?, ?, ?, ?)",
        (market.ticker, int(time.time()), market.best_bid, market.best_ask,
         (market.best_bid + market.best_ask) // 2)
    )

    # 6. Place order
    try:
        order_resp = client.create_order(
            ticker=market.ticker,
            side=market.side,
            contracts=contracts,
            price_cents=price,
        )
        order_data = order_resp.get("order", {})
        order_id = order_data.get("order_id", "")
        if not order_id:
            err = order_resp.get("error", {}).get("message", str(order_resp))
            db.close()
            return TradeResult(False, f"Order rejected: {err}")
        status = order_data.get("status", "resting")
    except Exception as e:
        db.close()
        return TradeResult(False, f"Order failed: {e}")

    # 7. Updated balance
    try:
        new_bal = client.get_balance().get("balance", balance_cents)
    except Exception:
        new_bal = balance_cents - actual_stake

    # 8. Log
    pick_label = f"{pick} {market.strike}"
    trade_id = _log_trade(db, {
        "timestamp": int(time.time()),
        "game_key": game_key,
        "market_ticker": market.ticker,
        "side": market.side,
        "bet_type": "total",
        "entry_price": price,
        "best_bid_at_entry": market.best_bid,
        "best_ask_at_entry": market.best_ask,
        "contracts": contracts,
        "stake_cents": actual_stake,
        "bankroll_before_cents": balance_cents,
        "bankroll_after_cents": new_bal,
        "order_id": order_id,
        "status": status,
        "kirk_pick": pick_label,
        "discord_user_id": discord_user_id,
        "discord_username": discord_username,
    })

    # 9. Update daily spend
    db.execute("""
        INSERT INTO daily_spend (date, total_staked_cents, bet_count)
        VALUES (?, ?, 1)
        ON CONFLICT(date) DO UPDATE SET
            total_staked_cents = total_staked_cents + ?,
            bet_count = bet_count + 1
    """, (today_str, actual_stake, actual_stake))
    db.commit()
    db.close()

    side_label = "OVER" if market.side == "yes" else "UNDER"
    return TradeResult(
        success=True,
        message=f"${actual_stake/100:.2f} on {side_label} {market.strike} @ {price}c ({contracts} contracts)",
        trade_id=trade_id,
        ticker=market.ticker,
        side=market.side,
        contracts=contracts,
        price_cents=price,
        stake_cents=actual_stake,
        order_id=order_id,
    )


def _fetch_orderbook(ticker: str, client: KalshiClient) -> Optional[dict]:
    """Fetch bid/ask for a market. Returns {bid, ask} in cents. None on failure.

    Uses the market endpoint directly for reliable NBBO (yes_bid_dollars, yes_ask_dollars).
    The orderbook endpoint has sparse books that give misleading bid/ask.
    """
    try:
        resp = client.get_market(ticker)
        m = resp.get("market", resp)

        # Dollar-based fields (current API)
        yes_bid_str = m.get("yes_bid_dollars")
        yes_ask_str = m.get("yes_ask_dollars")
        if yes_bid_str and yes_ask_str:
            best_bid = round(float(yes_bid_str) * 100)
            best_ask = round(float(yes_ask_str) * 100)
            return {"bid": best_bid, "ask": best_ask}

        # Legacy cents fields
        best_bid = m.get("yes_bid", 0)
        best_ask = m.get("yes_ask", 0)
        return {"bid": best_bid, "ask": best_ask}
    except Exception as e:
        print(f"[execution] Market fetch failed for {ticker}: {e}", flush=True)
        return None


# ─── Execution Engine ─────────────────────────────────────────────────────────

@dataclass
class TradeResult:
    success: bool
    message: str
    trade_id: Optional[int] = None
    ticker: Optional[str] = None
    side: Optional[str] = None
    contracts: int = 0
    price_cents: int = 0
    stake_cents: int = 0
    order_id: Optional[str] = None


def execute_pick(
    game_key: str,
    pick_abbr: str,
    bet_type: str = "moneyline",
    discord_user_id: str = "",
    discord_username: str = "",
) -> TradeResult:
    """
    Execute Kirk's pick as a real Kalshi order.
    Returns TradeResult with success/failure and details.
    """
    # Kill switch — touch PAUSED file in repo root to halt all betting
    if os.path.exists(os.path.join(os.path.dirname(__file__), "PAUSED")):
        return TradeResult(False, "Betting paused (PAUSED file present)")
    client = KalshiClient(env=Env.LIVE)
    db = _get_db()

    # 1. Check daily spend cap
    today_str = datetime.now(tz=_ET).date().isoformat()
    row = db.execute(
        "SELECT total_staked_cents, bet_count FROM daily_spend WHERE date = ?",
        (today_str,)
    ).fetchone()
    daily_staked = row["total_staked_cents"] if row else 0
    daily_bets = row["bet_count"] if row else 0

    # Get current balance
    try:
        bal_resp = client.get_balance()
        balance_cents = bal_resp.get("balance", 0)
    except Exception as e:
        return TradeResult(False, f"Failed to get balance: {e}")

    daily_cap_cents = int(balance_cents * DAILY_BANKROLL_CAP_PCT)
    if daily_staked >= daily_cap_cents:
        return TradeResult(
            False,
            f"Daily cap reached: ${daily_staked/100:.2f} staked of ${daily_cap_cents/100:.2f} limit"
        )

    # 2. Resolve market
    market = resolve_market(game_key, pick_abbr, client)
    if market is None:
        return TradeResult(False, f"No Kalshi market found for {pick_abbr} in {game_key}")

    # 3. Validate price
    if market.side == "yes":
        price = market.best_ask
    else:
        # For NO side, the price is 100 - best_bid (what we pay for NO)
        price = 100 - market.best_bid

    if price < MIN_ASK_CENTS:
        return TradeResult(False, f"Price too low ({price}c) — skipping")
    if price > MAX_ASK_CENTS:
        return TradeResult(False, f"Price too high ({price}c) — skipping")
    if price <= 0:
        return TradeResult(False, "No liquidity — bid or ask is 0")

    # 4. Size the bet
    stake_cents = BET_SIZE_DOLLARS * 100  # $10 = 1000 cents
    remaining_cap = daily_cap_cents - daily_staked
    if stake_cents > remaining_cap:
        stake_cents = remaining_cap
    if stake_cents > MAX_BET_DOLLARS * 100:
        stake_cents = MAX_BET_DOLLARS * 100
    if stake_cents <= 0:
        return TradeResult(False, "Daily cap exhausted")

    contracts = stake_cents // price
    if contracts <= 0:
        return TradeResult(False, f"Can't afford any contracts at {price}c")

    actual_stake = contracts * price

    # 5. Snapshot the market
    db.execute(
        "INSERT INTO market_snapshots (market_ticker, timestamp, bid, ask, mid_price) VALUES (?, ?, ?, ?, ?)",
        (market.ticker, int(time.time()), market.best_bid, market.best_ask,
         (market.best_bid + market.best_ask) // 2)
    )

    # 6. Place the order
    try:
        order_resp = client.create_order(
            ticker=market.ticker,
            side=market.side,
            contracts=contracts,
            price_cents=price,
        )
        order_id = order_resp.get("order", {}).get("order_id", "")
        if not order_id:
            # Check for error
            err = order_resp.get("error", {}).get("message", str(order_resp))
            return TradeResult(False, f"Order rejected: {err}")
        status = "filled"
    except Exception as e:
        return TradeResult(False, f"Order failed: {e}")

    # 7. Get updated balance
    try:
        new_bal = client.get_balance().get("balance", balance_cents)
    except Exception:
        new_bal = balance_cents - actual_stake

    # 8. Log the trade
    trade_id = _log_trade(db, {
        "timestamp": int(time.time()),
        "game_key": game_key,
        "market_ticker": market.ticker,
        "side": market.side,
        "bet_type": bet_type,
        "entry_price": price,
        "best_bid_at_entry": market.best_bid,
        "best_ask_at_entry": market.best_ask,
        "contracts": contracts,
        "stake_cents": actual_stake,
        "bankroll_before_cents": balance_cents,
        "bankroll_after_cents": new_bal,
        "order_id": order_id,
        "status": status,
        "kirk_pick": pick_abbr,
        "discord_user_id": discord_user_id,
        "discord_username": discord_username,
    })

    # 9. Update daily spend
    db.execute("""
        INSERT INTO daily_spend (date, total_staked_cents, bet_count)
        VALUES (?, ?, 1)
        ON CONFLICT(date) DO UPDATE SET
            total_staked_cents = total_staked_cents + ?,
            bet_count = bet_count + 1
    """, (today_str, actual_stake, actual_stake))
    db.commit()
    db.close()

    return TradeResult(
        success=True,
        message=f"Placed ${actual_stake/100:.2f} on {pick_abbr} {market.side.upper()} @ {price}c ({contracts} contracts)",
        trade_id=trade_id,
        ticker=market.ticker,
        side=market.side,
        contracts=contracts,
        price_cents=price,
        stake_cents=actual_stake,
        order_id=order_id,
    )


def _log_trade(db: sqlite3.Connection, data: dict) -> int:
    cur = db.execute("""
        INSERT INTO trades (
            timestamp, game_key, market_ticker, side, bet_type,
            entry_price, best_bid_at_entry, best_ask_at_entry,
            contracts, stake_cents, bankroll_before_cents, bankroll_after_cents,
            order_id, status, kirk_pick, discord_user_id, discord_username
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["timestamp"], data["game_key"], data["market_ticker"],
        data["side"], data["bet_type"], data["entry_price"],
        data["best_bid_at_entry"], data["best_ask_at_entry"],
        data["contracts"], data["stake_cents"],
        data["bankroll_before_cents"], data["bankroll_after_cents"],
        data["order_id"], data["status"], data["kirk_pick"],
        data["discord_user_id"], data["discord_username"],
    ))
    db.commit()
    return cur.lastrowid


# ─── Outcome Resolution ──────────────────────────────────────────────────────

def resolve_outcomes(client: Optional[KalshiClient] = None) -> int:
    """
    Check all unresolved trades and record outcomes.
    Returns number of newly resolved trades.
    """
    if client is None:
        client = KalshiClient(env=Env.LIVE)
    db = _get_db()

    unresolved = db.execute("""
        SELECT t.id, t.market_ticker, t.side, t.entry_price, t.contracts, t.stake_cents
        FROM trades t
        LEFT JOIN outcomes o ON o.trade_id = t.id
        WHERE o.id IS NULL AND t.status = 'filled'
    """).fetchall()

    resolved_count = 0
    for trade in unresolved:
        try:
            market_data = client.get_market(trade["market_ticker"])
            market_info = market_data.get("market", market_data)
            status = market_info.get("status", "")
            result_str = market_info.get("result", "")

            if status not in ("settled", "closed") or not result_str:
                continue

            # result is "yes" or "no"
            won = (result_str == trade["side"])
            settlement = 100 if result_str == "yes" else 0

            if won:
                pnl_cents = trade["contracts"] * (100 - trade["entry_price"])
            else:
                pnl_cents = -(trade["contracts"] * trade["entry_price"])

            # Get closing price for CLV
            closing_price = _get_closing_price(trade["market_ticker"], client)
            clv = (closing_price - trade["entry_price"]) if closing_price else None

            db.execute("""
                INSERT INTO outcomes (trade_id, result, settlement_price, pnl_cents, closing_price, clv_cents, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                trade["id"],
                "win" if won else "loss",
                settlement,
                pnl_cents,
                closing_price,
                clv,
                int(time.time()),
            ))
            resolved_count += 1

        except Exception as e:
            print(f"[execution] Failed to resolve trade {trade['id']}: {e}", flush=True)

    db.commit()
    db.close()
    return resolved_count


def _get_closing_price(ticker: str, client: KalshiClient) -> Optional[int]:
    """Get the last traded price before settlement. Best proxy for closing line."""
    try:
        market_data = client.get_market(ticker)
        market_info = market_data.get("market", market_data)
        return market_info.get("last_price", None)
    except Exception:
        return None


# ─── Analytics ────────────────────────────────────────────────────────────────

@dataclass
class DailyReport:
    date: str
    total_bets: int
    total_staked_cents: int
    pnl_cents: int
    avg_clv_cents: Optional[float]
    pct_positive_clv: Optional[float]
    win_rate: Optional[float]
    expected_win_rate: Optional[float]
    bankroll_cents: int
    bets_by_bucket: dict  # price bucket → {count, wins, pnl}


def build_daily_report(report_date: Optional[str] = None) -> Optional[DailyReport]:
    """Build analytics for a given date (default: yesterday)."""
    if report_date is None:
        report_date = (date.today() - timedelta(days=1)).isoformat()

    db = _get_db()

    # Get day boundaries (UTC)
    day_start = int(datetime.fromisoformat(report_date + "T00:00:00+00:00").timestamp())
    day_end = day_start + 86400

    trades = db.execute("""
        SELECT t.*, o.result, o.pnl_cents as outcome_pnl, o.clv_cents, o.closing_price
        FROM trades t
        LEFT JOIN outcomes o ON o.trade_id = t.id
        WHERE t.timestamp >= ? AND t.timestamp < ? AND t.status = 'filled'
    """, (day_start, day_end)).fetchall()

    if not trades:
        db.close()
        return None

    total_bets = len(trades)
    total_staked = sum(t["stake_cents"] for t in trades)

    # PnL from resolved trades
    resolved = [t for t in trades if t["result"] is not None]
    pnl = sum(t["outcome_pnl"] for t in resolved)

    # CLV
    clvs = [t["clv_cents"] for t in resolved if t["clv_cents"] is not None]
    avg_clv = sum(clvs) / len(clvs) if clvs else None
    pct_pos_clv = sum(1 for c in clvs if c > 0) / len(clvs) if clvs else None

    # Win rate vs expected
    wins = sum(1 for t in resolved if t["result"] == "win")
    win_rate = wins / len(resolved) if resolved else None
    expected = sum(t["entry_price"] / 100.0 for t in resolved) / len(resolved) if resolved else None

    # Price buckets
    buckets = {"10-30": [], "30-50": [], "50-70": [], "70-90": []}
    for t in resolved:
        p = t["entry_price"]
        if p < 30:
            buckets["10-30"].append(t)
        elif p < 50:
            buckets["30-50"].append(t)
        elif p < 70:
            buckets["50-70"].append(t)
        else:
            buckets["70-90"].append(t)

    bets_by_bucket = {}
    for bucket, bt in buckets.items():
        if bt:
            bw = sum(1 for t in bt if t["result"] == "win")
            bp = sum(t["outcome_pnl"] for t in bt)
            bets_by_bucket[bucket] = {"count": len(bt), "wins": bw, "pnl_cents": bp}

    # Current bankroll
    try:
        client = KalshiClient(env=Env.LIVE)
        bankroll = client.get_balance().get("balance", 0)
    except Exception:
        bankroll = trades[-1]["bankroll_after_cents"] if trades else 0

    db.close()

    return DailyReport(
        date=report_date,
        total_bets=total_bets,
        total_staked_cents=total_staked,
        pnl_cents=pnl,
        avg_clv_cents=avg_clv,
        pct_positive_clv=pct_pos_clv,
        win_rate=win_rate,
        expected_win_rate=expected,
        bankroll_cents=bankroll,
        bets_by_bucket=bets_by_bucket,
    )


def format_daily_report(report: DailyReport) -> str:
    """Format daily report for Discord."""
    lines = [
        f"**Daily Report — {report.date}**",
        "",
        f"Bets: {report.total_bets}",
        f"Staked: ${report.total_staked_cents / 100:.2f}",
        f"P&L: {'+'  if report.pnl_cents >= 0 else ''}{report.pnl_cents / 100:.2f}",
    ]

    if report.avg_clv_cents is not None:
        lines.append(f"Avg CLV: {'+' if report.avg_clv_cents >= 0 else ''}{report.avg_clv_cents:.1f}c")
    if report.pct_positive_clv is not None:
        lines.append(f"CLV+ rate: {report.pct_positive_clv:.0%}")
    if report.win_rate is not None:
        lines.append(f"Win rate: {report.win_rate:.0%}")
    if report.expected_win_rate is not None:
        lines.append(f"Expected (from price): {report.expected_win_rate:.0%}")

    lines.append(f"Bankroll: ${report.bankroll_cents / 100:.2f}")

    if report.bets_by_bucket:
        lines.append("")
        lines.append("**By price bucket:**")
        for bucket, data in sorted(report.bets_by_bucket.items()):
            wr = data["wins"] / data["count"] if data["count"] else 0
            lines.append(
                f"  {bucket}c: {data['count']} bets, "
                f"{wr:.0%} win, "
                f"{'+'  if data['pnl_cents'] >= 0 else ''}${data['pnl_cents']/100:.2f}"
            )

    return "\n".join(lines)


def get_daily_staked_cents() -> int:
    """Get total amount staked today."""
    db = _get_db()
    today_str = datetime.now(tz=_ET).date().isoformat()
    row = db.execute(
        "SELECT total_staked_cents FROM daily_spend WHERE date = ?",
        (today_str,)
    ).fetchone()
    db.close()
    return row["total_staked_cents"] if row else 0


def get_trade_count_today() -> int:
    """Get number of bets placed today."""
    db = _get_db()
    today_str = datetime.now(tz=_ET).date().isoformat()
    row = db.execute(
        "SELECT bet_count FROM daily_spend WHERE date = ?",
        (today_str,)
    ).fetchone()
    db.close()
    return row["bet_count"] if row else 0


# ─── Paper execution (weather mispricing bot) ─────────────────────────────────

from signals import TradeSignal
from config import SLIPPAGE_CENTS, MAX_RISK_PER_TRADE_PCT


def open_paper_position(
    db: sqlite3.Connection,
    signal: TradeSignal,
    bankroll: float,
) -> int:
    slippage = SLIPPAGE_CENTS / 100.0
    entry_price = signal.market_price + slippage
    entry_price = min(entry_price, 0.99)

    max_risk = bankroll * MAX_RISK_PER_TRADE_PCT
    quantity = max_risk / entry_price if entry_price > 0 else 0
    quantity = max(int(quantity), 1)
    max_loss = quantity * entry_price

    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """
        INSERT INTO paper_positions
            (contract_id, market_id, side, quantity, entry_price, max_loss,
             model_prob, edge, confidence, opened_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            signal.contract_id, signal.market_id, signal.side,
            quantity, entry_price, max_loss,
            signal.model_prob, signal.edge, signal.confidence, now,
        ),
    )
    db.commit()
    return cursor.lastrowid


def settle_paper_position(
    db: sqlite3.Connection,
    position_id: int,
    settlement_price: float,
) -> float:
    row = db.execute(
        "SELECT * FROM paper_positions WHERE id=?", (position_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Position {position_id} not found")

    if row["side"] == "yes":
        pnl = row["quantity"] * (settlement_price - row["entry_price"])
    else:
        pnl = row["quantity"] * ((1.0 - settlement_price) - row["entry_price"])

    pnl = round(pnl, 2)
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        UPDATE paper_positions
        SET status='settled', settlement_price=?, realized_pnl=?, closed_at=?
        WHERE id=?
        """,
        (settlement_price, pnl, now, position_id),
    )
    db.commit()
    return pnl


def get_open_paper_exposure(db: sqlite3.Connection) -> float:
    row = db.execute(
        "SELECT COALESCE(SUM(max_loss), 0) as total FROM paper_positions WHERE status='open'"
    ).fetchone()
    return float(row["total"])


def get_held_contract_ids(db: sqlite3.Connection) -> set[str]:
    rows = db.execute(
        "SELECT contract_id FROM paper_positions WHERE status='open'"
    ).fetchall()
    return {r["contract_id"] for r in rows}


# ─── Risk state management ────────────────────────────────────────────────────

from config import DAILY_MAX_LOSS_PCT, MAX_TRADES_PER_DAY, MAX_CONSECUTIVE_LOSSES


def get_or_create_risk_state(db: sqlite3.Connection, date_str: str, start_balance: float) -> dict:
    row = db.execute("SELECT * FROM risk_state WHERE date=?", (date_str,)).fetchone()
    if row:
        return dict(row)
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO risk_state (date, start_balance, daily_realized_pnl, trades_count,
                                consecutive_losses, day_locked, updated_at)
        VALUES (?, ?, 0.0, 0, 0, 0, ?)
        """,
        (date_str, start_balance, now),
    )
    db.commit()
    return {
        "date": date_str, "start_balance": start_balance,
        "daily_realized_pnl": 0.0, "trades_count": 0,
        "consecutive_losses": 0, "day_locked": 0,
    }


def update_risk_state(db: sqlite3.Connection, date_str: str, trade_pnl: float, was_loss: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if was_loss:
        db.execute(
            """
            UPDATE risk_state
            SET daily_realized_pnl = daily_realized_pnl + ?,
                trades_count = trades_count + 1,
                consecutive_losses = consecutive_losses + 1,
                updated_at = ?
            WHERE date = ?
            """,
            (trade_pnl, now, date_str),
        )
    else:
        db.execute(
            """
            UPDATE risk_state
            SET daily_realized_pnl = daily_realized_pnl + ?,
                trades_count = trades_count + 1,
                consecutive_losses = 0,
                updated_at = ?
            WHERE date = ?
            """,
            (trade_pnl, now, date_str),
        )
    db.commit()


def is_day_locked(db: sqlite3.Connection, date_str: str, start_balance: float) -> bool:
    state = get_or_create_risk_state(db, date_str, start_balance)
    max_loss = start_balance * DAILY_MAX_LOSS_PCT
    if abs(state["daily_realized_pnl"]) >= max_loss and state["daily_realized_pnl"] < 0:
        return True
    if state["trades_count"] >= MAX_TRADES_PER_DAY:
        return True
    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        return True
    if state["day_locked"]:
        return True
    return False
