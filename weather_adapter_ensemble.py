"""
weather_adapter_ensemble.py — Fetch GFS ensemble forecast from Open-Meteo.

31-member GFS ensemble provides a real probability distribution for daily
high temperature. Free, no auth required.

API: https://ensemble-api.open-meteo.com/v1/ensemble
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Optional

import requests

log = logging.getLogger("ensemble_adapter")

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Denver International Airport coordinates
KDEN_LAT = 39.85
KDEN_LON = -104.66


@dataclass
class EnsembleForecast:
    """GFS ensemble forecast for a single day."""
    target_date: date
    member_highs: list[float]   # 31 high-temp forecasts in °F
    mean_high: float
    sigma: float

    def prob_above(self, threshold_f: float) -> float:
        """Fraction of ensemble members with daily high >= threshold."""
        if not self.member_highs:
            return 0.5
        count = sum(1 for h in self.member_highs if h >= threshold_f)
        return count / len(self.member_highs)

    def prob_below(self, threshold_f: float) -> float:
        return 1.0 - self.prob_above(threshold_f)


class EnsembleAdapter:
    """Fetches GFS ensemble forecasts from Open-Meteo."""

    def get_forecast(
        self,
        lat: float = KDEN_LAT,
        lon: float = KDEN_LON,
        target_date: Optional[date] = None,
    ) -> Optional[EnsembleForecast]:
        """
        Fetch 31-member GFS ensemble for target_date.

        Returns None on failure. Uses today if target_date not specified.
        """
        if target_date is None:
            target_date = date.today()

        try:
            resp = requests.get(
                ENSEMBLE_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max",
                    "temperature_unit": "fahrenheit",
                    "start_date": target_date.isoformat(),
                    "end_date": target_date.isoformat(),
                    "models": "gfs_seamless",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return self._parse_response(data, target_date)
        except Exception as e:
            log.warning("Ensemble fetch failed: %s", e)
            return None

    def _parse_response(self, data: dict, target_date: date) -> Optional[EnsembleForecast]:
        """Parse Open-Meteo ensemble JSON into EnsembleForecast."""
        daily = data.get("daily", {})
        if not daily:
            return None

        # Find the index for our target date
        dates = daily.get("time", [])
        try:
            idx = dates.index(target_date.isoformat())
        except ValueError:
            log.warning("Target date %s not in response dates: %s", target_date, dates)
            return None

        # Collect all member values for this date
        member_highs: list[float] = []

        # Control member (ensemble mean)
        control = daily.get("temperature_2m_max")
        if control and idx < len(control) and control[idx] is not None:
            member_highs.append(float(control[idx]))

        # Members 01-30
        for i in range(1, 31):
            key = f"temperature_2m_max_member{i:02d}"
            vals = daily.get(key)
            if vals and idx < len(vals) and vals[idx] is not None:
                member_highs.append(float(vals[idx]))

        if len(member_highs) < 5:
            log.warning("Only %d ensemble members found (need >= 5)", len(member_highs))
            return None

        mean_high = statistics.mean(member_highs)
        sigma = statistics.stdev(member_highs) if len(member_highs) >= 2 else 3.0

        # Floor sigma at 1.0 to prevent overconfidence
        sigma = max(sigma, 1.0)

        return EnsembleForecast(
            target_date=target_date,
            member_highs=member_highs,
            mean_high=round(mean_high, 2),
            sigma=round(sigma, 2),
        )
