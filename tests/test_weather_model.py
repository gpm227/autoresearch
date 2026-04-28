# tests/test_weather_model.py
import pytest
from datetime import datetime, timezone
from weather_model import WeatherModel, ProbabilityEstimate
from weather_adapter_noaa import MetarObservation
from weather_contracts import WeatherContract


def _make_contract(threshold_f: float = 80.0) -> WeatherContract:
    return WeatherContract(
        contract_id="KXHIGHDEN-26APR10-T80",
        market_id="KXHIGHDEN-26APR10",
        title="Will the high temperature in Denver be 80°F or above on April 10?",
        city="Denver",
        station_id="KDEN",
        metric="high_temp",
        threshold_f=threshold_f,
        expiry_ts=datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc),
        is_priceable=True,
        parse_status="ok",
    )


def _make_metar(temp_c: float, hour_utc: int = 18) -> MetarObservation:
    return MetarObservation(
        station="KDEN",
        obs_time=datetime(2026, 4, 10, hour_utc, 0, tzinfo=timezone.utc),
        temp_c=temp_c,
        raw_text="...",
    )


def test_projected_gain_basic():
    model = WeatherModel()
    gain = model._projected_gain(hours_to_peak=3.0, recent_trend_f=None)
    assert abs(gain - 4.5) < 0.01


def test_projected_gain_with_hot_trend():
    model = WeatherModel()
    gain = model._projected_gain(hours_to_peak=3.0, recent_trend_f=3.0)
    assert gain > 4.5


def test_projected_gain_with_cold_trend():
    model = WeatherModel()
    gain = model._projected_gain(hours_to_peak=3.0, recent_trend_f=0.5)
    assert gain < 4.5


def test_sigma_varies_by_hours_to_peak():
    model = WeatherModel()
    assert model._sigma(hours_to_peak=6.0) == 3.5
    assert model._sigma(hours_to_peak=4.0) == 2.8
    assert model._sigma(hours_to_peak=2.0) == 2.0


def test_probability_threshold_well_below_expected():
    model = WeatherModel()
    prob = model._compute_probability(expected_high_f=85.0, sigma_f=2.0, threshold_f=75.0)
    assert prob > 0.99


def test_probability_threshold_well_above_expected():
    model = WeatherModel()
    prob = model._compute_probability(expected_high_f=75.0, sigma_f=2.0, threshold_f=85.0)
    assert prob < 0.01


def test_probability_threshold_at_expected():
    model = WeatherModel()
    prob = model._compute_probability(expected_high_f=80.0, sigma_f=2.5, threshold_f=80.0)
    assert 0.48 < prob < 0.52


def test_price_contract_returns_estimate():
    model = WeatherModel()
    contract = _make_contract(threshold_f=80.0)
    metars = [_make_metar(temp_c=21.1, hour_utc=18)]
    now = datetime(2026, 4, 10, 18, 30, tzinfo=timezone.utc)
    est = model.price_contract(contract, metars, now=now)
    assert isinstance(est, ProbabilityEstimate)
    assert 0.0 <= est.prob_yes <= 1.0
    assert est.model_version == "intraday_metar_v1"
    assert est.current_temp_f is not None
    assert est.sigma_f > 0


def test_price_contract_no_metars_returns_low_confidence():
    model = WeatherModel()
    contract = _make_contract()
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)
    est = model.price_contract(contract, [], now=now)
    assert est.confidence < 0.5


def test_price_contract_stale_metar_returns_low_confidence():
    model = WeatherModel()
    contract = _make_contract()
    old_metar = _make_metar(temp_c=20.0, hour_utc=14)
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)
    est = model.price_contract(contract, [old_metar], now=now)
    assert est.confidence < 0.5
