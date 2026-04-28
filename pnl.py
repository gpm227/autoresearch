"""
pnl.py — Daily and lifetime P&L tracking.
"""
from __future__ import annotations


def compute_daily_summary(
    trades: list[dict],
    start_balance: float,
    end_balance: float,
) -> dict:
    realized_pnl = sum(t["realized_pnl"] for t in trades)
    wins = sum(1 for t in trades if t["realized_pnl"] > 0)
    losses = sum(1 for t in trades if t["realized_pnl"] < 0)

    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        running += t["realized_pnl"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return {
        "starting_balance": start_balance,
        "ending_balance": end_balance,
        "realized_pnl": realized_pnl,
        "trades_count": len(trades),
        "wins": wins,
        "losses": losses,
        "max_drawdown": max_dd,
    }


def compute_lifetime_summary(
    total_trades: int,
    wins: int,
    current_balance: float,
    peak_balance: float,
    initial_balance: float,
) -> dict:
    aggregate_pnl = current_balance - initial_balance
    aggregate_return_pct = (aggregate_pnl / initial_balance * 100) if initial_balance > 0 else 0.0
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    max_dd_pct = ((peak_balance - current_balance) / peak_balance * 100) if peak_balance > 0 else 0.0

    return {
        "initial_balance": initial_balance,
        "current_balance": current_balance,
        "aggregate_pnl": aggregate_pnl,
        "aggregate_return_pct": round(aggregate_return_pct, 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "peak_balance": peak_balance,
        "max_drawdown_pct": round(max_dd_pct, 2),
    }
