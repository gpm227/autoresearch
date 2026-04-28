import pytest
from pnl import compute_daily_summary, compute_lifetime_summary


def test_daily_summary_basic():
    trades = [
        {"realized_pnl": 5.00, "status": "settled", "side": "yes"},
        {"realized_pnl": -3.00, "status": "settled", "side": "no"},
        {"realized_pnl": 2.00, "status": "settled", "side": "yes"},
    ]
    s = compute_daily_summary(trades, start_balance=340.00, end_balance=344.00)
    assert s["realized_pnl"] == 4.00
    assert s["trades_count"] == 3
    assert s["wins"] == 2
    assert s["losses"] == 1


def test_daily_summary_no_trades():
    s = compute_daily_summary([], start_balance=340.00, end_balance=340.00)
    assert s["realized_pnl"] == 0.0
    assert s["trades_count"] == 0
    assert s["wins"] == 0
    assert s["losses"] == 0


def test_lifetime_summary():
    s = compute_lifetime_summary(
        total_trades=10,
        wins=7,
        current_balance=360.00,
        peak_balance=365.00,
        initial_balance=340.00,
    )
    assert s["aggregate_pnl"] == 20.00
    assert abs(s["aggregate_return_pct"] - 5.88) < 0.1
    assert s["win_rate"] == 70.0
    assert abs(s["max_drawdown_pct"] - 1.37) < 0.1


def test_lifetime_summary_no_trades():
    s = compute_lifetime_summary(
        total_trades=0,
        wins=0,
        current_balance=340.00,
        peak_balance=340.00,
        initial_balance=340.00,
    )
    assert s["win_rate"] == 0.0
    assert s["max_drawdown_pct"] == 0.0


def test_daily_summary_drawdown():
    trades = [
        {"realized_pnl": -10.00, "status": "settled", "side": "yes"},
        {"realized_pnl": -5.00, "status": "settled", "side": "no"},
        {"realized_pnl": 3.00, "status": "settled", "side": "yes"},
    ]
    s = compute_daily_summary(trades, start_balance=340.00, end_balance=328.00)
    assert s["losses"] == 2
    assert s["wins"] == 1
    assert s["max_drawdown"] == 15.00
