import pytest
import sqlite3
from execution import (
    _create_tables, _migrate_market_observations,
    get_or_create_risk_state, update_risk_state, is_day_locked,
)


def _make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _create_tables(db)
    _migrate_market_observations(db)
    return db


def test_create_risk_state():
    db = _make_db()
    state = get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    assert state["start_balance"] == 340.00
    assert state["daily_realized_pnl"] == 0.0
    assert state["trades_count"] == 0
    assert state["day_locked"] == 0


def test_update_risk_state_after_trade():
    db = _make_db()
    get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)
    state = get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    assert state["daily_realized_pnl"] == -5.00
    assert state["trades_count"] == 1
    assert state["consecutive_losses"] == 1


def test_day_locks_on_max_loss():
    db = _make_db()
    get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    for _ in range(10):
        update_risk_state(db, "2026-04-10", trade_pnl=-8.00, was_loss=True)
    assert is_day_locked(db, "2026-04-10", start_balance=340.00) is True


def test_day_not_locked_under_limit():
    db = _make_db()
    get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)
    assert is_day_locked(db, "2026-04-10", start_balance=340.00) is False


def test_consecutive_losses_reset_on_win():
    db = _make_db()
    get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)
    update_risk_state(db, "2026-04-10", trade_pnl=3.00, was_loss=False)
    state = get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    assert state["consecutive_losses"] == 0
