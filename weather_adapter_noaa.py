"""
weather_adapter_noaa.py — Fetch and parse METAR data from NOAA Aviation Weather Center.

Uses the Aviation Weather API: https://aviationweather.gov/api/data/metar
Free, no auth required. Rate limit: 100 req/min.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from config import METAR_HOURS_BACK

log = logging.getLogger("weather_adapter")

NOAA_METAR_URL = "https://aviationweather.gov/api/data/metar"


@dataclass
class MetarObservation:
    station: str
    obs_time: datetime
    temp_c: float
    raw_text: str


class WeatherAdapterNOAA:
    """Fetches and parses METAR observations from NOAA Aviation Weather Center."""

    def get_recent_metars(self, station: str, hours: int = METAR_HOURS_BACK) -> list[MetarObservation]:
        """Fetch recent METARs for a station. Retries up to 3 times on failure."""
        for attempt in range(3):
            try:
                resp = requests.get(
                    NOAA_METAR_URL,
                    params={"ids": station, "format": "json", "hours": hours},
                    timeout=15,
                )
                if resp.status_code == 204:
                    log.info("NOAA returned 204 (no data) for %s", station)
                    return []
                resp.raise_for_status()
                data = resp.json()
                return self._parse_metars(data)
            except Exception as e:
                log.warning("METAR fetch attempt %d failed for %s: %s", attempt + 1, station, e)
                if attempt < 2:
                    import time
                    time.sleep(2)
        log.error("METAR fetch failed after 3 attempts for %s", station)
        return []

    def _parse_metars(self, data: list | None) -> list[MetarObservation]:
        """Parse NOAA JSON METAR response into MetarObservation list."""
        if not data:
            return []
        results = []
        for entry in data:
            temp = entry.get("temp")
            if temp is None:
                continue
            try:
                obs_time = datetime.fromtimestamp(entry["obsTime"], tz=timezone.utc)
            except (KeyError, TypeError, ValueError, OSError):
                continue
            results.append(MetarObservation(
                station=entry.get("icaoId", ""),
                obs_time=obs_time,
                temp_c=float(temp),
                raw_text=entry.get("rawOb", ""),
            ))
        results.sort(key=lambda m: m.obs_time)
        return results

    @staticmethod
    def c_to_f(temp_c: float) -> float:
        return temp_c * 9.0 / 5.0 + 32.0

    def compute_recent_trend_f(self, metars: list[MetarObservation]) -> Optional[float]:
        """
        Compute recent temperature trend in degrees F per hour.
        Uses earliest and latest observation. Returns None if <2 obs or <30 min span.
        """
        if len(metars) < 2:
            return None
        oldest = metars[0]
        newest = metars[-1]
        hours = (newest.obs_time - oldest.obs_time).total_seconds() / 3600.0
        if hours < 0.5:
            return None
        delta_f = self.c_to_f(newest.temp_c) - self.c_to_f(oldest.temp_c)
        return delta_f / hours
