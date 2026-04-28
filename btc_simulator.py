"""
btc_simulator.py — Replay logged BTC evaluations with a fixed strategy.

This is a lightweight experiment harness for the Karpathy-style loop:
- fixed infrastructure in the bot logs snapshots to SQLite
- strategy.py owns the tunable BTC decision surface
- this module replays those snapshots and scores a strategy
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Mapping, Optional, Sequence

from execution import _get_db
from file_io import append_tsv, ensure_tsv_header, read_json, write_json
from infra import Env, KalshiClient
from strategy import (
    BTCEvaluationInput,
    BTCStrategy,
    DEFAULT_BTC_STRATEGY,
    evaluate_btc_signal,
)


DATA_DIR = Path(__file__).parent
BTC_RESULTS_TSV = DATA_DIR / "btc_results.tsv"
BTC_EXPERIMENTS_TSV = DATA_DIR / "btc_experiments.tsv"
BTC_SETTLEMENT_CACHE = DATA_DIR / "btc_settlements.json"

BTC_RESULTS_COLS = [
    "evaluated_at",
    "ticker",
    "action",
    "side",
    "reason",
    "entry_price",
    "contracts",
    "market_result",
    "pnl",
]

BTC_EXPERIMENTS_COLS = [
    "timestamp",
    "description",
    "rows_train",
    "rows_val",
    "trades_train",
    "trades_val",
    "win_rate_train",
    "win_rate_val",
    "sharpe_train",
    "sharpe_val",
    "max_drawdown_train",
    "max_drawdown_val",
    "trades_per_day_train",
    "trades_per_day_val",
    "net_pnl_train",
    "net_pnl_val",
    "strategy_json",
]


@dataclass(frozen=True)
class BTCSimulationDecision:
    evaluated_at: str
    ticker: str
    action: str
    side: Optional[str]
    reason: str
    entry_price: Optional[float]
    contracts: int
    market_result: Optional[str]
    pnl: float


@dataclass(frozen=True)
class BTCSimulationMetrics:
    evaluated_rows: int
    resolved_rows: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    avg_pnl: float
    sharpe: float
    max_drawdown: float
    trades_per_day: float
    skipped: int
    unresolved: int


@dataclass(frozen=True)
class BTCSimulationReport:
    strategy: dict[str, object]
    overall: BTCSimulationMetrics
    train: BTCSimulationMetrics
    validation: BTCSimulationMetrics
    trade_count: int
    split_index: int


def _fetch_market_result(client: KalshiClient, ticker: str) -> Optional[str]:
    try:
        live_resp = client.get_market(ticker)
        market = live_resp.get("market", live_resp)
        result = market.get("result")
        if result in {"yes", "no"}:
            return result
    except Exception:
        pass

    try:
        hist_resp = client.get_historical_market(ticker)
        market = hist_resp.get("market", hist_resp)
        result = market.get("result")
        if result in {"yes", "no"}:
            return result
    except Exception:
        pass

    return None


def load_settlement_results(
    tickers: Sequence[str],
    client: Optional[KalshiClient] = None,
    cache_path: Path = BTC_SETTLEMENT_CACHE,
) -> dict[str, str]:
    cache = read_json(cache_path, default={}) or {}
    updated = False
    results: dict[str, str] = {}

    for ticker in sorted(set(tickers)):
        cached = cache.get(ticker)
        if cached in {"yes", "no"}:
            results[ticker] = cached
            continue
        if client is None:
            continue
        result = _fetch_market_result(client, ticker)
        if result in {"yes", "no"}:
            cache[ticker] = result
            results[ticker] = result
            updated = True

    if updated:
        write_json(cache_path, cache)

    return results


def _coerce_feed_health(value: object) -> str:
    if value is None:
        return "LIVE"
    return str(value).upper()


def _coerce_float(value: object) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _distance_to_target(row: Mapping[str, object]) -> Optional[float]:
    price = _coerce_float(row.get("btc_price"))
    target = _coerce_float(row.get("target_price"))
    if not price or not target:
        return None
    return abs(price - target) / target


def _decision_from_row(
    row: Mapping[str, object],
    strategy: BTCStrategy,
    traded_tickers: set[str],
    day_state: dict[str, dict[str, float | int | bool]],
) -> tuple[Optional[str], float, str]:
    day = str(row["evaluated_at"])[:10]
    state = day_state.setdefault(
        day,
        {"trades_count": 0, "realized_pnl": 0.0, "day_locked": False},
    )
    distance = _distance_to_target(row)
    inputs = BTCEvaluationInput(
        ticker=str(row["ticker"]),
        prob_yes=float(row["model_prob"]),
        confidence=float(row["confidence"]),
        yes_ask=_coerce_float(row.get("yes_ask")),
        no_ask=_coerce_float(row.get("no_ask")),
        spread=_coerce_float(row.get("kalshi_spread")),
        seconds_remaining=float(row["seconds_remaining"]),
        feed_health=_coerce_feed_health(row.get("feed_health")),
        venue_spread_bps=_coerce_float(row.get("venue_spread_bps")),
        already_held=str(row["ticker"]) in traded_tickers,
        trades_today=int(state["trades_count"]),
        realized_pnl_today=float(state["realized_pnl"]),
        day_locked=bool(state["day_locked"]),
        too_far_from_target=distance is not None and distance > strategy.max_target_distance,
        target_distance=distance,
    )
    return evaluate_btc_signal(inputs, strategy=strategy)


def simulate_rows(
    rows: Sequence[Mapping[str, object]],
    settlement_results: Mapping[str, str],
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
) -> tuple[list[BTCSimulationDecision], BTCSimulationMetrics]:
    decisions: list[BTCSimulationDecision] = []
    traded_tickers: set[str] = set()
    day_state: dict[str, dict[str, float | int | bool]] = {}

    for row in rows:
        side, _, reason = _decision_from_row(row, strategy, traded_tickers, day_state)
        if side is None:
            decisions.append(
                BTCSimulationDecision(
                    evaluated_at=str(row["evaluated_at"]),
                    ticker=str(row["ticker"]),
                    action="skip",
                    side=None,
                    reason=reason,
                    entry_price=None,
                    contracts=0,
                    market_result=None,
                    pnl=0.0,
                )
            )
            continue

        ticker = str(row["ticker"])
        result = settlement_results.get(ticker)
        price_key = "yes_ask" if side == "yes" else "no_ask"
        entry_price = _coerce_float(row.get(price_key))
        if result not in {"yes", "no"} or entry_price is None:
            decisions.append(
                BTCSimulationDecision(
                    evaluated_at=str(row["evaluated_at"]),
                    ticker=ticker,
                    action="unresolved",
                    side=side,
                    reason="UNRESOLVED_MARKET",
                    entry_price=entry_price,
                    contracts=0,
                    market_result=result,
                    pnl=0.0,
                )
            )
            continue

        contracts = strategy.contracts_for_entry_price(entry_price)
        pnl = contracts * ((1.0 - entry_price) if result == side else -entry_price)
        traded_tickers.add(ticker)

        day = str(row["evaluated_at"])[:10]
        state = day_state.setdefault(
            day,
            {"trades_count": 0, "realized_pnl": 0.0, "day_locked": False},
        )
        state["trades_count"] = int(state["trades_count"]) + 1
        state["realized_pnl"] = float(state["realized_pnl"]) + pnl
        if float(state["realized_pnl"]) <= -strategy.max_daily_loss:
            state["day_locked"] = True

        decisions.append(
            BTCSimulationDecision(
                evaluated_at=str(row["evaluated_at"]),
                ticker=ticker,
                action="trade",
                side=side,
                reason="SIGNAL",
                entry_price=entry_price,
                contracts=contracts,
                market_result=result,
                pnl=pnl,
            )
        )

    metrics = summarize_decisions(decisions)
    return decisions, metrics


def summarize_decisions(decisions: Sequence[BTCSimulationDecision]) -> BTCSimulationMetrics:
    trades = [d for d in decisions if d.action == "trade"]
    trade_pnls = [d.pnl for d in trades]
    wins = sum(1 for pnl in trade_pnls if pnl > 0)
    losses = sum(1 for pnl in trade_pnls if pnl <= 0)
    win_rate = wins / len(trades) if trades else 0.0
    net_pnl = sum(trade_pnls)
    avg_pnl = mean(trade_pnls) if trade_pnls else 0.0

    resolved_rows = sum(1 for d in decisions if d.action in {"trade", "skip"})
    unresolved = sum(1 for d in decisions if d.action == "unresolved")
    skipped = sum(1 for d in decisions if d.action == "skip")

    daily_pnl: dict[str, float] = defaultdict(float)
    for trade in trades:
        daily_pnl[trade.evaluated_at[:10]] += trade.pnl
    daily_returns = list(daily_pnl.values())

    if len(daily_returns) >= 2 and stdev(daily_returns) > 0:
        sharpe = (mean(daily_returns) / stdev(daily_returns)) * math.sqrt(252.0)
    else:
        sharpe = 0.0

    peak = 0.0
    equity = 0.0
    max_drawdown = 0.0
    for pnl in trade_pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    trade_days = len(daily_pnl) if daily_pnl else 0
    trades_per_day = len(trades) / trade_days if trade_days else 0.0

    return BTCSimulationMetrics(
        evaluated_rows=len(decisions),
        resolved_rows=resolved_rows,
        trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        net_pnl=net_pnl,
        avg_pnl=avg_pnl,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        trades_per_day=trades_per_day,
        skipped=skipped,
        unresolved=unresolved,
    )


def load_btc_evaluations(db: Optional[sqlite3.Connection] = None) -> list[dict[str, object]]:
    owns_db = db is None
    db = db or _get_db()
    table_exists = db.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table' AND name='btc_market_evaluations'
        """
    ).fetchone()
    if not table_exists:
        if owns_db:
            db.close()
        return []
    rows = db.execute(
        "SELECT * FROM btc_market_evaluations ORDER BY evaluated_at ASC, id ASC"
    ).fetchall()
    payload = [dict(row) for row in rows]
    if owns_db:
        db.close()
    return payload


def run_btc_simulation(
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
    experiment_label: Optional[str] = None,
    refresh_settlements: bool = True,
    write_results: bool = True,
) -> BTCSimulationReport:
    rows = load_btc_evaluations()
    if not rows:
        empty = BTCSimulationMetrics(
            evaluated_rows=0,
            resolved_rows=0,
            trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            net_pnl=0.0,
            avg_pnl=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            trades_per_day=0.0,
            skipped=0,
            unresolved=0,
        )
        return BTCSimulationReport(
            strategy=strategy.as_dict(),
            overall=empty,
            train=empty,
            validation=empty,
            trade_count=0,
            split_index=0,
        )

    client = KalshiClient(env=Env.LIVE) if refresh_settlements else None
    settlements = load_settlement_results(
        [str(row["ticker"]) for row in rows],
        client=client,
    )

    split_index = max(1, int(len(rows) * 0.7)) if len(rows) > 1 else len(rows)
    overall_decisions, overall_metrics = simulate_rows(rows, settlements, strategy=strategy)
    _, train_metrics = simulate_rows(rows[:split_index], settlements, strategy=strategy)
    _, validation_metrics = simulate_rows(rows[split_index:], settlements, strategy=strategy)

    if write_results:
        ensure_tsv_header(BTC_RESULTS_TSV, BTC_RESULTS_COLS)
        BTC_RESULTS_TSV.write_text("\t".join(BTC_RESULTS_COLS) + "\n", encoding="utf-8")
        for decision in overall_decisions:
            append_tsv(
                BTC_RESULTS_TSV,
                [
                    decision.evaluated_at,
                    decision.ticker,
                    decision.action,
                    decision.side or "",
                    decision.reason,
                    "" if decision.entry_price is None else f"{decision.entry_price:.4f}",
                    decision.contracts,
                    decision.market_result or "",
                    f"{decision.pnl:.4f}",
                ],
            )

    report = BTCSimulationReport(
        strategy=strategy.as_dict(),
        overall=overall_metrics,
        train=train_metrics,
        validation=validation_metrics,
        trade_count=overall_metrics.trades,
        split_index=split_index,
    )

    if experiment_label:
        ensure_tsv_header(BTC_EXPERIMENTS_TSV, BTC_EXPERIMENTS_COLS)
        append_tsv(
            BTC_EXPERIMENTS_TSV,
            [
                datetime.utcnow().isoformat(),
                experiment_label,
                train_metrics.evaluated_rows,
                validation_metrics.evaluated_rows,
                train_metrics.trades,
                validation_metrics.trades,
                f"{train_metrics.win_rate:.4f}",
                f"{validation_metrics.win_rate:.4f}",
                f"{train_metrics.sharpe:.4f}",
                f"{validation_metrics.sharpe:.4f}",
                f"{train_metrics.max_drawdown:.4f}",
                f"{validation_metrics.max_drawdown:.4f}",
                f"{train_metrics.trades_per_day:.4f}",
                f"{validation_metrics.trades_per_day:.4f}",
                f"{train_metrics.net_pnl:.4f}",
                f"{validation_metrics.net_pnl:.4f}",
                json.dumps(strategy.as_dict(), sort_keys=True),
            ],
        )

    return report


def report_to_json(report: BTCSimulationReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)
