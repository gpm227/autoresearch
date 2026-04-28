#!/usr/bin/env python3
"""
bot.py — Entry point for the Kalshi weather mispricing bot.

Runs a scan/price/paper-trade loop every ~100 seconds.
Denver high-temp contracts only. Paper mode until operator enables live.

Usage:
    uv run bot.py --demo      # paper trading on Kalshi demo environment
    uv run bot.py --live      # real trades on Kalshi production
    uv run bot.py --status    # print bankroll, open positions, exchange status
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timezone

from infra import Env, KalshiClient
from execution import (
    _get_db, _migrate_market_observations,
    open_paper_position, get_open_paper_exposure, get_held_contract_ids,
    get_or_create_risk_state, update_risk_state, is_day_locked,
)
from scanner import scan_weather_pipeline
from config import (
    INITIAL_BANKROLL, SCAN_INTERVAL_SEC, CITY_TIMEZONE,
)
from pnl import compute_daily_summary, compute_lifetime_summary

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

PAUSED_FILE = os.path.join(os.path.dirname(__file__), "PAUSED")


def _is_paused() -> bool:
    return os.path.exists(PAUSED_FILE)


def _today_str() -> str:
    tz = ZoneInfo(CITY_TIMEZONE)
    return datetime.now(tz).strftime("%Y-%m-%d")


def _tick(client: KalshiClient) -> None:
    """
    One scan cycle:
    1. Check PAUSED / risk locks
    2. Run weather pipeline (scan → parse → model → signal)
    3. Paper-execute any signals
    4. Print console summary
    """
    db = _get_db()
    _migrate_market_observations(db)

    today = _today_str()
    bankroll = INITIAL_BANKROLL  # TODO: compute from paper positions once we have history

    # Risk state
    risk_state = get_or_create_risk_state(db, today, start_balance=bankroll)
    day_locked = _is_paused() or is_day_locked(db, today, start_balance=bankroll)

    # Current exposure
    open_exposure = get_open_paper_exposure(db)
    held_contracts = get_held_contract_ids(db)

    # Run pipeline
    signals = scan_weather_pipeline(
        client=client,
        db=db,
        bankroll=bankroll,
        held_contracts=held_contracts,
        open_exposure=open_exposure,
        day_locked=day_locked,
    )

    # Paper execute signals
    for sig in signals:
        if day_locked:
            break
        pos_id = open_paper_position(db, sig, bankroll=bankroll)
        log.info("PAPER TRADE opened: %s %s @ %.2f (pos_id=%d)",
                 sig.contract_id, sig.side, sig.market_price, pos_id)

    # Console summary
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    risk_state = get_or_create_risk_state(db, today, start_balance=bankroll)
    open_exposure = get_open_paper_exposure(db)
    open_count = db.execute(
        "SELECT COUNT(*) as c FROM paper_positions WHERE status='open'"
    ).fetchone()["c"]

    print(f"\n--- scan {now_utc} ---")
    print(f"  signals={len(signals)}")
    print(f"  paper_positions_open={open_count}")
    print(f"  open_exposure=${open_exposure:.2f}")
    print(f"  daily_pnl=${risk_state['daily_realized_pnl']:.2f}")
    print(f"  trades_today={risk_state['trades_count']}")
    print(f"  day_locked={'YES' if day_locked else 'no'}")
    print(f"  paused={'YES' if _is_paused() else 'no'}")

    db.close()


def run_loop(env: Env) -> None:
    client = KalshiClient(env=env)
    log.info("Loop started (env=%s, interval=%ds)", env.name, SCAN_INTERVAL_SEC)

    while True:
        try:
            _tick(client)
            time.sleep(SCAN_INTERVAL_SEC)
        except KeyboardInterrupt:
            log.info("Interrupted — shutting down cleanly")
            break
        except Exception as e:
            log.exception("Tick error: %s", e)
            time.sleep(60)


def cmd_demo() -> None:
    log.info("Starting DEMO session (paper trading)")
    run_loop(env=Env.DEMO)


def cmd_live(skip_confirm: bool = False) -> None:
    log.info("Starting LIVE session")
    if not skip_confirm:
        print("=" * 60)
        print("WARNING: This will place REAL trades with REAL money.")
        print("=" * 60)
        confirm = input("Type 'YES I AM SURE' to proceed: ")
        if confirm.strip() != "YES I AM SURE":
            print("Aborted.")
            sys.exit(0)
    run_loop(env=Env.LIVE)


def cmd_status() -> None:
    client = KalshiClient(env=Env.LIVE)
    try:
        balance_resp = client.get_balance()
        balance = balance_resp.get("balance", 0) / 100.0

        positions_resp = client.get_positions()
        positions = positions_resp.get("market_positions", [])
        open_positions = [p for p in positions if p.get("position", 0) != 0]

        exchange_status = client.get_exchange_status()
        trading_active = exchange_status.get("trading_active", False)

        print(f"Balance:         ${balance:.2f}")
        print(f"Open positions:  {len(open_positions)}")
        print(f"Exchange active: {trading_active}")

        # Paper positions
        db = _get_db()
        _migrate_market_observations(db)
        open_paper = db.execute(
            "SELECT COUNT(*) as c FROM paper_positions WHERE status='open'"
        ).fetchone()["c"]
        exposure = get_open_paper_exposure(db)
        print(f"Paper positions: {open_paper}")
        print(f"Paper exposure:  ${exposure:.2f}")
        db.close()

    except Exception as e:
        print(f"Status check failed: {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kalshi Weather Mispricing Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", action="store_true",
                       help="Paper trading on Kalshi demo environment")
    group.add_argument("--live", action="store_true",
                       help="Real trading on Kalshi production (real money)")
    group.add_argument("--status", action="store_true",
                       help="Print current bankroll, positions, and exchange status")
    parser.add_argument("--yes", action="store_true",
                        help="Skip interactive confirmation (for headless/systemd)")
    args = parser.parse_args()

    if args.demo:
        cmd_demo()
    elif args.live:
        cmd_live(skip_confirm=args.yes)
    elif args.status:
        cmd_status()


if __name__ == "__main__":
    main()
