"""Tests for order book microstructure analysis."""
import pytest
from infra import OrderBook


def test_weighted_mid_balanced_book():
    """Balanced book → mid is arithmetic mean of best bid/ask."""
    book = OrderBook(
        yes_bids=[(50, 100), (49, 200)],   # (price_cents, size)
        yes_asks=[(51, 100), (52, 200)],
    )
    mid = book.weighted_mid()
    assert mid == pytest.approx(50.5, abs=0.5)


def test_weighted_mid_imbalanced_buy_pressure():
    """Heavy bid side → mid pulled above arithmetic mean."""
    book = OrderBook(
        yes_bids=[(50, 1000)],
        yes_asks=[(51, 10)],
    )
    mid = book.weighted_mid()
    assert mid > 50.5  # pulled toward ask due to buy pressure


def test_spread():
    book = OrderBook(
        yes_bids=[(48, 100)],
        yes_asks=[(53, 100)],
    )
    assert book.spread_cents() == 5


def test_depth_at_ticks():
    """Depth within N ticks of mid on each side."""
    book = OrderBook(
        yes_bids=[(50, 100), (49, 200), (45, 500)],
        yes_asks=[(51, 100), (52, 200), (56, 500)],
    )
    depth = book.depth_at_ticks(n=2)
    # Only bids at 50, 49 and asks at 51, 52 within 2 ticks
    assert depth["bid"] == pytest.approx(300.0)
    assert depth["ask"] == pytest.approx(300.0)


def test_empty_book_raises():
    book = OrderBook(yes_bids=[], yes_asks=[])
    with pytest.raises(ValueError):
        book.weighted_mid()
