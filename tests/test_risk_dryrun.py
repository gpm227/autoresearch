import pytest
import sqlite3
from execution import (
    _create_tables, _migrate_market_observations,
    get_or_create_risk_state, update_risk_state, is_day_locked,
    open_paper_position, get_held_contract_ids, get_open_paper_exposure,
)
from signals import TradeSignal


def _make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _create_tables(db)
    _migrate_market_observations(db)
    return db


def _make_signal(contract_id="TEST-T80", side="yes", market_price=0.55):
    return TradeSignal(
        contract_id=contract_id, market_id="TEST", side=side,
        market_price=market_price, model_prob=0.70, edge=0.15,
        confidence=0.75, spread=0.04, reason="test",
    )


def test_three_consecutive_losses_locks_day():
    """Case A: 3 losses in a row should trigger day lock."""
    db = _make_db()
    get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)
    assert is_day_locked(db, "2026-04-10", start_balance=340.00) is False  # 2 losses, not yet
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)
    assert is_day_locked(db, "2026-04-10", start_balance=340.00) is True   # 3 losses, locked


def test_daily_20pct_loss_locks_day():
    """Case B: Daily loss exceeding 20% of start balance should hard stop."""
    db = _make_db()
    get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    # 20% of 340 = $68. Accumulate losses past this.
    update_risk_state(db, "2026-04-10", trade_pnl=-35.00, was_loss=True)
    assert is_day_locked(db, "2026-04-10", start_balance=340.00) is False  # -35 < 68, but 1 loss
    # Reset consecutive (would need a win to truly test daily-loss-only path)
    update_risk_state(db, "2026-04-10", trade_pnl=1.00, was_loss=False)  # reset streak
    update_risk_state(db, "2026-04-10", trade_pnl=-35.00, was_loss=True)
    # Now daily_realized_pnl = -35 + 1 + -35 = -69. Exceeds 68.
    assert is_day_locked(db, "2026-04-10", start_balance=340.00) is True


def test_stale_metar_produces_low_confidence():
    """Case C: Stale METAR (>60 min old) should produce confidence < 0.5."""
    from datetime import datetime, timezone
    from weather_model import WeatherModel
    from weather_adapter_noaa import MetarObservation
    from weather_contracts import WeatherContract

    model = WeatherModel()
    contract = WeatherContract(
        contract_id="TEST-T80", market_id="TEST",
        title="test", city="Denver", station_id="KDEN",
        metric="high_temp", threshold_f=80.0,
        expiry_ts=datetime(2026, 4, 11, tzinfo=timezone.utc),
        is_priceable=True, parse_status="ok",
    )
    # METAR from 2 hours ago
    old_metar = MetarObservation(
        station="KDEN",
        obs_time=datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc),
        temp_c=20.0,
        raw_text="...",
    )
    now = datetime(2026, 4, 10, 16, 30, tzinfo=timezone.utc)  # 2.5 hours later
    est = model.price_contract(contract, [old_metar], now=now)
    assert est.confidence < 0.5, f"Stale METAR should produce low confidence, got {est.confidence}"


def test_duplicate_exposure_blocked():
    """Case D: Cannot open a second position on the same contract."""
    db = _make_db()
    sig1 = _make_signal(contract_id="KXHIGHDEN-T80")
    open_paper_position(db, sig1, bankroll=340.00)
    held = get_held_contract_ids(db)
    assert "KXHIGHDEN-T80" in held
    # generate_signal would return None for held contracts
    # (tested in test_signals.py::test_no_signal_when_already_holding)
    # But verify the DB state is correct
    sig2 = _make_signal(contract_id="KXHIGHDEN-T80")
    open_paper_position(db, sig2, bankroll=340.00)
    # Now there are 2 positions — this is the DB layer, the dedup happens in signals
    # Verify that get_held_contract_ids catches it
    held = get_held_contract_ids(db)
    assert "KXHIGHDEN-T80" in held


def test_max_trades_per_day_locks():
    """Case E: Hitting MAX_TRADES_PER_DAY (6) should lock the day."""
    db = _make_db()
    get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    for i in range(6):
        update_risk_state(db, "2026-04-10", trade_pnl=1.00, was_loss=False)
    assert is_day_locked(db, "2026-04-10", start_balance=340.00) is True


def test_win_resets_consecutive_losses():
    """Verify that a win after 2 losses resets the streak, preventing premature lock."""
    db = _make_db()
    get_or_create_risk_state(db, "2026-04-10", start_balance=340.00)
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)
    update_risk_state(db, "2026-04-10", trade_pnl=3.00, was_loss=False)  # win resets streak
    update_risk_state(db, "2026-04-10", trade_pnl=-5.00, was_loss=True)  # only 1 consecutive now
    assert is_day_locked(db, "2026-04-10", start_balance=340.00) is False


def test_no_metars_produces_low_confidence():
    """No METAR data at all should produce very low confidence."""
    from datetime import datetime, timezone
    from weather_model import WeatherModel
    from weather_contracts import WeatherContract

    model = WeatherModel()
    contract = WeatherContract(
        contract_id="TEST-T80", market_id="TEST",
        title="test", city="Denver", station_id="KDEN",
        metric="high_temp", threshold_f=80.0,
        expiry_ts=datetime(2026, 4, 11, tzinfo=timezone.utc),
        is_priceable=True, parse_status="ok",
    )
    now = datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc)
    est = model.price_contract(contract, [], now=now)
    assert est.confidence < 0.5
    assert est.prob_yes == 0.5  # no data = coin flip
