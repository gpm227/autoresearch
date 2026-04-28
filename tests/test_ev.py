"""Tests for expected value calculation."""
import pytest
from infra import calculate_ev, TradeParams


def test_positive_ev_clear_edge():
    """When p_hat >> price, EV should be clearly positive."""
    params = TradeParams(
        p_hat=0.80,          # estimated probability
        price_cents=60,      # market price (60 cents)
        contracts=10,
        fee_per_contract=0.02,
        slippage_cents=0.5,
    )
    ev = calculate_ev(params)
    assert ev > 0


def test_negative_ev_no_edge():
    """When p_hat matches price, EV is negative (fees eat it)."""
    params = TradeParams(
        p_hat=0.60,
        price_cents=60,
        contracts=10,
        fee_per_contract=0.02,
        slippage_cents=0.5,
    )
    ev = calculate_ev(params)
    assert ev < 0


def test_ev_scales_with_contracts():
    """EV per contract is consistent regardless of size."""
    params1 = TradeParams(p_hat=0.80, price_cents=60, contracts=1,
                          fee_per_contract=0.02, slippage_cents=0.5)
    params10 = TradeParams(p_hat=0.80, price_cents=60, contracts=10,
                           fee_per_contract=0.02, slippage_cents=0.5)
    ev1 = calculate_ev(params1)
    ev10 = calculate_ev(params10)
    assert abs(ev10 / ev1 - 10) < 0.01
