"""
weather_model.py — Temperature probability model for Denver.

V2 (active): METAR-only intraday curve projection.
V3 (shadow): GFS ensemble base + METAR intraday adjustment.

Both models run every tick. V2 drives signals; V3 logs for comparison.
Switch to V3 as primary after calibration proves it's better.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from scipy.stats import norm

from config import (
    GAIN_RATE_F_PER_HOUR,
    PEAK_HOUR_LOCAL,
    TREND_ADJUST_F,
    SIGMA_EARLY_F,
    SIGMA_MID_F,
    SIGMA_LATE_F,
    MODEL_VERSION,
    MAX_METAR_AGE_MINUTES,
    CITY_TIMEZONE,
    MIN_CONFIDENCE,
)
from weather_adapter_noaa import MetarObservation, WeatherAdapterNOAA
from weather_adapter_ensemble import EnsembleForecast
from weather_contracts import WeatherContract

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

log = logging.getLogger("weather_model")


@dataclass
class ProbabilityEstimate:
    contract_id: str
    prob_yes: float
    confidence: float
    expected_high_f: float
    adjusted_high_f: float
    sigma_f: float
    current_temp_f: Optional[float]
    diagnostics: dict
    model_version: str


class WeatherModel:

    def __init__(self):
        self._tz = ZoneInfo(CITY_TIMEZONE)

    def _hours_to_peak(self, now: datetime) -> float:
        local_now = now.astimezone(self._tz)
        current_hour = local_now.hour + local_now.minute / 60.0
        return max(PEAK_HOUR_LOCAL - current_hour, 0.0)

    def _projected_gain(self, hours_to_peak: float, recent_trend_f: Optional[float]) -> float:
        base_gain = GAIN_RATE_F_PER_HOUR * hours_to_peak
        if recent_trend_f is None or hours_to_peak <= 0:
            return base_gain
        expected_trend = GAIN_RATE_F_PER_HOUR * 1.8
        deviation = recent_trend_f - expected_trend
        if deviation > 0.2:
            return base_gain + TREND_ADJUST_F
        elif deviation < -0.2:
            return base_gain - TREND_ADJUST_F
        return base_gain

    def _sigma(self, hours_to_peak: float) -> float:
        if hours_to_peak > 5.0:
            return SIGMA_EARLY_F
        elif hours_to_peak > 3.0:
            return SIGMA_MID_F
        else:
            return SIGMA_LATE_F

    def _compute_probability(self, expected_high_f: float, sigma_f: float, threshold_f: float) -> float:
        return float(1.0 - norm.cdf(threshold_f, loc=expected_high_f, scale=sigma_f))

    def price_contract(
        self,
        contract: WeatherContract,
        metars: list[MetarObservation],
        now: Optional[datetime] = None,
    ) -> ProbabilityEstimate:
        if now is None:
            now = datetime.now(timezone.utc)

        adapter = WeatherAdapterNOAA()
        hours_to_peak = self._hours_to_peak(now)

        current_temp_f = None
        confidence = 0.70

        if not metars:
            return ProbabilityEstimate(
                contract_id=contract.contract_id,
                prob_yes=0.5, confidence=0.3,
                expected_high_f=0.0, adjusted_high_f=0.0,
                sigma_f=SIGMA_EARLY_F, current_temp_f=None,
                diagnostics={"error": "no_metars"},
                model_version=MODEL_VERSION,
            )

        latest = metars[-1]
        current_temp_f = adapter.c_to_f(latest.temp_c)
        metar_age_min = (now - latest.obs_time).total_seconds() / 60.0

        if metar_age_min > MAX_METAR_AGE_MINUTES:
            return ProbabilityEstimate(
                contract_id=contract.contract_id,
                prob_yes=0.5, confidence=0.3,
                expected_high_f=0.0, adjusted_high_f=0.0,
                sigma_f=SIGMA_EARLY_F, current_temp_f=current_temp_f,
                diagnostics={"error": "stale_metar", "metar_age_min": metar_age_min},
                model_version=MODEL_VERSION,
            )

        recent_trend_f = adapter.compute_recent_trend_f(metars)
        projected_gain = self._projected_gain(hours_to_peak, recent_trend_f)
        expected_high = current_temp_f + projected_gain

        if hours_to_peak <= 0:
            all_temps_f = [adapter.c_to_f(m.temp_c) for m in metars]
            expected_high = max(all_temps_f)

        sigma = self._sigma(hours_to_peak)
        prob_yes = self._compute_probability(expected_high, sigma, contract.threshold_f)

        if metar_age_min > 30:
            confidence -= 0.10
        if len(metars) < 3:
            confidence -= 0.10
        confidence = max(confidence, 0.0)

        return ProbabilityEstimate(
            contract_id=contract.contract_id,
            prob_yes=prob_yes,
            confidence=confidence,
            expected_high_f=expected_high,
            adjusted_high_f=expected_high,
            sigma_f=sigma,
            current_temp_f=current_temp_f,
            diagnostics={
                "hours_to_peak": round(hours_to_peak, 2),
                "projected_gain": round(projected_gain, 2),
                "recent_trend": round(recent_trend_f, 2) if recent_trend_f else None,
                "metar_age_min": round(metar_age_min, 1),
                "metar_count": len(metars),
                "model_type": MODEL_VERSION,
            },
            model_version=MODEL_VERSION,
        )

    # ─── V3: Ensemble + METAR blended model (shadow mode) ────────────────────

    ENSEMBLE_MODEL_VERSION = "ensemble_metar_v3"
    METAR_BLEND_WEIGHT = 0.5  # how much to shift mean based on METAR delta

    def _expected_temp_at_time(self, daily_high_f: float, hours_to_peak: float) -> float:
        """
        Estimate what temp should be right now, given the forecasted daily high.

        Simple intraday curve: morning low is ~30F below high, rises linearly
        to peak. After peak, holds near high.

        This is a rough heuristic — the key insight is that the DELTA between
        this expectation and the actual METAR is what matters, not the absolute value.
        """
        if hours_to_peak <= 0:
            return daily_high_f
        # Assume morning low is ~30F below daily high (Denver typical diurnal range)
        morning_low = daily_high_f - 30.0
        # Total hours from assumed sunrise (6:00 local) to peak (15:30)
        total_rise_hours = PEAK_HOUR_LOCAL - 6.0  # ~9.5 hours
        # How far through the day are we?
        hours_since_sunrise = total_rise_hours - hours_to_peak
        if hours_since_sunrise <= 0:
            return morning_low
        frac = min(hours_since_sunrise / total_rise_hours, 1.0)
        return morning_low + frac * (daily_high_f - morning_low)

    def price_contract_ensemble(
        self,
        contract: WeatherContract,
        metars: list[MetarObservation],
        ensemble: Optional[EnsembleForecast],
        now: Optional[datetime] = None,
    ) -> ProbabilityEstimate:
        """
        V3 model: GFS ensemble base distribution + METAR intraday correction.

        1. Start with ensemble mean_high and sigma
        2. Compute expected temp at current time from intraday curve
        3. Compare to actual METAR temp → delta
        4. Shift ensemble mean by 0.5 * delta
        5. Compute P(high >= threshold) with adjusted mean + ensemble sigma

        Returns low-confidence estimate if ensemble is unavailable.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        adapter = WeatherAdapterNOAA()
        hours_to_peak = self._hours_to_peak(now)

        # No ensemble → fall back to METAR-only V2
        if ensemble is None:
            est = self.price_contract(contract, metars, now=now)
            est.diagnostics["ensemble_status"] = "unavailable"
            est.model_version = self.ENSEMBLE_MODEL_VERSION
            return est

        mean_high = ensemble.mean_high
        sigma = ensemble.sigma
        confidence = 0.75  # ensemble gives higher base confidence

        # Get current temp from METAR
        current_temp_f = None
        metar_age_min = None

        if metars:
            latest = metars[-1]
            current_temp_f = adapter.c_to_f(latest.temp_c)
            metar_age_min = (now - latest.obs_time).total_seconds() / 60.0

            if metar_age_min <= MAX_METAR_AGE_MINUTES:
                # Compute delta: actual vs expected for this time of day
                expected_now = self._expected_temp_at_time(mean_high, hours_to_peak)
                delta = current_temp_f - expected_now

                # Shift the distribution
                adjusted_mean = mean_high + self.METAR_BLEND_WEIGHT * delta
            else:
                # Stale METAR — use ensemble as-is
                adjusted_mean = mean_high
                confidence -= 0.10
        else:
            # No METAR — pure ensemble
            adjusted_mean = mean_high
            confidence -= 0.15

        # After peak, use max observed temp if available
        if hours_to_peak <= 0 and metars:
            all_temps_f = [adapter.c_to_f(m.temp_c) for m in metars]
            max_observed = max(all_temps_f)
            # Blend: 70% max observed, 30% ensemble (obs dominates late)
            adjusted_mean = 0.7 * max_observed + 0.3 * mean_high

        prob_yes = self._compute_probability(adjusted_mean, sigma, contract.threshold_f)

        return ProbabilityEstimate(
            contract_id=contract.contract_id,
            prob_yes=prob_yes,
            confidence=confidence,
            expected_high_f=mean_high,
            adjusted_high_f=adjusted_mean,
            sigma_f=sigma,
            current_temp_f=current_temp_f,
            diagnostics={
                "hours_to_peak": round(hours_to_peak, 2),
                "ensemble_mean": mean_high,
                "ensemble_sigma": sigma,
                "ensemble_members": len(ensemble.member_highs),
                "metar_delta": round(current_temp_f - self._expected_temp_at_time(mean_high, hours_to_peak), 2) if current_temp_f and metar_age_min and metar_age_min <= MAX_METAR_AGE_MINUTES else None,
                "adjusted_mean": round(adjusted_mean, 2),
                "metar_age_min": round(metar_age_min, 1) if metar_age_min else None,
                "model_type": self.ENSEMBLE_MODEL_VERSION,
            },
            model_version=self.ENSEMBLE_MODEL_VERSION,
        )
