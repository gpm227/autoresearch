"""
weather_contracts.py — Parse Kalshi weather contract titles into structured objects.

Only supports Denver daily high temperature threshold contracts.
Anything else is marked unpriceable.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config import TARGET_CITY, TARGET_STATION

log = logging.getLogger("weather_contracts")

_CITY_STATION_MAP = {
    "denver": "KDEN",
}

# Matches: "79°", "79°F", "79 °F", ">79°", "<72°"
_THRESHOLD_TITLE_RE = re.compile(r"[><]?\s*(\d{1,3})\s*°")
# Ticker suffix: -T79 (threshold above) or -B78.5 (bracket — unpriceable)
_THRESHOLD_TICKER_RE = re.compile(r"-T(\d{1,3})$")
_BRACKET_TICKER_RE = re.compile(r"-B\d")


@dataclass
class WeatherContract:
    contract_id: str
    market_id: str
    title: str
    city: str
    station_id: str
    metric: str
    threshold_f: float
    expiry_ts: Optional[datetime]
    is_priceable: bool
    parse_status: str


def _extract_city(title: str) -> Optional[str]:
    title_lower = title.lower()
    for city in _CITY_STATION_MAP:
        if city in title_lower:
            return city
    return None


def _is_high_temp(title: str, ticker: str) -> bool:
    title_lower = title.lower()
    ticker_upper = ticker.upper()
    if "high" in title_lower or "KXHIGH" in ticker_upper:
        return True
    if ("above" in title_lower or "reach" in title_lower) and "low" not in title_lower:
        return True
    return False


def _extract_threshold(title: str, ticker: str) -> Optional[float]:
    m = _THRESHOLD_TITLE_RE.search(title)
    if m:
        return float(m.group(1))
    m = _THRESHOLD_TICKER_RE.search(ticker)
    if m:
        return float(m.group(1))
    return None


def _parse_close_time(close_time_str: str) -> Optional[datetime]:
    if not close_time_str:
        return None
    try:
        s = close_time_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def parse_weather_contract(market: dict) -> WeatherContract:
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    event_ticker = market.get("event_ticker", "")
    close_time_str = market.get("close_time", "")

    city_key = _extract_city(title)
    if city_key is None:
        return WeatherContract(
            contract_id=ticker, market_id=event_ticker, title=title,
            city="", station_id="", metric="", threshold_f=0.0,
            expiry_ts=None, is_priceable=False,
            parse_status="City not Denver or not recognized",
        )

    # Store display name (title-case) for the city field
    city_display = city_key.title()

    if city_key != TARGET_CITY.lower():
        return WeatherContract(
            contract_id=ticker, market_id=event_ticker, title=title,
            city=city_display, station_id="", metric="", threshold_f=0.0,
            expiry_ts=None, is_priceable=False,
            parse_status=f"City '{city_display}' is not target city {TARGET_CITY}",
        )

    station_id = _CITY_STATION_MAP[city_key]

    if not _is_high_temp(title, ticker):
        return WeatherContract(
            contract_id=ticker, market_id=event_ticker, title=title,
            city=city_display, station_id=station_id, metric="unknown",
            threshold_f=0.0, expiry_ts=None, is_priceable=False,
            parse_status="Metric is not daily high temperature",
        )

    # Reject bracket/range contracts (e.g. "78-79°", ticker -B78.5)
    if _BRACKET_TICKER_RE.search(ticker) or re.search(r"\d+\s*-\s*\d+\s*°", title):
        return WeatherContract(
            contract_id=ticker, market_id=event_ticker, title=title,
            city=city_display, station_id=station_id, metric="high_temp",
            threshold_f=0.0, expiry_ts=None, is_priceable=False,
            parse_status="Bracket/range contract — not a clean threshold",
        )

    threshold = _extract_threshold(title, ticker)
    if threshold is None:
        return WeatherContract(
            contract_id=ticker, market_id=event_ticker, title=title,
            city=city_display, station_id=station_id, metric="high_temp",
            threshold_f=0.0, expiry_ts=None, is_priceable=False,
            parse_status="Could not extract temperature threshold",
        )

    expiry = _parse_close_time(close_time_str)

    return WeatherContract(
        contract_id=ticker, market_id=event_ticker, title=title,
        city=city_display, station_id=station_id, metric="high_temp",
        threshold_f=threshold, expiry_ts=expiry,
        is_priceable=True, parse_status="ok",
    )
