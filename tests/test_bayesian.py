"""Tests for Bayesian signal processing."""
import pytest
import time
from infra import BayesianEngine, Signal


def test_neutral_prior():
    engine = BayesianEngine()
    market_id = "TEST-MARKET-1"
    p, conf = engine.posterior(market_id)
    assert p == pytest.approx(0.5, abs=1e-9)
    assert conf == pytest.approx(0.5, abs=0.1)


def test_positive_signal_updates_probability_upward():
    engine = BayesianEngine()
    market_id = "TEST-MARKET-2"
    signal = Signal(
        market_id=market_id,
        source="ap",
        headline="Court rules in favor of YES outcome",
        body="The court has definitively ruled.",
        sentiment=0.8,
        magnitude=0.7,
        timestamp=time.time(),
    )
    engine.update(signal)
    p, conf = engine.posterior(market_id)
    assert p > 0.5


def test_duplicate_signals_deduplicated():
    """Same headline within cluster window counts once."""
    engine = BayesianEngine(cluster_window_sec=600)
    market_id = "TEST-MARKET-3"
    headline = "Court rules in favor"
    body = "The court has definitively ruled on the matter."

    for _ in range(5):
        signal = Signal(
            market_id=market_id,
            source="ap",
            headline=headline,
            body=body,
            sentiment=0.8,
            magnitude=0.7,
            timestamp=time.time(),
        )
        engine.update(signal)

    p_deduped, _ = engine.posterior(market_id)

    # Fresh engine, one signal only
    engine2 = BayesianEngine(cluster_window_sec=600)
    engine2.update(Signal(
        market_id=market_id,
        source="reuters",
        headline=headline,
        body=body,
        sentiment=0.8,
        magnitude=0.7,
        timestamp=time.time(),
    ))
    p_single, _ = engine2.posterior(market_id)
    assert abs(p_deduped - p_single) < 0.01  # nearly identical


def test_negative_signal_downweights():
    engine = BayesianEngine()
    market_id = "TEST-MARKET-4"
    signal = Signal(
        market_id=market_id,
        source="reuters",
        headline="Ruling reversed, NO outcome confirmed",
        body="The appellate court reversed the prior ruling.",
        sentiment=-0.8,
        magnitude=0.7,
        timestamp=time.time(),
    )
    engine.update(signal)
    p, _ = engine.posterior(market_id)
    assert p < 0.5
