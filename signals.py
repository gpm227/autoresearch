"""
signals.py — Convert model output + market prices into trade signals.

Applies all filters: edge, confidence, spread, time, risk, dedup.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config import MIN_EDGE, MIN_CONFIDENCE, MAX_SPREAD, MIN_HOURS_TO_PEAK
from weather_model import ProbabilityEstimate

log = logging.getLogger("signals")


@dataclass
class TradeSignal:
    contract_id: str
    market_id: str
    side: str
    market_price: float
    model_prob: float
    edge: float
    confidence: float
    spread: float
    reason: str


MIN_THRESHOLD_DISTANCE_F = 1.0  # skip if expected high is within this of threshold


def generate_signal(
    estimate: ProbabilityEstimate,
    market_yes_price: float,
    market_no_price: float,
    spread: float,
    hours_to_peak: float,
    held_contracts: set[str],
    risk_ok: bool,
    threshold_f: float = 0.0,
) -> Optional[TradeSignal]:
    contract_id = estimate.contract_id

    if not risk_ok:
        return None
    if estimate.confidence < MIN_CONFIDENCE:
        return None
    if spread > MAX_SPREAD:
        return None
    if hours_to_peak < MIN_HOURS_TO_PEAK:
        return None
    if contract_id in held_contracts:
        return None
    if abs(estimate.prob_yes - 0.50) < 0.10:
        return None

    # Skip when expected high is within ~1°F of threshold — model noise dominates
    if threshold_f > 0 and abs(estimate.expected_high_f - threshold_f) < MIN_THRESHOLD_DISTANCE_F:
        return None

    edge_yes = estimate.prob_yes - market_yes_price
    edge_no = (1.0 - estimate.prob_yes) - market_no_price

    side = None
    edge = 0.0
    market_price = 0.0

    if edge_yes >= MIN_EDGE and edge_no >= MIN_EDGE:
        if edge_yes >= edge_no:
            side, edge, market_price = "yes", edge_yes, market_yes_price
        else:
            side, edge, market_price = "no", edge_no, market_no_price
    elif edge_yes >= MIN_EDGE:
        side, edge, market_price = "yes", edge_yes, market_yes_price
    elif edge_no >= MIN_EDGE:
        side, edge, market_price = "no", edge_no, market_no_price
    else:
        return None

    reason = (
        f"model={estimate.prob_yes:.2f} vs market_{side}={market_price:.2f} "
        f"edge={edge:.2f} conf={estimate.confidence:.2f}"
    )
    log.info("SIGNAL %s %s: %s", contract_id, side.upper(), reason)

    return TradeSignal(
        contract_id=contract_id,
        market_id=estimate.contract_id.rsplit("-", 1)[0] if "-" in estimate.contract_id else estimate.contract_id,
        side=side,
        market_price=market_price,
        model_prob=estimate.prob_yes,
        edge=edge,
        confidence=estimate.confidence,
        spread=spread,
        reason=reason,
    )
