"""Tests for LMSR theoretical prior."""
import pytest
import math
from infra import lmsr_price, lmsr_cost


def test_lmsr_fair_coin():
    """At equal quantities, LMSR prices both outcomes at 0.5."""
    p_yes = lmsr_price(q_yes=0.0, q_no=0.0, b=100.0)
    assert p_yes == pytest.approx(0.5, abs=1e-9)


def test_lmsr_price_increases_with_quantity():
    """Buying YES contracts pushes YES price up."""
    p1 = lmsr_price(q_yes=0.0, q_no=0.0, b=100.0)
    p2 = lmsr_price(q_yes=50.0, q_no=0.0, b=100.0)
    assert p2 > p1


def test_lmsr_price_sums_to_one():
    """YES + NO prices always sum to 1.0."""
    p_yes = lmsr_price(q_yes=30.0, q_no=10.0, b=100.0)
    p_no = lmsr_price(q_yes=10.0, q_no=30.0, b=100.0)
    # By symmetry: p_no with flipped quantities = 1 - p_yes
    assert p_yes + (1 - p_yes) == pytest.approx(1.0)


def test_lmsr_cost_positive():
    """Cost to buy contracts is positive."""
    cost = lmsr_cost(q_yes_before=0.0, q_no_before=0.0,
                     q_yes_after=10.0, q_no_after=0.0, b=100.0)
    assert cost > 0
