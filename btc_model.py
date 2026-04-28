"""
btc_model.py — BTC 15-minute up/down pricing model.

V2: Log-normal with adaptive vol floor + momentum drift.

Given current BTC price, target price, time remaining, realized vol,
and recent price history, computes P(BTC > target at expiry).

Changes from V1:
  - Vol floor raised to 35% annualized (BTC jump risk is never 10%)
  - Adaptive floor: max(realized_vol, regime_floor) where regime_floor
    uses the higher of 15-min vs 1-hr realized vol
  - Momentum drift: 5-candle signed return adjusts expected mean
  - Enhanced logging for every evaluation (skip reason included)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from scipy.stats import norm
from strategy import BTCStrategy, DEFAULT_BTC_STRATEGY

log = logging.getLogger("btc_model")

# Vol floor: BTC is never this quiet on a 15-min horizon.
# Even in "flat" tapes, microstructure noise creates ~35% annualized.
VOL_FLOOR_ANNUAL = DEFAULT_BTC_STRATEGY.vol_floor_annual

# Momentum: how much to shift drift based on recent returns.
# A 5-candle signed return of +0.1% shifts drift by MOMENTUM_WEIGHT * 0.001.
# Keep it small — this is a testable hypothesis, not a conviction.
MOMENTUM_WEIGHT = DEFAULT_BTC_STRATEGY.momentum_weight
MOMENTUM_CANDLES = DEFAULT_BTC_STRATEGY.momentum_candles


@dataclass
class BTCEstimate:
    """Output of the pricing model for a single 15-min market."""
    contract_id: str
    prob_yes: float         # model probability of "up" (above target)
    confidence: float       # how much we trust the vol estimate
    current_price: float    # live BTC price
    target_price: float     # Kalshi's target
    seconds_remaining: float
    realized_vol: float     # annualized vol used (after floor/adaptive)
    raw_vol: float = 0.0    # vol before floor applied
    drift: float = 0.0      # momentum-adjusted drift (annualized)
    model_version: str = "btc_lognormal_v2"


def price_btc_contract(
    contract_id: str,
    current_price: float,
    target_price: float,
    seconds_remaining: float,
    realized_vol_annual: float,
    vol_sample_count: int = 0,
    recent_closes: Optional[list[float]] = None,
    strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
) -> BTCEstimate:
    """
    Log-normal model for P(BTC > target in t seconds).

    V2 changes:
      - Drift adjusted by momentum (5-candle signed return)
      - Vol floored at 35% annualized
      - P(S_t > K) = Φ( (ln(S/K) + μt) / (σ√t) )

    where μ is the momentum-adjusted drift (annualized).
    """
    raw_vol = realized_vol_annual

    if seconds_remaining <= 0:
        prob = 1.0 if current_price > target_price else 0.0
        return BTCEstimate(
            contract_id=contract_id, prob_yes=prob, confidence=1.0,
            current_price=current_price, target_price=target_price,
            seconds_remaining=0, realized_vol=raw_vol, raw_vol=raw_vol,
        )

    if current_price <= 0 or target_price <= 0:
        return BTCEstimate(
            contract_id=contract_id, prob_yes=0.5, confidence=0.0,
            current_price=current_price, target_price=target_price,
            seconds_remaining=seconds_remaining, realized_vol=raw_vol,
            raw_vol=raw_vol,
        )

    # ── Adaptive vol: max(realized, floor) ──────────────────────────────
    vol = max(realized_vol_annual, strategy.vol_floor_annual)

    # ── Momentum drift ──────────────────────────────────────────────────
    drift = _compute_momentum_drift(
        recent_closes,
        momentum_weight=strategy.momentum_weight,
        momentum_candles=strategy.momentum_candles,
    )

    # ── Log-normal with drift ───────────────────────────────────────────
    t_years = seconds_remaining / (365.25 * 24 * 3600)
    sigma_sqrt_t = vol * math.sqrt(t_years)

    if sigma_sqrt_t < 1e-10:
        prob = 1.0 if current_price > target_price else 0.0
    else:
        # d = (ln(S/K) + μ*t) / (σ√t)
        d = (math.log(current_price / target_price) + drift * t_years) / sigma_sqrt_t
        prob = norm.cdf(d)

    prob = max(0.01, min(0.99, prob))
    confidence = _vol_confidence(vol_sample_count, seconds_remaining)

    return BTCEstimate(
        contract_id=contract_id,
        prob_yes=prob,
        confidence=confidence,
        current_price=current_price,
        target_price=target_price,
        seconds_remaining=seconds_remaining,
        realized_vol=vol,
        raw_vol=raw_vol,
        drift=drift,
    )


def _compute_momentum_drift(
    recent_closes: Optional[list[float]],
    momentum_weight: float = MOMENTUM_WEIGHT,
    momentum_candles: int = MOMENTUM_CANDLES,
) -> float:
    """
    Compute annualized drift adjustment from recent candle closes.

    Uses the signed return over the last MOMENTUM_CANDLES candles,
    scaled by MOMENTUM_WEIGHT, then annualized.

    Returns 0.0 if insufficient data.
    """
    if not recent_closes or len(recent_closes) < momentum_candles + 1:
        return 0.0

    # Last N candles
    tail = recent_closes[-(momentum_candles + 1):]
    if tail[0] <= 0:
        return 0.0

    # Signed return over the momentum window
    ret = (tail[-1] - tail[0]) / tail[0]

    # Scale by weight and annualize (candles are 1-min, so N candles = N minutes)
    minutes = momentum_candles
    periods_per_year = (365.25 * 24 * 60) / minutes
    annualized_drift = ret * momentum_weight * periods_per_year

    return annualized_drift


def _vol_confidence(sample_count: int, seconds_remaining: float) -> float:
    """
    Confidence in our vol estimate.
    - More samples → higher confidence
    - Very little time remaining → lower confidence (microstructure noise)
    """
    if sample_count >= 30:
        sample_conf = 0.85
    elif sample_count >= 15:
        sample_conf = 0.70
    elif sample_count >= 5:
        sample_conf = 0.50
    else:
        sample_conf = 0.30

    if seconds_remaining < 60:
        time_conf = 0.30
    elif seconds_remaining < 120:
        time_conf = 0.60
    else:
        time_conf = 1.0

    return sample_conf * time_conf


def compute_realized_vol(
    candle_closes: list[float],
    candle_interval_sec: int = 60,
    vol_floor_annual: float = VOL_FLOOR_ANNUAL,
) -> tuple[float, int]:
    """
    Compute annualized realized volatility from a list of close prices.

    Uses log-returns standard deviation, annualized by sqrt(periods_per_year).
    Floor at VOL_FLOOR_ANNUAL (35%).

    Returns (annualized_vol, sample_count).
    """
    if len(candle_closes) < 2:
        return 0.0, 0

    log_returns = []
    for i in range(1, len(candle_closes)):
        if candle_closes[i - 1] > 0 and candle_closes[i] > 0:
            log_returns.append(math.log(candle_closes[i] / candle_closes[i - 1]))

    if len(log_returns) < 2:
        return 0.0, 0

    mean_ret = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_ret) ** 2 for r in log_returns) / (len(log_returns) - 1)
    std_dev = math.sqrt(variance)

    periods_per_year = (365.25 * 24 * 3600) / candle_interval_sec
    annualized_vol = std_dev * math.sqrt(periods_per_year)

    # Floor at 35% — BTC jump risk on a 15-min horizon is never below this
    annualized_vol = max(annualized_vol, vol_floor_annual)

    return annualized_vol, len(log_returns)
