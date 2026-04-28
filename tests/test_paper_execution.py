import pytest
import sqlite3
from execution import _create_tables, _migrate_market_observations
from signals import TradeSignal


def _make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _create_tables(db)
    _migrate_market_observations(db)
    return db


def _make_signal(contract_id="KXHIGHDEN-26APR10-T80", side="yes", edge=0.15, market_price=0.55):
    return TradeSignal(
        contract_id=contract_id,
        market_id="KXHIGHDEN-26APR10",
        side=side,
        market_price=market_price,
        model_prob=0.70,
        edge=edge,
        confidence=0.75,
        spread=0.04,
        reason="test",
    )


def test_open_paper_position():
    from execution import open_paper_position
    db = _make_db()
    sig = _make_signal()
    pos_id = open_paper_position(db, sig, bankroll=340.00)
    assert pos_id is not None
    row = db.execute("SELECT * FROM paper_positions WHERE id=?", (pos_id,)).fetchone()
    assert row["contract_id"] == "KXHIGHDEN-26APR10-T80"
    assert row["side"] == "yes"
    assert row["status"] == "open"
    assert row["entry_price"] > 0
    assert row["max_loss"] <= 340.00 * 0.02 + 0.01


def test_settle_paper_position_win():
    from execution import open_paper_position, settle_paper_position
    db = _make_db()
    sig = _make_signal(side="yes", market_price=0.55)
    pos_id = open_paper_position(db, sig, bankroll=340.00)
    pnl = settle_paper_position(db, pos_id, settlement_price=1.0)
    assert pnl > 0
    row = db.execute("SELECT * FROM paper_positions WHERE id=?", (pos_id,)).fetchone()
    assert row["status"] == "settled"
    assert row["realized_pnl"] == pnl


def test_settle_paper_position_loss():
    from execution import open_paper_position, settle_paper_position
    db = _make_db()
    sig = _make_signal(side="yes", market_price=0.55)
    pos_id = open_paper_position(db, sig, bankroll=340.00)
    pnl = settle_paper_position(db, pos_id, settlement_price=0.0)
    assert pnl < 0
    row = db.execute("SELECT * FROM paper_positions WHERE id=?", (pos_id,)).fetchone()
    assert row["status"] == "settled"


def test_get_open_exposure():
    from execution import open_paper_position, get_open_paper_exposure
    db = _make_db()
    sig1 = _make_signal(contract_id="A")
    sig2 = _make_signal(contract_id="B")
    open_paper_position(db, sig1, bankroll=340.00)
    open_paper_position(db, sig2, bankroll=340.00)
    exposure = get_open_paper_exposure(db)
    assert exposure > 0


def test_get_held_contracts():
    from execution import open_paper_position, get_held_contract_ids
    db = _make_db()
    sig = _make_signal(contract_id="KXHIGHDEN-26APR10-T80")
    open_paper_position(db, sig, bankroll=340.00)
    held = get_held_contract_ids(db)
    assert "KXHIGHDEN-26APR10-T80" in held
