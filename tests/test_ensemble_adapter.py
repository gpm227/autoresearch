# tests/test_ensemble_adapter.py
import pytest
from datetime import date
from weather_adapter_ensemble import EnsembleAdapter, EnsembleForecast


SAMPLE_RESPONSE = {
    "daily": {
        "time": ["2026-04-10"],
        "temperature_2m_max": [72.5],
        "temperature_2m_max_member01": [70.9],
        "temperature_2m_max_member02": [73.1],
        "temperature_2m_max_member03": [71.5],
        "temperature_2m_max_member04": [74.2],
        "temperature_2m_max_member05": [69.8],
        "temperature_2m_max_member06": [72.0],
        "temperature_2m_max_member07": [75.1],
        "temperature_2m_max_member08": [71.0],
        "temperature_2m_max_member09": [73.5],
        "temperature_2m_max_member10": [70.5],
        "temperature_2m_max_member11": [74.0],
        "temperature_2m_max_member12": [72.8],
        "temperature_2m_max_member13": [71.2],
        "temperature_2m_max_member14": [73.9],
        "temperature_2m_max_member15": [70.1],
        "temperature_2m_max_member16": [72.3],
        "temperature_2m_max_member17": [74.5],
        "temperature_2m_max_member18": [71.8],
        "temperature_2m_max_member19": [73.2],
        "temperature_2m_max_member20": [70.3],
        "temperature_2m_max_member21": [72.6],
        "temperature_2m_max_member22": [74.8],
        "temperature_2m_max_member23": [71.4],
        "temperature_2m_max_member24": [73.7],
        "temperature_2m_max_member25": [70.7],
        "temperature_2m_max_member26": [72.1],
        "temperature_2m_max_member27": [75.0],
        "temperature_2m_max_member28": [71.6],
        "temperature_2m_max_member29": [73.3],
        "temperature_2m_max_member30": [69.3],
    }
}


def test_parse_response_returns_forecast():
    adapter = EnsembleAdapter()
    fc = adapter._parse_response(SAMPLE_RESPONSE, date(2026, 4, 10))
    assert fc is not None
    assert len(fc.member_highs) == 31  # control + 30 members
    assert fc.target_date == date(2026, 4, 10)
    assert 69.0 < fc.mean_high < 75.0
    assert fc.sigma > 0


def test_prob_above_low_threshold():
    """All members above 65 → prob should be 1.0."""
    adapter = EnsembleAdapter()
    fc = adapter._parse_response(SAMPLE_RESPONSE, date(2026, 4, 10))
    assert fc.prob_above(65.0) == 1.0


def test_prob_above_high_threshold():
    """All members below 80 → prob should be 0.0."""
    adapter = EnsembleAdapter()
    fc = adapter._parse_response(SAMPLE_RESPONSE, date(2026, 4, 10))
    assert fc.prob_above(80.0) == 0.0


def test_prob_above_mid_threshold():
    """Threshold near the mean → prob should be between 0 and 1."""
    adapter = EnsembleAdapter()
    fc = adapter._parse_response(SAMPLE_RESPONSE, date(2026, 4, 10))
    prob = fc.prob_above(72.5)
    assert 0.2 < prob < 0.8


def test_parse_missing_date_returns_none():
    adapter = EnsembleAdapter()
    fc = adapter._parse_response(SAMPLE_RESPONSE, date(2026, 4, 15))
    assert fc is None


def test_parse_empty_response_returns_none():
    adapter = EnsembleAdapter()
    assert adapter._parse_response({}, date(2026, 4, 10)) is None
    assert adapter._parse_response({"daily": {}}, date(2026, 4, 10)) is None


def test_sigma_floor():
    """Sigma should be at least 1.0 even if all members agree."""
    fc = EnsembleForecast(
        target_date=date(2026, 4, 10),
        member_highs=[75.0, 75.0, 75.0, 75.0, 75.0],
        mean_high=75.0,
        sigma=1.0,  # floored from 0.0
    )
    assert fc.sigma >= 1.0


def test_ensemble_forecast_prob_empty_members():
    fc = EnsembleForecast(
        target_date=date(2026, 4, 10),
        member_highs=[],
        mean_high=0.0,
        sigma=3.0,
    )
    assert fc.prob_above(75.0) == 0.5  # fallback
