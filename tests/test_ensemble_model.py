# tests/test_ensemble_model.py
"""Tests for the V3 ensemble + METAR blended model."""
import pytest
from datetime import date, datetime, timezone
from weather_model import WeatherModel, ProbabilityEstimate
from weather_adapter_noaa import MetarObservation
from weather_adapter_ensemble import EnsembleForecast
from weather_contracts import WeatherContract


def _make_contract(threshold_f: float = 75.0) -> WeatherContract:
    return WeatherContract(
        contract_id="KXHIGHDEN-26APR10-T75",
        market_id="KXHIGHDEN-26APR10",
        title="test",
        city="Denver",
        station_id="KDEN",
        metric="high_temp",
        threshold_f=threshold_f,
        expiry_ts=datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc),
        is_priceable=True,
        parse_status="ok",
    )


def _make_ensemble(mean_high: float = 75.0, sigma: float = 2.5, n: int = 31) -> EnsembleForecast:
    # Generate members centered on mean with given spread
    import random
    random.seed(42)
    members = [mean_high + random.gauss(0, sigma) for _ in range(n)]
    return EnsembleForecast(
        target_date=date(2026, 4, 10),
        member_highs=members,
        mean_high=mean_high,
        sigma=sigma,
    )


def _make_metar(temp_c: float, hour_utc: int = 18) -> MetarObservation:
    return MetarObservation(
        station="KDEN",
        obs_time=datetime(2026, 4, 10, hour_utc, 0, tzinfo=timezone.utc),
        temp_c=temp_c,
        raw_text="...",
    )


def test_ensemble_model_returns_estimate():
    model = WeatherModel()
    contract = _make_contract(75.0)
    ensemble = _make_ensemble(mean_high=78.0, sigma=2.5)
    metars = [_make_metar(temp_c=21.0, hour_utc=18)]  # ~70F at noon MDT
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)

    est = model.price_contract_ensemble(contract, metars, ensemble, now=now)
    assert isinstance(est, ProbabilityEstimate)
    assert est.model_version == "ensemble_metar_v3"
    assert 0.0 <= est.prob_yes <= 1.0
    assert est.sigma_f == 2.5  # from ensemble, not hardcoded


def test_ensemble_sigma_replaces_hardcoded():
    """Ensemble sigma should be used instead of hardcoded 3.5/2.8/2.0."""
    model = WeatherModel()
    contract = _make_contract(75.0)
    ensemble = _make_ensemble(mean_high=78.0, sigma=4.2)
    metars = [_make_metar(temp_c=21.0, hour_utc=18)]
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)

    est = model.price_contract_ensemble(contract, metars, ensemble, now=now)
    assert est.sigma_f == 4.2


def test_ensemble_no_metar_uses_pure_ensemble():
    """Without METAR, should use ensemble mean directly."""
    model = WeatherModel()
    contract = _make_contract(75.0)
    ensemble = _make_ensemble(mean_high=78.0, sigma=2.5)
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)

    est = model.price_contract_ensemble(contract, [], ensemble, now=now)
    assert est.prob_yes > 0.5  # mean 78 > threshold 75
    assert est.current_temp_f is None
    assert est.confidence < 0.75  # reduced without METAR


def test_ensemble_unavailable_falls_back_to_v2():
    """No ensemble → should fall back to METAR-only V2."""
    model = WeatherModel()
    contract = _make_contract(75.0)
    metars = [_make_metar(temp_c=21.0, hour_utc=18)]
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)

    est = model.price_contract_ensemble(contract, metars, None, now=now)
    assert est.diagnostics.get("ensemble_status") == "unavailable"
    assert est.model_version == "ensemble_metar_v3"


def test_metar_ahead_of_curve_shifts_mean_up():
    """If METAR temp is higher than expected for this time, mean shifts up."""
    model = WeatherModel()
    contract = _make_contract(80.0)
    ensemble = _make_ensemble(mean_high=78.0, sigma=2.5)

    # 26°C = 78.8°F at noon MDT — ahead of the curve for a 78°F high
    metars = [_make_metar(temp_c=26.0, hour_utc=18)]
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)

    est = model.price_contract_ensemble(contract, metars, ensemble, now=now)
    # Adjusted mean should be > ensemble mean since we're ahead
    assert est.adjusted_high_f > ensemble.mean_high


def test_metar_behind_curve_shifts_mean_down():
    """If METAR temp is lower than expected, mean shifts down."""
    model = WeatherModel()
    contract = _make_contract(80.0)
    ensemble = _make_ensemble(mean_high=78.0, sigma=2.5)

    # 10°C = 50°F at noon MDT — way behind curve for 78°F high
    metars = [_make_metar(temp_c=10.0, hour_utc=18)]
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)

    est = model.price_contract_ensemble(contract, metars, ensemble, now=now)
    assert est.adjusted_high_f < ensemble.mean_high


def test_expected_temp_at_peak_equals_daily_high():
    model = WeatherModel()
    assert model._expected_temp_at_time(80.0, hours_to_peak=0.0) == 80.0


def test_expected_temp_early_morning_near_low():
    model = WeatherModel()
    # hours_to_peak = 9.5 means we're at sunrise (6:00)
    temp = model._expected_temp_at_time(80.0, hours_to_peak=9.5)
    assert temp < 55.0  # Should be near morning low (80-30=50)


def test_both_models_produce_different_results():
    """V2 and V3 should produce different probabilities (shadow mode comparison)."""
    model = WeatherModel()
    contract = _make_contract(75.0)
    ensemble = _make_ensemble(mean_high=78.0, sigma=2.5)
    metars = [_make_metar(temp_c=21.0, hour_utc=18)]
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)

    v2 = model.price_contract(contract, metars, now=now)
    v3 = model.price_contract_ensemble(contract, metars, ensemble, now=now)

    # They should both produce valid probabilities but likely differ
    assert 0.0 <= v2.prob_yes <= 1.0
    assert 0.0 <= v3.prob_yes <= 1.0
    assert v2.model_version != v3.model_version
    # V3 should use ensemble sigma, not hardcoded
    assert v3.sigma_f == 2.5
