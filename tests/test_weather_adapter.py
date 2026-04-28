# tests/test_weather_adapter.py
import pytest
from datetime import datetime, timezone
from weather_adapter_noaa import WeatherAdapterNOAA, MetarObservation


# Sample NOAA JSON response (one METAR entry)
SAMPLE_METAR_JSON = [
    {
        "icaoId": "KDEN",
        "obsTime": 1744214400,
        "reportTime": "2026-04-09T14:00:00Z",
        "temp": 18.3,
        "dewp": -2.8,
        "wdir": 180,
        "wspd": 12,
        "visib": "10+",
        "rawOb": "KDEN 091400Z 18012KT 10SM FEW120 18/M03 A3012",
        "name": "Denver Intl",
        "lat": 39.85,
        "lon": -104.66,
        "elev": 1656,
    }
]


def test_parse_metars_from_json():
    adapter = WeatherAdapterNOAA()
    results = adapter._parse_metars(SAMPLE_METAR_JSON)
    assert len(results) == 1
    m = results[0]
    assert isinstance(m, MetarObservation)
    assert m.station == "KDEN"
    assert m.temp_c == 18.3
    assert m.obs_time.tzinfo is not None


def test_parse_metars_skips_missing_temp():
    bad = [{"icaoId": "KDEN", "obsTime": 1744214400, "rawOb": "..."}]
    adapter = WeatherAdapterNOAA()
    results = adapter._parse_metars(bad)
    assert len(results) == 0


def test_parse_metars_empty_input():
    adapter = WeatherAdapterNOAA()
    assert adapter._parse_metars([]) == []
    assert adapter._parse_metars(None) == []


def test_temp_c_to_f():
    adapter = WeatherAdapterNOAA()
    assert abs(adapter.c_to_f(0) - 32.0) < 0.01
    assert abs(adapter.c_to_f(100) - 212.0) < 0.01
    assert abs(adapter.c_to_f(18.3) - 64.94) < 0.01


def test_compute_recent_trend_needs_2_obs():
    adapter = WeatherAdapterNOAA()
    m1 = MetarObservation(
        station="KDEN",
        obs_time=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        temp_c=15.0,
        raw_text="...",
    )
    assert adapter.compute_recent_trend_f([m1]) is None


def test_compute_recent_trend_two_obs():
    adapter = WeatherAdapterNOAA()
    m1 = MetarObservation(
        station="KDEN",
        obs_time=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        temp_c=15.0,
        raw_text="...",
    )
    m2 = MetarObservation(
        station="KDEN",
        obs_time=datetime(2026, 4, 9, 14, 0, tzinfo=timezone.utc),
        temp_c=18.0,
        raw_text="...",
    )
    trend = adapter.compute_recent_trend_f([m1, m2])
    assert trend is not None
    assert abs(trend - 2.7) < 0.1
