# tests/test_signals.py
import pytest
from datetime import datetime, timezone
from signals import generate_signal, TradeSignal
from weather_model import ProbabilityEstimate


def _make_estimate(prob_yes: float = 0.75, confidence: float = 0.75) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        contract_id="KXHIGHDEN-26APR10-T80",
        prob_yes=prob_yes,
        confidence=confidence,
        expected_high_f=83.0,
        adjusted_high_f=83.0,
        sigma_f=2.5,
        current_temp_f=74.0,
        diagnostics={"hours_to_peak": 3.0},
        model_version="intraday_metar_v1",
    )


def test_signal_generated_when_edge_sufficient():
    est = _make_estimate(prob_yes=0.75, confidence=0.75)
    sig = generate_signal(
        estimate=est, market_yes_price=0.55, market_no_price=0.45,
        spread=0.04, hours_to_peak=3.0, held_contracts=set(),
        risk_ok=True,
    )
    assert sig is not None
    assert sig.side == "yes"
    assert abs(sig.edge - 0.20) < 0.01


def test_no_signal_when_edge_too_small():
    est = _make_estimate(prob_yes=0.60)
    sig = generate_signal(
        estimate=est, market_yes_price=0.55, market_no_price=0.45,
        spread=0.04, hours_to_peak=3.0, held_contracts=set(),
        risk_ok=True,
    )
    assert sig is None


def test_no_signal_when_confidence_too_low():
    est = _make_estimate(prob_yes=0.80, confidence=0.50)
    sig = generate_signal(
        estimate=est, market_yes_price=0.55, market_no_price=0.45,
        spread=0.04, hours_to_peak=3.0, held_contracts=set(),
        risk_ok=True,
    )
    assert sig is None


def test_no_signal_when_spread_too_wide():
    est = _make_estimate(prob_yes=0.80, confidence=0.75)
    sig = generate_signal(
        estimate=est, market_yes_price=0.50, market_no_price=0.40,
        spread=0.10, hours_to_peak=3.0, held_contracts=set(),
        risk_ok=True,
    )
    assert sig is None


def test_no_signal_when_too_close_to_peak():
    est = _make_estimate(prob_yes=0.80)
    sig = generate_signal(
        estimate=est, market_yes_price=0.55, market_no_price=0.45,
        spread=0.04, hours_to_peak=0.5, held_contracts=set(),
        risk_ok=True,
    )
    assert sig is None


def test_no_signal_when_risk_not_ok():
    est = _make_estimate(prob_yes=0.80)
    sig = generate_signal(
        estimate=est, market_yes_price=0.55, market_no_price=0.45,
        spread=0.04, hours_to_peak=3.0, held_contracts=set(),
        risk_ok=False,
    )
    assert sig is None


def test_no_signal_when_already_holding():
    est = _make_estimate(prob_yes=0.80)
    sig = generate_signal(
        estimate=est, market_yes_price=0.55, market_no_price=0.45,
        spread=0.04, hours_to_peak=3.0,
        held_contracts={"KXHIGHDEN-26APR10-T80"},
        risk_ok=True,
    )
    assert sig is None


def test_no_side_when_model_near_fifty():
    est = _make_estimate(prob_yes=0.52)
    sig = generate_signal(
        estimate=est, market_yes_price=0.50, market_no_price=0.50,
        spread=0.02, hours_to_peak=3.0, held_contracts=set(),
        risk_ok=True,
    )
    assert sig is None


def test_signal_no_side_when_no_edge_larger():
    est = _make_estimate(prob_yes=0.20, confidence=0.75)
    sig = generate_signal(
        estimate=est, market_yes_price=0.55, market_no_price=0.45,
        spread=0.04, hours_to_peak=3.0, held_contracts=set(),
        risk_ok=True,
    )
    assert sig is not None
    assert sig.side == "no"
    assert abs(sig.edge - 0.35) < 0.01


def test_no_signal_when_expected_high_near_threshold():
    """Skip trades when model expected high is within 1°F of threshold — noise dominates."""
    # expected_high_f=83.0 and threshold=83.5 → distance 0.5 < 1.0 → skip
    est = _make_estimate(prob_yes=0.75, confidence=0.75)
    sig = generate_signal(
        estimate=est, market_yes_price=0.55, market_no_price=0.45,
        spread=0.04, hours_to_peak=3.0, held_contracts=set(),
        risk_ok=True, threshold_f=83.5,
    )
    assert sig is None


def test_signal_when_expected_high_far_from_threshold():
    """Allow trades when distance from threshold is meaningful."""
    # expected_high_f=83.0 and threshold=80.0 → distance 3.0 > 1.0 → allow
    est = _make_estimate(prob_yes=0.75, confidence=0.75)
    sig = generate_signal(
        estimate=est, market_yes_price=0.55, market_no_price=0.45,
        spread=0.04, hours_to_peak=3.0, held_contracts=set(),
        risk_ok=True, threshold_f=80.0,
    )
    assert sig is not None
