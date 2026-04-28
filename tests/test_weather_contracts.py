import pytest
from weather_contracts import parse_weather_contract, WeatherContract


def test_parse_denver_high_temp_above():
    market = {
        "ticker": "KXHIGHDEN-26APR10-T82",
        "title": "Will the high temperature in Denver be 82°F or above on April 10?",
        "event_ticker": "KXHIGHDEN-26APR10",
        "close_time": "2026-04-11T00:00:00Z",
    }
    c = parse_weather_contract(market)
    assert c.is_priceable is True
    assert c.city == "Denver"
    assert c.station_id == "KDEN"
    assert c.threshold_f == 82.0
    assert c.metric == "high_temp"


def test_parse_non_denver_unpriceable():
    market = {
        "ticker": "KXHIGHLAX-26APR10-T90",
        "title": "Will the high temperature in Los Angeles be 90°F or above on April 10?",
        "event_ticker": "KXHIGHLAX-26APR10",
        "close_time": "2026-04-11T00:00:00Z",
    }
    c = parse_weather_contract(market)
    assert c.is_priceable is False
    assert "city" in c.parse_status.lower() or "denver" in c.parse_status.lower()


def test_parse_low_temp_unpriceable():
    market = {
        "ticker": "KXLOWDEN-26APR10-T30",
        "title": "Will the low temperature in Denver be 30°F or below on April 10?",
        "event_ticker": "KXLOWDEN-26APR10",
        "close_time": "2026-04-11T00:00:00Z",
    }
    c = parse_weather_contract(market)
    assert c.is_priceable is False
    assert "metric" in c.parse_status.lower() or "high" in c.parse_status.lower()


def test_parse_threshold_extraction_variants():
    market = {
        "ticker": "KXHIGHDEN-26APR10-T75",
        "title": "Will the high temperature in Denver reach 75°F on April 10, 2026?",
        "event_ticker": "KXHIGHDEN-26APR10",
        "close_time": "2026-04-11T00:00:00Z",
    }
    c = parse_weather_contract(market)
    assert c.is_priceable is True
    assert c.threshold_f == 75.0


def test_parse_ambiguous_title_unpriceable():
    market = {
        "ticker": "KXWEATHERDEN-26APR10",
        "title": "Denver weather something something",
        "event_ticker": "KXWEATHERDEN-26APR10",
        "close_time": "2026-04-11T00:00:00Z",
    }
    c = parse_weather_contract(market)
    assert c.is_priceable is False


def test_parse_expiry_extraction():
    market = {
        "ticker": "KXHIGHDEN-26APR10-T80",
        "title": "Will the high temperature in Denver be 80°F or above on April 10?",
        "event_ticker": "KXHIGHDEN-26APR10",
        "close_time": "2026-04-11T00:00:00Z",
    }
    c = parse_weather_contract(market)
    assert c.expiry_ts is not None


def test_parse_real_kalshi_above_format():
    """Real Kalshi format: 'Will the **high temp in Denver** be >79° on Apr 11, 2026?'"""
    market = {
        "ticker": "KXHIGHDEN-26APR11-T79",
        "title": "Will the **high temp in Denver** be >79° on Apr 11, 2026?",
        "event_ticker": "KXHIGHDEN-26APR11",
        "close_time": "2026-04-12T00:00:00Z",
    }
    c = parse_weather_contract(market)
    assert c.is_priceable is True
    assert c.threshold_f == 79.0
    assert c.city == "Denver"


def test_parse_real_kalshi_below_format():
    """Real Kalshi format: 'Will the **high temp in Denver** be <72° on Apr 11, 2026?'"""
    market = {
        "ticker": "KXHIGHDEN-26APR11-T72",
        "title": "Will the **high temp in Denver** be <72° on Apr 11, 2026?",
        "event_ticker": "KXHIGHDEN-26APR11",
        "close_time": "2026-04-12T00:00:00Z",
    }
    c = parse_weather_contract(market)
    assert c.is_priceable is True
    assert c.threshold_f == 72.0


def test_parse_bracket_contract_unpriceable():
    """Bracket contracts like '78-79°' should be unpriceable."""
    market = {
        "ticker": "KXHIGHDEN-26APR11-B78.5",
        "title": "Will the **high temp in Denver** be 78-79° on Apr 11, 2026?",
        "event_ticker": "KXHIGHDEN-26APR11",
        "close_time": "2026-04-12T00:00:00Z",
    }
    c = parse_weather_contract(market)
    assert c.is_priceable is False
    assert "bracket" in c.parse_status.lower() or "range" in c.parse_status.lower()
