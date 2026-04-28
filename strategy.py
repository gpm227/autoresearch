"""
strategy.py — Single-file BTC strategy surface for autoresearch.

The intent is to keep the experimentable parts of the BTC bot in one file:
- signal thresholds
- risk caps
- simple model knobs
- trade selection logic
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


LIVE_FEED = "LIVE"
DEGRADED_FEED = "DEGRADED"
STALE_FEED = "STALE"


@dataclass(frozen=True)
class BTCStrategy:
    min_edge: float = 0.12
    min_confidence: float = 0.70
    max_spread: float = 0.06
    max_feed_spread_bps: float = 8.0
    degraded_feed_edge_penalty: float = 0.05
    min_time_remaining_sec: int = 120
    max_target_distance: float = 0.005
    max_risk_per_trade: float = 3.00
    max_daily_trades: int = 50
    max_daily_loss: float = 25.00
    vol_floor_annual: float = 0.35
    momentum_weight: float = 0.5
    momentum_candles: int = 5
    strategy_version: str = "btc_strategy_v1"

    def min_edge_for_feed(self, feed_health: str) -> float:
        if str(feed_health).upper() == DEGRADED_FEED:
            return self.min_edge + self.degraded_feed_edge_penalty
        return self.min_edge

    def contracts_for_entry_price(self, entry_price: float) -> int:
        if entry_price <= 0:
            return 0
        return max(int(self.max_risk_per_trade / entry_price), 1)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


DEFAULT_BTC_STRATEGY = BTCStrategy()


@dataclass(frozen=True)
class BTCEvaluationInput:
    ticker: str
    prob_yes: float
    confidence: float
    yes_ask: Optional[float]
    no_ask: Optional[float]
    spread: Optional[float]
    seconds_remaining: float
    feed_health: str = LIVE_FEED
    venue_spread_bps: Optional[float] = None
    already_held: bool = False
    trades_today: int = 0
    realized_pnl_today: float = 0.0
    day_locked: bool = False
    too_far_from_target: bool = False
    target_distance: Optional[float] = None


def evaluate_btc_signal(
    inputs: BTCEvaluationInput,
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
) -> tuple[Optional[str], float, str]:
    """
    Decide whether to trade a BTC Kalshi market snapshot.

    Returns:
        (side, edge, reason)
    where side is "yes"/"no" or None and reason is a stable skip/signal label.
    """
    if str(inputs.feed_health).upper() == STALE_FEED:
        return None, 0.0, "STALE_FEED"

    if (
        inputs.venue_spread_bps is not None
        and inputs.venue_spread_bps > strategy.max_feed_spread_bps
    ):
        return None, 0.0, f"WIDE_SPREAD({inputs.venue_spread_bps:.0f}bps)"

    if inputs.already_held:
        return None, 0.0, "HELD"

    if inputs.trades_today >= strategy.max_daily_trades:
        return None, 0.0, "MAX_TRADES"

    if inputs.realized_pnl_today <= -strategy.max_daily_loss:
        return None, 0.0, "MAX_LOSS"

    if inputs.day_locked:
        return None, 0.0, "DAY_LOCKED"

    if inputs.spread is None:
        return None, 0.0, "NO_SPREAD"

    if inputs.spread > strategy.max_spread:
        return None, 0.0, f"KALSHI_SPREAD({inputs.spread:.3f})"

    if inputs.seconds_remaining < strategy.min_time_remaining_sec:
        return None, 0.0, "TOO_CLOSE"

    if inputs.too_far_from_target:
        if inputs.target_distance is not None:
            return None, 0.0, f"FAR_FROM_TARGET({inputs.target_distance:.4f})"
        return None, 0.0, "FAR_FROM_TARGET"

    if inputs.yes_ask is None or inputs.no_ask is None:
        return None, 0.0, "NO_ASK"

    if inputs.confidence < strategy.min_confidence:
        return None, 0.0, f"LOW_CONF({inputs.confidence:.2f})"

    min_edge = strategy.min_edge_for_feed(inputs.feed_health)
    edge_yes = inputs.prob_yes - inputs.yes_ask
    edge_no = (1.0 - inputs.prob_yes) - inputs.no_ask

    side: Optional[str] = None
    edge = 0.0
    if edge_yes >= min_edge and edge_no >= min_edge:
        if edge_yes >= edge_no:
            side, edge = "yes", edge_yes
        else:
            side, edge = "no", edge_no
    elif edge_yes >= min_edge:
        side, edge = "yes", edge_yes
    elif edge_no >= min_edge:
        side, edge = "no", edge_no
    else:
        return None, 0.0, f"LOW_EDGE({max(edge_yes, edge_no):.3f})"

    return side, edge, "SIGNAL"
