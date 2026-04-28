#!/usr/bin/env python3
"""
btc_bot.py — Kalshi BTC 15-minute up/down trading bot.

Runs a fast loop (every 30s):
1. Get live BTC price from Binance websocket
2. Scan Kalshi for active 15-min BTC markets
3. Price each market with log-normal model
4. Paper trade where edge exceeds threshold

Usage:
    uv run btc_bot.py --live --yes                  # paper trade live BTC 15m markets
    uv run btc_bot.py --live --status               # print 48h paper warmup status
    KALSHI_BTC_ENABLE_REAL_ORDERS=true \
      uv run btc_bot.py --live --yes --execute-live-orders
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from infra import Env, KalshiClient
from btc_feed import BTCFeed, FeedHealth
from btc_scanner import scan_btc_markets, BTCMarket, is_btc15_market
import btc_model
from btc_model import price_btc_contract, BTCEstimate
from btc_simulator import BTC_RESULTS_TSV, report_to_json, run_btc_simulation
from execution import _get_db
from config import (
    BTC_SCAN_INTERVAL_SEC,
    BTC_PAPER_WARMUP_HOURS,
)
from strategy import (
    BTCEvaluationInput,
    BTCStrategy,
    DEFAULT_BTC_STRATEGY,
    evaluate_btc_signal,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("btc_bot")


PAUSED_FILE = os.path.join(os.path.dirname(__file__), "PAUSED")
LIVE_ACK_ENV = "KALSHI_BTC_ENABLE_REAL_ORDERS"


@dataclass(frozen=True)
class BTCExecutionConfig:
    env: Env
    live_orders_requested: bool = False


@dataclass(frozen=True)
class BTCWarmupStatus:
    started_at: datetime
    elapsed: timedelta
    required: timedelta

    @property
    def complete(self) -> bool:
        return self.elapsed >= self.required

    @property
    def hours_elapsed(self) -> float:
        return self.elapsed.total_seconds() / 3600.0

    @property
    def hours_required(self) -> float:
        return self.required.total_seconds() / 3600.0


@dataclass(frozen=True)
class BTCLiveGate:
    allowed: bool
    reason: str
    warmup: BTCWarmupStatus


# ─── BTC risk state ─────────────────────────────────────────────────────────

def _ensure_btc_tables(db: sqlite3.Connection) -> None:
    """Create BTC-specific tables if they don't exist."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS btc_paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            max_loss REAL NOT NULL,
            model_prob REAL,
            edge REAL,
            confidence REAL,
            btc_price REAL,
            target_price REAL,
            realized_vol REAL,
            seconds_remaining REAL,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            settlement_price REAL,
            realized_pnl REAL,
            status TEXT NOT NULL DEFAULT 'open'
        );

        CREATE TABLE IF NOT EXISTS btc_daily_state (
            date TEXT PRIMARY KEY,
            trades_count INTEGER NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0.0,
            day_locked INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS btc_runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS btc_market_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluated_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT,
            skip_reason TEXT,
            btc_price REAL NOT NULL,
            target_price REAL NOT NULL,
            seconds_remaining REAL NOT NULL,
            model_prob REAL NOT NULL,
            confidence REAL NOT NULL,
            yes_bid REAL,
            yes_ask REAL,
            no_bid REAL,
            no_ask REAL,
            edge_yes REAL,
            edge_no REAL,
            kalshi_spread REAL,
            feed_health TEXT NOT NULL,
            venue_spread_bps REAL,
            paper_trade_opened INTEGER NOT NULL DEFAULT 0,
            live_order_attempted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS btc_live_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_position_id INTEGER,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price_cents INTEGER NOT NULL,
            max_cost_cents INTEGER NOT NULL,
            order_id TEXT,
            response_json TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    db.commit()


def _get_btc_daily_state(db: sqlite3.Connection, today: str) -> dict:
    """Get or create today's BTC risk state."""
    _ensure_btc_tables(db)
    row = db.execute(
        "SELECT * FROM btc_daily_state WHERE date=?", (today,)
    ).fetchone()
    if row:
        return dict(row)
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO btc_daily_state (date, trades_count, realized_pnl, day_locked, updated_at) VALUES (?, 0, 0.0, 0, ?)",
        (today, now),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM btc_daily_state WHERE date=?", (today,)
    ).fetchone()
    return dict(row)


def _state_get(db: sqlite3.Connection, key: str) -> Optional[str]:
    row = db.execute(
        "SELECT value FROM btc_runtime_state WHERE key=?", (key,)
    ).fetchone()
    return str(row["value"]) if row else None


def _state_set(db: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    db.execute(
        """
        INSERT INTO btc_runtime_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, now.isoformat()),
    )
    db.commit()


def _parse_state_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _get_or_start_btc_warmup(
    db: sqlite3.Connection,
    now: Optional[datetime] = None,
) -> BTCWarmupStatus:
    """Start or read the persistent 48-hour paper-trading warmup clock."""
    _ensure_btc_tables(db)
    current = now or datetime.now(timezone.utc)
    raw_started = _state_get(db, "paper_warmup_started_at")
    if raw_started:
        started_at = _parse_state_datetime(raw_started)
    else:
        started_at = current
        _state_set(db, "paper_warmup_started_at", started_at.isoformat(), current)
    return BTCWarmupStatus(
        started_at=started_at,
        elapsed=max(current - started_at, timedelta(0)),
        required=timedelta(hours=BTC_PAPER_WARMUP_HOURS),
    )


def _get_btc_warmup_status(
    db: sqlite3.Connection,
    now: Optional[datetime] = None,
) -> Optional[BTCWarmupStatus]:
    """Read warmup status without starting the clock."""
    _ensure_btc_tables(db)
    raw_started = _state_get(db, "paper_warmup_started_at")
    if not raw_started:
        return None
    current = now or datetime.now(timezone.utc)
    started_at = _parse_state_datetime(raw_started)
    return BTCWarmupStatus(
        started_at=started_at,
        elapsed=max(current - started_at, timedelta(0)),
        required=timedelta(hours=BTC_PAPER_WARMUP_HOURS),
    )


def _live_gate(
    db: sqlite3.Connection,
    execution: BTCExecutionConfig,
    now: Optional[datetime] = None,
) -> BTCLiveGate:
    """Return whether a real order may be placed this tick."""
    warmup = _get_or_start_btc_warmup(db, now=now)
    if not execution.live_orders_requested:
        return BTCLiveGate(False, "paper mode: live orders not requested", warmup)
    if execution.env != Env.LIVE:
        return BTCLiveGate(False, "live orders require --live", warmup)
    if os.path.exists(PAUSED_FILE):
        return BTCLiveGate(False, "PAUSED file present", warmup)
    if os.environ.get(LIVE_ACK_ENV, "").strip().lower() != "true":
        return BTCLiveGate(
            False,
            f"{LIVE_ACK_ENV}=true is required for real orders",
            warmup,
        )
    if not warmup.complete:
        return BTCLiveGate(
            False,
            (
                f"paper warmup incomplete: "
                f"{warmup.hours_elapsed:.1f}/{warmup.hours_required:.1f}h"
            ),
            warmup,
        )
    return BTCLiveGate(True, "live order gate passed", warmup)


def _record_btc_paper_trade(
    db: sqlite3.Connection,
    market: BTCMarket,
    estimate: BTCEstimate,
    side: str,
    edge: float,
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
) -> int:
    """Record a BTC paper trade."""
    _ensure_btc_tables(db)

    entry_price = market.yes_ask if side == "yes" else market.no_ask
    if entry_price is None:
        return -1

    # Size: fixed dollar risk per trade
    quantity = _btc_trade_quantity(entry_price, strategy=strategy)
    max_loss = quantity * entry_price

    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """
        INSERT INTO btc_paper_positions
            (contract_id, ticker, side, quantity, entry_price, max_loss,
             model_prob, edge, confidence, btc_price, target_price,
             realized_vol, seconds_remaining, opened_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            market.ticker, market.ticker, side, quantity, entry_price, max_loss,
            estimate.prob_yes, edge, estimate.confidence,
            estimate.current_price, estimate.target_price,
            estimate.realized_vol, estimate.seconds_remaining,
            now,
        ),
    )
    db.commit()

    # Update daily state
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.execute(
        """
        UPDATE btc_daily_state
        SET trades_count = trades_count + 1, updated_at = ?
        WHERE date = ?
        """,
        (now, today),
    )
    db.commit()
    return cursor.lastrowid


def _btc_trade_quantity(
    entry_price: float,
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
) -> int:
    if entry_price <= 0:
        return 0
    return strategy.contracts_for_entry_price(entry_price)


def _record_btc_evaluation(
    db: sqlite3.Connection,
    market: BTCMarket,
    estimate: BTCEstimate,
    side: Optional[str],
    skip_reason: str,
    edge_yes: Optional[float],
    edge_no: Optional[float],
    feed_health: FeedHealth,
    venue_spread_bps: Optional[float],
    paper_trade_opened: bool = False,
    live_order_attempted: bool = False,
) -> None:
    _ensure_btc_tables(db)
    db.execute(
        """
        INSERT INTO btc_market_evaluations
            (evaluated_at, ticker, side, skip_reason, btc_price, target_price,
             seconds_remaining, model_prob, confidence, yes_bid, yes_ask,
             no_bid, no_ask, edge_yes, edge_no, kalshi_spread, feed_health,
             venue_spread_bps, paper_trade_opened, live_order_attempted)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            market.ticker,
            side,
            skip_reason,
            estimate.current_price,
            estimate.target_price,
            estimate.seconds_remaining,
            estimate.prob_yes,
            estimate.confidence,
            market.yes_bid,
            market.yes_ask,
            market.no_bid,
            market.no_ask,
            edge_yes,
            edge_no,
            market.spread,
            feed_health.value,
            venue_spread_bps,
            1 if paper_trade_opened else 0,
            1 if live_order_attempted else 0,
        ),
    )
    db.commit()


def _record_btc_live_order(
    db: sqlite3.Connection,
    paper_position_id: int,
    market: BTCMarket,
    side: str,
    quantity: int,
    price_cents: int,
    response: dict,
    status: str,
) -> int:
    _ensure_btc_tables(db)
    order = response.get("order", response) if isinstance(response, dict) else {}
    order_id = order.get("order_id") or order.get("id")
    cursor = db.execute(
        """
        INSERT INTO btc_live_orders
            (paper_position_id, ticker, side, quantity, price_cents,
             max_cost_cents, order_id, response_json, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            paper_position_id,
            market.ticker,
            side,
            quantity,
            price_cents,
            quantity * price_cents,
            order_id,
            json.dumps(response, sort_keys=True),
            status,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()
    return cursor.lastrowid


def _maybe_place_live_order(
    db: sqlite3.Connection,
    client: KalshiClient,
    market: BTCMarket,
    side: str,
    paper_position_id: int,
    gate: BTCLiveGate,
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
) -> bool:
    """Place a real Kalshi order only after all explicit live gates pass."""
    if not gate.allowed:
        log.info("LIVE ORDER BLOCKED: %s", gate.reason)
        return False

    entry_price = market.yes_ask if side == "yes" else market.no_ask
    if entry_price is None:
        log.info("LIVE ORDER BLOCKED: missing %s ask", side)
        return False

    quantity = _btc_trade_quantity(entry_price, strategy=strategy)
    if quantity <= 0:
        log.info("LIVE ORDER BLOCKED: invalid quantity")
        return False

    price_cents = int(round(entry_price * 100))
    try:
        response = client.create_order(
            ticker=market.ticker,
            side=side,
            contracts=quantity,
            price_cents=price_cents,
        )
        _record_btc_live_order(
            db=db,
            paper_position_id=paper_position_id,
            market=market,
            side=side,
            quantity=quantity,
            price_cents=price_cents,
            response=response,
            status="submitted",
        )
        log.warning(
            "BTC LIVE ORDER SUBMITTED: %s %s qty=%d price=%dc",
            market.ticker, side.upper(), quantity, price_cents,
        )
        return True
    except Exception as e:
        _record_btc_live_order(
            db=db,
            paper_position_id=paper_position_id,
            market=market,
            side=side,
            quantity=quantity,
            price_cents=price_cents,
            response={"error": str(e)},
            status="error",
        )
        log.exception("BTC live order failed: %s", e)
        return True


# ─── Signal generation ───────────────────────────────────────────────────────

def _generate_btc_signal(
    market: BTCMarket,
    estimate: BTCEstimate,
    held_tickers: set[str],
    daily_state: dict,
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
    feed_health: FeedHealth = FeedHealth.LIVE,
    venue_spread_bps: Optional[float] = None,
) -> tuple[str | None, float, str]:
    return evaluate_btc_signal(
        BTCEvaluationInput(
            ticker=market.ticker,
            prob_yes=estimate.prob_yes,
            confidence=estimate.confidence,
            yes_ask=market.yes_ask,
            no_ask=market.no_ask,
            spread=market.spread,
            seconds_remaining=market.seconds_remaining,
            feed_health=feed_health.value,
            venue_spread_bps=venue_spread_bps,
            already_held=market.ticker in held_tickers,
            trades_today=int(daily_state.get("trades_count", 0)),
            realized_pnl_today=float(daily_state.get("realized_pnl", 0.0)),
            day_locked=bool(daily_state.get("day_locked", 0)),
        ),
        strategy=strategy,
    )


# ─── Settlement ──────────────────────────────────────────────────────────────

def _settle_open_positions(db: sqlite3.Connection, client: KalshiClient) -> int:
    """
    Check all open BTC paper positions. If the market has settled on Kalshi,
    compute P&L and close the position.

    Returns number of positions settled this tick.
    """
    rows = db.execute(
        "SELECT * FROM btc_paper_positions WHERE status='open'"
    ).fetchall()
    if not rows:
        return 0

    settled = 0
    for row in rows:
        pos = dict(row)
        ticker = pos["ticker"]

        try:
            resp = client._request("GET", f"/markets/{ticker}")
            # Kalshi wraps single-market responses in {"market": {...}}
            market = resp.get("market", resp)
        except Exception as e:
            log.debug("Could not fetch market %s for settlement: %s", ticker, e)
            continue

        result = market.get("result", "")
        if not result:
            continue  # not settled yet

        # result is "yes" or "no"
        side = pos["side"]
        entry_price = pos["entry_price"]
        quantity = pos["quantity"]

        if result == side:
            # Won: payout is $1 per contract, paid entry_price
            pnl = quantity * (1.0 - entry_price)
        else:
            # Lost: lose the entry cost
            pnl = -quantity * entry_price

        now = datetime.now(timezone.utc).isoformat()
        settlement_price = 1.0 if result == "yes" else 0.0

        db.execute(
            """
            UPDATE btc_paper_positions
            SET status='settled', realized_pnl=?, settlement_price=?, closed_at=?
            WHERE id=?
            """,
            (pnl, settlement_price, now, pos["id"]),
        )

        # Update daily P&L
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.execute(
            "UPDATE btc_daily_state SET realized_pnl = realized_pnl + ?, updated_at = ? WHERE date = ?",
            (pnl, now, today),
        )

        settled += 1
        outcome = "WIN" if pnl > 0 else "LOSS"
        log.info(
            "SETTLED %s: %s %s → result=%s %s $%.2f (entry=%.2f qty=%d)",
            ticker, side.upper(), outcome, result, "+" if pnl > 0 else "",
            pnl, entry_price, quantity,
        )

    if settled:
        db.commit()
    return settled


# ─── Main tick ───────────────────────────────────────────────────────────────

def _tick(
    client: KalshiClient,
    feed: BTCFeed,
    execution: BTCExecutionConfig,
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
) -> None:
    """One scan cycle for BTC 15-min markets."""
    db = _get_db()
    _ensure_btc_tables(db)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_state = _get_btc_daily_state(db, today)

    # Settle any expired positions
    settled = _settle_open_positions(db, client)
    if settled:
        daily_state = _get_btc_daily_state(db, today)

    # Check feed health
    feed_state = feed.health
    price = feed.fair_price
    spread_bps = feed.spread_bps

    if price is None:
        log.warning("No BTC price available — skipping tick")
        db.close()
        return

    if feed_state == FeedHealth.STALE:
        log.warning("Feed STALE (age=%.1fs) — skipping tick", feed.price_age_sec)
        db.close()
        return

    vol, vol_count = feed.realized_vol(strategy=strategy)
    if vol == 0:
        log.warning("No vol data yet (samples=%d) — skipping tick", vol_count)
        db.close()
        return

    gate = _live_gate(db, execution)

    # Log feed snapshot
    snap = feed.snapshot()
    log.info(
        "FEED %s | fair=$%s mid=$%s last=$%s | spread=%.1fbps age=%.1fs | candles=%d | warmup=%.1f/%.1fh live=%s",
        snap["health"],
        f"{snap['fair']:,.2f}" if snap["fair"] else "?",
        f"{snap['mid']:,.2f}" if snap["mid"] else "?",
        f"{snap['last_trade']:,.2f}" if snap["last_trade"] else "?",
        snap["spread_bps"] or 0,
        snap["age_sec"] or 0,
        snap["candle_count"],
        gate.warmup.hours_elapsed,
        gate.warmup.hours_required,
        "allowed" if gate.allowed else gate.reason,
    )

    # Get held positions
    held = set()
    rows = db.execute(
        "SELECT ticker FROM btc_paper_positions WHERE status='open'"
    ).fetchall()
    for r in rows:
        held.add(r["ticker"])

    # Scan Kalshi for BTC markets
    markets = scan_btc_markets(client)
    if not markets:
        log.info("No BTC 15-min markets found")
        db.close()
        return

    signals_found = 0
    trades_opened = 0
    recent_closes = feed.recent_closes()

    for market in markets:
        # Distance filter for 15-min markets. We still price/log the market,
        # but do not trade when spot is too far from target.
        distance = abs(price - market.target_price) / market.target_price if market.target_price else 999
        too_far_from_target = (
            is_btc15_market({"ticker": market.ticker})
            and distance > strategy.max_target_distance
        )

        # Price with model (V2: adaptive vol + momentum drift)
        estimate = price_btc_contract(
            contract_id=market.ticker,
            current_price=price,
            target_price=market.target_price,
            seconds_remaining=market.seconds_remaining,
            realized_vol_annual=vol,
            vol_sample_count=vol_count,
            recent_closes=recent_closes,
            strategy=strategy,
        )

        # Compute edge for logging (even if we don't trade)
        edge_yes = estimate.prob_yes - market.yes_ask if market.yes_ask else None
        edge_no = (1.0 - estimate.prob_yes) - market.no_ask if market.no_ask else None

        # Check for signal — enforce feed health + spread gates
        if too_far_from_target:
            side, edge, skip_reason = None, 0.0, f"FAR_FROM_TARGET({distance:.4f})"
        else:
            side, edge, skip_reason = _generate_btc_signal(
                market, estimate, held, daily_state,
                strategy=strategy,
                feed_health=feed_state,
                venue_spread_bps=spread_bps,
            )

        log.info(
            "EVAL %s | btc=$%.2f strike=$%.2f t=%ds dist=%.4f "
            "vol=%.0f%%(raw=%.0f%%) drift=%.1f%% | "
            "model=%.3f edge_y=%s edge_n=%s spread=%s | %s",
            market.ticker, price, market.target_price,
            int(market.seconds_remaining), distance,
            estimate.realized_vol * 100, estimate.raw_vol * 100,
            estimate.drift * 100 / (365.25 * 24 * 60) * market.seconds_remaining / 60 if estimate.drift else 0,
            estimate.prob_yes,
            f"{edge_yes:+.3f}" if edge_yes is not None else "?",
            f"{edge_no:+.3f}" if edge_no is not None else "?",
            f"{market.spread:.3f}" if market.spread else "None",
            f"SIGNAL:{side.upper()} edge={edge:.3f}" if side else f"SKIP:{skip_reason}",
        )

        paper_opened = False
        live_attempted = False

        if side is None:
            _record_btc_evaluation(
                db=db,
                market=market,
                estimate=estimate,
                side=None,
                skip_reason=skip_reason,
                edge_yes=edge_yes,
                edge_no=edge_no,
                feed_health=feed_state,
                venue_spread_bps=spread_bps,
            )
            continue

        signals_found += 1

        # Paper trade
        pos_id = _record_btc_paper_trade(
            db,
            market,
            estimate,
            side,
            edge,
            strategy=strategy,
        )
        if pos_id > 0:
            paper_opened = True
            trades_opened += 1
            log.info(
                "BTC PAPER TRADE: %s %s | btc=$%.2f target=$%.2f | "
                "model=%.3f market=%.3f edge=%.3f | vol=%.1f%% t=%ds",
                market.ticker, side.upper(),
                price, market.target_price,
                estimate.prob_yes,
                market.yes_ask if side == "yes" else market.no_ask,
                edge,
                vol * 100, int(market.seconds_remaining),
            )
            # Refresh daily state after trade
            daily_state = _get_btc_daily_state(db, today)
            live_attempted = _maybe_place_live_order(
                db=db,
                client=client,
                market=market,
                side=side,
                paper_position_id=pos_id,
                gate=gate,
                strategy=strategy,
            )

        _record_btc_evaluation(
            db=db,
            market=market,
            estimate=estimate,
            side=side,
            skip_reason="SIGNAL",
            edge_yes=edge_yes,
            edge_no=edge_no,
            feed_health=feed_state,
            venue_spread_bps=spread_bps,
            paper_trade_opened=paper_opened,
            live_order_attempted=live_attempted,
        )

    # Console summary
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    open_count = db.execute(
        "SELECT COUNT(*) as c FROM btc_paper_positions WHERE status='open'"
    ).fetchone()["c"]
    lifetime = db.execute(
        """SELECT
            COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(realized_pnl), 0) as total_pnl
        FROM btc_paper_positions WHERE status='settled'"""
    ).fetchone()

    log.info(
        "SUMMARY | btc=$%s vol=%.0f%%(floor=%.0f%%) feed=%s | "
        "mkt=%d sig=%d trade=%d open=%d | "
        "today: %d trades $%.2f | lifetime: %d-%d (W-L) $%.2f",
        f"{price:,.2f}", vol * 100, btc_model.VOL_FLOOR_ANNUAL * 100,
        snap["health"],
        len(markets), signals_found, trades_opened, open_count,
        daily_state["trades_count"], daily_state["realized_pnl"],
        lifetime["wins"] or 0, lifetime["losses"] or 0, lifetime["total_pnl"],
    )

    db.close()


# ─── Entry point ─────────────────────────────────────────────────────────────

def run_loop(
    env: Env,
    live_orders_requested: bool = False,
    max_ticks: Optional[int] = None,
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
) -> None:
    execution = BTCExecutionConfig(
        env=env,
        live_orders_requested=live_orders_requested,
    )
    client = KalshiClient(env=env)

    # Start price feed
    feed = BTCFeed()
    log.info("Seeding BTC price history from Binance REST...")
    feed.seed_from_rest()
    feed.start()

    # Wait for price to be available (seed should have set it already)
    log.info("Waiting for price... (seed last_trade=%s)", feed._last_trade)
    for i in range(30):
        fp = feed.fair_price
        if fp is not None:
            log.info("Price available after %ds: $%.2f", i, fp)
            break
        if i % 5 == 0:
            log.info("Still waiting... fair=%s last_trade=%s bid=%s ask=%s age=%.1f",
                     feed.fair_price, feed._last_trade, feed._best_bid, feed._best_ask, feed.price_age_sec)
        time.sleep(1)

    if feed.fair_price is None:
        log.error("Could not get initial BTC price after 30s — check network")
        feed.stop()
        return

    snap = feed.snapshot()
    log.info(
        "Feed ready: fair=$%.2f health=%s spread=%.1fbps candles=%d. Loop interval=%ds.",
        snap["fair"] or 0, snap["health"],
        snap["spread_bps"] or 0, snap["candle_count"],
        BTC_SCAN_INTERVAL_SEC,
    )

    try:
        ticks = 0
        while True:
            try:
                _tick(client, feed, execution, strategy=strategy)
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    log.info("Max ticks reached (%d); exiting", max_ticks)
                    return
                time.sleep(BTC_SCAN_INTERVAL_SEC)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.exception("Tick error: %s", e)
                time.sleep(30)
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        feed.stop()


def print_status() -> None:
    """Print paper warmup and BTC paper-trading stats."""
    db = _get_db()
    _ensure_btc_tables(db)
    warmup = _get_btc_warmup_status(db)

    settled = db.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(realized_pnl), 0) AS pnl
        FROM btc_paper_positions
        WHERE status='settled'
        """
    ).fetchone()
    open_count = db.execute(
        "SELECT COUNT(*) AS c FROM btc_paper_positions WHERE status='open'"
    ).fetchone()["c"]
    evaluations = db.execute(
        "SELECT COUNT(*) AS c FROM btc_market_evaluations"
    ).fetchone()["c"]
    signals = db.execute(
        "SELECT COUNT(*) AS c FROM btc_market_evaluations WHERE side IS NOT NULL"
    ).fetchone()["c"]
    live_orders = db.execute(
        "SELECT COUNT(*) AS c FROM btc_live_orders"
    ).fetchone()["c"]

    if warmup is None:
        print("paper_warmup_started_at=not_started")
        print(f"paper_warmup_elapsed=0.00h/{BTC_PAPER_WARMUP_HOURS:.2f}h complete=False")
    else:
        print(f"paper_warmup_started_at={warmup.started_at.isoformat()}")
        print(
            f"paper_warmup_elapsed={warmup.hours_elapsed:.2f}h/"
            f"{warmup.hours_required:.2f}h complete={warmup.complete}"
        )
    print(f"evaluations={evaluations} signals={signals}")
    print(f"paper_open_positions={open_count}")
    print(
        "paper_settled="
        f"{settled['total'] or 0} wins={settled['wins'] or 0} "
        f"losses={settled['losses'] or 0} pnl=${settled['pnl'] or 0:.2f}"
    )
    print(f"live_orders_recorded={live_orders}")
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kalshi BTC 15-Minute Trading Bot",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument(
        "--simulate",
        action="store_true",
        help="Replay logged BTC evaluations and score the current strategy",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Print BTC paper status and exit",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation")
    parser.add_argument("--max-ticks", type=int, default=None, help="Run N ticks, then exit")
    parser.add_argument(
        "--label",
        default=None,
        help="Optional experiment label to append to btc_experiments.tsv during --simulate",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print simulation report as JSON during --simulate",
    )
    parser.add_argument(
        "--execute-live-orders",
        action="store_true",
        help=(
            "Allow real orders only after the 48h paper warmup and "
            f"{LIVE_ACK_ENV}=true. Without this flag, --live is still paper."
        ),
    )
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    if args.simulate:
        report = run_btc_simulation(
            strategy=DEFAULT_BTC_STRATEGY,
            experiment_label=args.label,
            write_results=True,
        )
        if args.json:
            print(report_to_json(report))
        else:
            print(f"strategy_version={report.strategy.get('strategy_version')}")
            print(
                "overall "
                f"rows={report.overall.evaluated_rows} resolved={report.overall.resolved_rows} "
                f"trades={report.overall.trades} win_rate={report.overall.win_rate:.2%} "
                f"net_pnl=${report.overall.net_pnl:.2f} sharpe={report.overall.sharpe:.2f} "
                f"max_drawdown=${report.overall.max_drawdown:.2f} "
                f"trades_per_day={report.overall.trades_per_day:.2f} "
                f"unresolved={report.overall.unresolved}"
            )
            print(
                "train "
                f"rows={report.train.evaluated_rows} trades={report.train.trades} "
                f"win_rate={report.train.win_rate:.2%} net_pnl=${report.train.net_pnl:.2f}"
            )
            print(
                "validation "
                f"rows={report.validation.evaluated_rows} trades={report.validation.trades} "
                f"win_rate={report.validation.win_rate:.2%} "
                f"net_pnl=${report.validation.net_pnl:.2f}"
            )
            print(f"results_tsv={BTC_RESULTS_TSV}")
            if args.label:
                print(f"experiment_logged={args.label}")
        return

    env = Env.LIVE if args.live else Env.DEMO

    if args.execute_live_orders and not args.live:
        parser.error("--execute-live-orders requires --live")

    if args.execute_live_orders and not args.yes:
        print("=" * 60)
        print("BTC Bot — REAL ORDER MODE REQUESTED")
        print("=" * 60)
        print(f"Real orders still require 48h paper warmup and {LIVE_ACK_ENV}=true.")
        confirm = input("Type 'YES LIVE BTC' to proceed: ")
        if confirm.strip() != "YES LIVE BTC":
            print("Aborted.")
            sys.exit(0)
    elif args.live and not args.yes:
        print("=" * 60)
        print("BTC Bot — LIVE environment (paper trades only)")
        print("=" * 60)
        confirm = input("Type 'YES' to proceed: ")
        if confirm.strip() != "YES":
            print("Aborted.")
            sys.exit(0)

    run_loop(
        env,
        live_orders_requested=args.execute_live_orders,
        max_ticks=args.max_ticks,
        strategy=DEFAULT_BTC_STRATEGY,
    )


if __name__ == "__main__":
    main()
