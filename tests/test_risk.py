"""Tests for risk engine and position sizing."""
import pytest
from infra import RiskEngine, TradingPhase


def test_half_kelly_sizing():
    """Half-Kelly: f* = 0.5 * (b*p - q) / b."""
    engine = RiskEngine(bankroll_cents=10000 * 100)  # $10,000
    contracts = engine.position_size(p_hat=0.80, price_cents=60)
    assert contracts > 0
    assert contracts <= 333  # 2% of $10k at 60c/contract


def test_kill_switch_activates_on_drawdown():
    """15% rolling drawdown halts trading."""
    engine = RiskEngine(bankroll_cents=10000 * 100)
    engine.record_loss(cents=1500 * 100)
    assert engine.kill_switch_active()


def test_kill_switch_not_triggered_on_small_loss():
    engine = RiskEngine(bankroll_cents=10000 * 100)
    engine.record_loss(cents=100 * 100)  # $100 loss on $10k = 1%
    assert not engine.kill_switch_active()


def test_portfolio_exposure_cap():
    """Never exceed 25% of bankroll in open positions."""
    engine = RiskEngine(bankroll_cents=10000 * 100)
    engine.add_open_position("MKT-1", value_cents=2400 * 100)
    assert not engine.can_open_position(value_cents=200 * 100)


def test_phase_detection():
    """Phase auto-detected from bankroll."""
    e1 = RiskEngine(bankroll_cents=500 * 100)
    assert e1.phase == TradingPhase.PROVE

    e2 = RiskEngine(bankroll_cents=10000 * 100)
    assert e2.phase == TradingPhase.SCALE

    e3 = RiskEngine(bankroll_cents=30000 * 100)
    assert e3.phase == TradingPhase.INCOME
