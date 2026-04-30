"""Tests for BTC 15-min trading bot components."""
import math
import sqlite3
import pytest
from btc_model import (
    price_btc_contract, compute_realized_vol, BTCEstimate,
    _compute_momentum_drift, VOL_FLOOR_ANNUAL,
)
from datetime import datetime, timedelta, timezone

from btc_scanner import (
    BTCMarket,
    _extract_target_price,
    _is_btc_short_term,
    _parse_btc_market,
    _parse_target_price,
)
from btc_bot import (
    BTCPortfolioSnapshot,
    BTCExecutionConfig,
    LIVE_ACK_ENV,
    _format_btc_portfolio_message,
    _format_btc_settlement_message,
    _format_btc_trade_open_message,
    _ensure_btc_tables,
    _get_btc_portfolio_snapshot,
    _get_or_start_btc_warmup,
    _live_gate,
)
from btc_simulator import simulate_rows
from config import BTC_PAPER_WARMUP_HOURS
from infra import Env
from infra import KalshiClient
from strategy import (
    BTCEvaluationInput,
    DEFAULT_BTC_STRATEGY,
    evaluate_btc_signal,
)


# ─── Model tests ─────────────────────────────────────────────────────────────

class TestBTCModel:
    def test_price_at_target_is_50_percent(self):
        """When price == target with no momentum, model should be ~50%."""
        est = price_btc_contract(
            contract_id="TEST",
            current_price=70000.0,
            target_price=70000.0,
            seconds_remaining=900,
            realized_vol_annual=0.50,
            vol_sample_count=30,
        )
        assert 0.48 <= est.prob_yes <= 0.52

    def test_price_above_target_is_high(self):
        """When price is well above target, prob should be high."""
        est = price_btc_contract(
            contract_id="TEST",
            current_price=71000.0,
            target_price=70000.0,
            seconds_remaining=900,
            realized_vol_annual=0.50,
            vol_sample_count=30,
        )
        assert est.prob_yes > 0.70

    def test_price_below_target_is_low(self):
        """When price is well below target, prob should be low."""
        est = price_btc_contract(
            contract_id="TEST",
            current_price=69000.0,
            target_price=70000.0,
            seconds_remaining=900,
            realized_vol_annual=0.50,
            vol_sample_count=30,
        )
        assert est.prob_yes < 0.30

    def test_less_time_more_extreme(self):
        """With less time, same price distance should give more extreme prob."""
        est_long = price_btc_contract(
            contract_id="TEST",
            current_price=70050.0,
            target_price=70000.0,
            seconds_remaining=900,
            realized_vol_annual=0.50,
            vol_sample_count=30,
        )
        est_short = price_btc_contract(
            contract_id="TEST",
            current_price=70050.0,
            target_price=70000.0,
            seconds_remaining=60,
            realized_vol_annual=0.50,
            vol_sample_count=30,
        )
        assert est_short.prob_yes > est_long.prob_yes

    def test_higher_vol_less_extreme(self):
        """Higher vol → probability closer to 50%."""
        est_low_vol = price_btc_contract(
            contract_id="TEST",
            current_price=70500.0,
            target_price=70000.0,
            seconds_remaining=900,
            realized_vol_annual=0.50,
            vol_sample_count=30,
        )
        est_high_vol = price_btc_contract(
            contract_id="TEST",
            current_price=70500.0,
            target_price=70000.0,
            seconds_remaining=900,
            realized_vol_annual=1.00,
            vol_sample_count=30,
        )
        assert est_low_vol.prob_yes > est_high_vol.prob_yes

    def test_expired_market(self):
        """Zero time remaining → deterministic."""
        est_above = price_btc_contract(
            contract_id="TEST",
            current_price=71000.0,
            target_price=70000.0,
            seconds_remaining=0,
            realized_vol_annual=0.50,
        )
        assert est_above.prob_yes == 1.0

        est_below = price_btc_contract(
            contract_id="TEST",
            current_price=69000.0,
            target_price=70000.0,
            seconds_remaining=0,
            realized_vol_annual=0.50,
        )
        assert est_below.prob_yes == 0.0

    def test_prob_clamped(self):
        """Probability should always be in [0.01, 0.99]."""
        est = price_btc_contract(
            contract_id="TEST",
            current_price=100000.0,
            target_price=50000.0,
            seconds_remaining=60,
            realized_vol_annual=0.50,
            vol_sample_count=30,
        )
        assert est.prob_yes <= 0.99

    def test_zero_price_returns_low_confidence(self):
        est = price_btc_contract(
            contract_id="TEST",
            current_price=0,
            target_price=70000.0,
            seconds_remaining=900,
            realized_vol_annual=0.50,
        )
        assert est.prob_yes == 0.5
        assert est.confidence == 0.0

    def test_vol_floor_applied(self):
        """Even with low realized vol, model uses the floor."""
        est = price_btc_contract(
            contract_id="TEST",
            current_price=70000.0,
            target_price=70000.0,
            seconds_remaining=900,
            realized_vol_annual=0.05,  # way below floor
            vol_sample_count=30,
        )
        assert est.realized_vol == VOL_FLOOR_ANNUAL
        assert est.raw_vol == 0.05

    def test_model_version_v2(self):
        est = price_btc_contract(
            contract_id="TEST",
            current_price=70000.0,
            target_price=70000.0,
            seconds_remaining=900,
            realized_vol_annual=0.50,
        )
        assert est.model_version == "btc_lognormal_v2"


# ─── Momentum tests ─────────────────────────────────────────────────────────

class TestMomentum:
    def test_upward_momentum_increases_prob(self):
        """Rising prices should shift probability upward."""
        # Rising candles
        rising = [70000.0 + i * 10 for i in range(10)]
        est_momentum = price_btc_contract(
            contract_id="TEST",
            current_price=70050.0,
            target_price=70050.0,  # at target
            seconds_remaining=900,
            realized_vol_annual=0.50,
            vol_sample_count=30,
            recent_closes=rising,
        )
        est_flat = price_btc_contract(
            contract_id="TEST",
            current_price=70050.0,
            target_price=70050.0,
            seconds_remaining=900,
            realized_vol_annual=0.50,
            vol_sample_count=30,
            recent_closes=None,
        )
        # With upward momentum, prob_yes should be higher
        assert est_momentum.prob_yes > est_flat.prob_yes
        assert est_momentum.drift > 0

    def test_downward_momentum_decreases_prob(self):
        """Falling prices should shift probability downward."""
        falling = [70000.0 - i * 10 for i in range(10)]
        est = price_btc_contract(
            contract_id="TEST",
            current_price=69910.0,
            target_price=69910.0,
            seconds_remaining=900,
            realized_vol_annual=0.50,
            vol_sample_count=30,
            recent_closes=falling,
        )
        assert est.prob_yes < 0.50
        assert est.drift < 0

    def test_no_momentum_without_data(self):
        """Without recent closes, drift should be 0."""
        drift = _compute_momentum_drift(None)
        assert drift == 0.0

        drift2 = _compute_momentum_drift([70000.0, 70001.0])  # too few
        assert drift2 == 0.0

    def test_flat_candles_no_drift(self):
        """Flat prices → drift near 0."""
        flat = [70000.0] * 10
        drift = _compute_momentum_drift(flat)
        assert drift == 0.0


# ─── Vol estimation tests ────────────────────────────────────────────────────

class TestVolEstimation:
    def test_constant_prices_hit_floor(self):
        """Constant prices should give the 35% floor vol."""
        closes = [70000.0] * 30
        vol, count = compute_realized_vol(closes)
        assert vol == VOL_FLOOR_ANNUAL
        assert count == 29

    def test_volatile_prices_higher_vol(self):
        """Swinging prices should produce higher vol."""
        closes = [70000.0 + (500.0 if i % 2 else 0.0) for i in range(30)]
        vol, count = compute_realized_vol(closes)
        assert vol > VOL_FLOOR_ANNUAL
        assert count == 29

    def test_too_few_samples(self):
        vol, count = compute_realized_vol([70000.0])
        assert vol == 0.0
        assert count == 0

    def test_empty_list(self):
        vol, count = compute_realized_vol([])
        assert vol == 0.0
        assert count == 0

    def test_vol_floor_at_35(self):
        """Vol should never go below 35%."""
        closes = [70000.0 + i * 0.01 for i in range(60)]
        vol, count = compute_realized_vol(closes)
        assert vol >= VOL_FLOOR_ANNUAL


# ─── Scanner tests ───────────────────────────────────────────────────────────

class TestTargetParsing:
    def test_parse_dollar_amount(self):
        assert _parse_target_price("BTC 15 min · $71,197.82 target") == 71197.82

    def test_parse_round_number(self):
        assert _parse_target_price("Will Bitcoin be above $71,250 at 5pm?") == 71250.0

    def test_parse_with_decimals(self):
        assert _parse_target_price("Bitcoin above $71,200.00") == 71200.0

    def test_parse_no_commas(self):
        assert _parse_target_price("BTC above $71250") == 71250.0

    def test_no_price_returns_none(self):
        assert _parse_target_price("BTC market") is None

    def test_target_price_tbd_returns_none(self):
        assert _parse_target_price("Target price: TBD") is None

    def test_extracts_target_from_live_subtitle(self):
        m = {
            "ticker": "KXBTC15M-26APR131230-30",
            "title": "BTC price up in next 15 mins?",
            "subtitle": "Target price: $83,421.25",
        }
        assert _extract_target_price(m) == 83421.25

    def test_extracts_target_from_custom_strike(self):
        m = {
            "custom_strike": {"target": {"value": "$83,421.25"}},
            "title": "BTC price up in next 15 mins?",
        }
        assert _extract_target_price(m) == 83421.25


class TestBTCMarketParsing:
    class DummyClient:
        def get_orderbook(self, ticker, depth=10):
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.4500", "10.00"]],
                    "no_dollars": [["0.5200", "12.00"]],
                }
            }

    def test_initialized_target_tbd_is_not_priceable(self):
        now = datetime.now(timezone.utc)
        m = {
            "ticker": "KXBTC15M-26APR131230-30",
            "event_ticker": "KXBTC15M-26APR131230",
            "title": "BTC price up in next 15 mins?",
            "subtitle": "Target price: TBD",
            "status": "initialized",
            "close_time": (now + timedelta(minutes=15)).isoformat(),
        }
        assert _parse_btc_market(m, now, self.DummyClient()) is None

    def test_initialized_target_price_is_not_tradeable(self):
        now = datetime.now(timezone.utc)
        m = {
            "ticker": "KXBTC15M-26APR131230-30",
            "event_ticker": "KXBTC15M-26APR131230",
            "title": "BTC price up in next 15 mins?",
            "subtitle": "Target price: $83,421.25",
            "status": "initialized",
            "close_time": (now + timedelta(minutes=10)).isoformat(),
        }
        assert _parse_btc_market(m, now, self.DummyClient()) is None

    def test_open_15m_market_with_target_is_priceable(self):
        now = datetime.now(timezone.utc)
        m = {
            "ticker": "KXBTC15M-26APR131230-30",
            "event_ticker": "KXBTC15M-26APR131230",
            "title": "BTC price up in next 15 mins?",
            "subtitle": "Target price: $83,421.25",
            "status": "open",
            "close_time": (now + timedelta(minutes=10)).isoformat(),
        }
        parsed = _parse_btc_market(m, now, self.DummyClient())
        assert parsed is not None
        assert parsed.target_price == 83421.25
        assert parsed.yes_bid == 0.45
        assert parsed.yes_ask == 0.48


class TestBTCWarmupGate:
    def test_warmup_starts_incomplete(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        _ensure_btc_tables(db)
        now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)

        status = _get_or_start_btc_warmup(db, now=now)

        assert status.started_at == now
        assert not status.complete
        assert status.hours_required == BTC_PAPER_WARMUP_HOURS

    def test_live_gate_blocks_until_48h_even_with_ack(self, monkeypatch):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        _ensure_btc_tables(db)
        monkeypatch.setenv(LIVE_ACK_ENV, "true")
        monkeypatch.setattr("btc_bot.PAUSED_FILE", "/tmp/nonexistent-btc-paused-file")
        now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)

        gate = _live_gate(
            db,
            BTCExecutionConfig(env=Env.LIVE, live_orders_requested=True),
            now=now,
        )

        assert not gate.allowed
        assert "warmup incomplete" in gate.reason

    def test_live_gate_allows_after_48h_with_ack(self, monkeypatch):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        _ensure_btc_tables(db)
        monkeypatch.setenv(LIVE_ACK_ENV, "true")
        monkeypatch.setattr("btc_bot.PAUSED_FILE", "/tmp/nonexistent-btc-paused-file")
        now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
        started = now - timedelta(hours=BTC_PAPER_WARMUP_HOURS, minutes=1)
        db.execute(
            "INSERT INTO btc_runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
            ("paper_warmup_started_at", started.isoformat(), started.isoformat()),
        )
        db.commit()

        gate = _live_gate(
            db,
            BTCExecutionConfig(env=Env.LIVE, live_orders_requested=True),
            now=now,
        )

        assert gate.allowed


class TestKalshiPublicClient:
    def test_public_client_can_initialize_without_credentials(self, monkeypatch):
        monkeypatch.delenv("KALSHI_API_KEY", raising=False)
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)

        client = KalshiClient(env=Env.LIVE)

        assert client._api_key == ""
        assert client._private_key is None

    def test_portfolio_request_requires_credentials(self, monkeypatch):
        monkeypatch.delenv("KALSHI_API_KEY", raising=False)
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
        client = KalshiClient(env=Env.LIVE)

        with pytest.raises(RuntimeError, match="credentials"):
            client.create_order(
                ticker="KXBTC15M-TEST",
                side="yes",
                contracts=1,
                price_cents=50,
            )


class TestMarketClassification:
    def test_updown_market(self):
        m = {"ticker": "KXBTC-26APR12-UP", "title": "Will Bitcoin be above $71,250?", "subtitle": ""}
        assert _is_btc_short_term(m) is True

    def test_range_market_rejected(self):
        m = {"ticker": "KXBTC-RANGE", "title": "Bitcoin price range $71,000 to 71,249.99", "subtitle": ""}
        assert _is_btc_short_term(m) is False

    def test_longterm_market_rejected(self):
        m = {"ticker": "KXBTCMINY", "title": "How low will Bitcoin get this year?", "subtitle": ""}
        assert _is_btc_short_term(m) is False

    def test_milestone_market_rejected(self):
        m = {"ticker": "KXBTC150K", "title": "When will Bitcoin hit $150k?", "subtitle": "Before January 2027"}
        assert _is_btc_short_term(m) is False

    def test_15min_market(self):
        m = {"ticker": "KXBTC15M-26APR12", "title": "BTC 15 min · $71,197.82 target", "subtitle": ""}
        assert _is_btc_short_term(m) is True

    def test_target_market(self):
        m = {"ticker": "KXBTC15M-26APR12", "title": "BTC 15 min · $71,197.82 target", "subtitle": ""}
        assert _is_btc_short_term(m) is True

    def test_hourly_above_market(self):
        m = {"ticker": "KXBTCD-26APR12-T71250", "title": "Bitcoin price today at 5pm EDT?", "subtitle": "$71,250 or above"}
        assert _is_btc_short_term(m) is True

    def test_price_today_market(self):
        m = {"ticker": "KXBTCD-26APR12", "title": "Bitcoin price today at 4pm EDT?", "subtitle": "$71,200 or above"}
        assert _is_btc_short_term(m) is True

    def test_non_btc_rejected(self):
        m = {"ticker": "KXETH-UP", "title": "Will Ethereum be above $3,000?", "subtitle": ""}
        assert _is_btc_short_term(m) is False


class TestBTCStrategy:
    def test_degraded_feed_requires_extra_edge(self):
        inputs = BTCEvaluationInput(
            ticker="KXBTC15M-TEST",
            prob_yes=0.66,
            confidence=0.90,
            yes_ask=0.50,
            no_ask=0.50,
            spread=0.02,
            seconds_remaining=300,
            feed_health="DEGRADED",
        )

        side, edge, reason = evaluate_btc_signal(inputs)

        assert side is None
        assert edge == 0.0
        assert reason == "LOW_EDGE(0.160)"

    def test_live_feed_allows_same_edge(self):
        inputs = BTCEvaluationInput(
            ticker="KXBTC15M-TEST",
            prob_yes=0.66,
            confidence=0.90,
            yes_ask=0.50,
            no_ask=0.50,
            spread=0.02,
            seconds_remaining=300,
            feed_health="LIVE",
        )

        side, edge, reason = evaluate_btc_signal(inputs)

        assert side == "yes"
        assert edge == pytest.approx(0.16)
        assert reason == "SIGNAL"


class TestBTCSimulator:
    def test_simulator_replays_trades_once_per_ticker(self):
        rows = [
            {
                "evaluated_at": "2026-04-13T12:00:00+00:00",
                "ticker": "KXBTC15M-A",
                "model_prob": 0.70,
                "confidence": 0.90,
                "yes_ask": 0.50,
                "no_ask": 0.52,
                "kalshi_spread": 0.02,
                "seconds_remaining": 300,
                "feed_health": "LIVE",
                "venue_spread_bps": 4.0,
                "btc_price": 70010.0,
                "target_price": 70000.0,
            },
            {
                "evaluated_at": "2026-04-13T12:00:30+00:00",
                "ticker": "KXBTC15M-A",
                "model_prob": 0.71,
                "confidence": 0.91,
                "yes_ask": 0.51,
                "no_ask": 0.51,
                "kalshi_spread": 0.02,
                "seconds_remaining": 270,
                "feed_health": "LIVE",
                "venue_spread_bps": 4.0,
                "btc_price": 70012.0,
                "target_price": 70000.0,
            },
            {
                "evaluated_at": "2026-04-13T12:01:00+00:00",
                "ticker": "KXBTC15M-B",
                "model_prob": 0.40,
                "confidence": 0.95,
                "yes_ask": 0.55,
                "no_ask": 0.43,
                "kalshi_spread": 0.03,
                "seconds_remaining": 240,
                "feed_health": "LIVE",
                "venue_spread_bps": 4.0,
                "btc_price": 69980.0,
                "target_price": 70000.0,
            },
        ]
        settlements = {"KXBTC15M-A": "yes", "KXBTC15M-B": "no"}

        decisions, metrics = simulate_rows(rows, settlements, strategy=DEFAULT_BTC_STRATEGY)

        assert len(decisions) == 3
        assert [d.action for d in decisions] == ["trade", "skip", "trade"]
        assert decisions[1].reason == "HELD"
        assert metrics.trades == 2
        assert metrics.wins == 2
        assert metrics.losses == 0
        assert metrics.win_rate == 1.0
        assert metrics.net_pnl == pytest.approx(6.42, rel=1e-6)


class TestBTCPortfolioFormatting:
    def test_portfolio_snapshot_aggregates_open_and_settled_rows(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        _ensure_btc_tables(db)
        today = "2026-04-28"
        db.execute(
            "INSERT INTO btc_daily_state (date, trades_count, realized_pnl, day_locked, updated_at) VALUES (?, ?, ?, 0, ?)",
            (today, 3, 4.25, "2026-04-28T12:00:00+00:00"),
        )
        db.execute(
            """
            INSERT INTO btc_paper_positions
            (contract_id, ticker, side, quantity, entry_price, max_loss, opened_at, status)
            VALUES
            ('A', 'A', 'yes', 5, 0.40, 2.0, '2026-04-28T12:00:00+00:00', 'open'),
            ('B', 'B', 'no', 4, 0.30, 1.2, '2026-04-28T12:05:00+00:00', 'open')
            """
        )
        db.execute(
            """
            INSERT INTO btc_paper_positions
            (contract_id, ticker, side, quantity, entry_price, max_loss, opened_at, closed_at, realized_pnl, status)
            VALUES
            ('C', 'C', 'yes', 4, 0.45, 1.8, '2026-04-28T11:00:00+00:00', '2026-04-28T11:15:00+00:00', 2.20, 'settled'),
            ('D', 'D', 'no', 6, 0.25, 1.5, '2026-04-28T11:30:00+00:00', '2026-04-28T11:45:00+00:00', -1.00, 'settled')
            """
        )
        db.commit()

        snapshot = _get_btc_portfolio_snapshot(db, today=today)

        assert snapshot.open_positions == 2
        assert snapshot.open_risk == pytest.approx(3.2)
        assert snapshot.today_trades == 3
        assert snapshot.today_realized_pnl == pytest.approx(4.25)
        assert snapshot.settled_total == 2
        assert snapshot.wins == 1
        assert snapshot.losses == 1
        assert snapshot.realized_pnl_total == pytest.approx(1.20)

    def test_portfolio_message_includes_rolling_record(self):
        snapshot = BTCPortfolioSnapshot(
            open_positions=1,
            open_risk=2.9,
            today_trades=4,
            today_realized_pnl=-1.75,
            settled_total=5,
            wins=3,
            losses=2,
            realized_pnl_total=6.40,
        )

        msg = _format_btc_portfolio_message(snapshot)

        assert msg == (
            "PORTFOLIO | open=1 risk=$2.90 | today=4 pnl=$-1.75 | "
            "lifetime=3-2 (60%) pnl=$+6.40"
        )

    def test_trade_open_message_contains_market_and_edge(self):
        market = BTCMarket(
            ticker="KXBTC15M-TEST",
            event_ticker="KXBTC15M-TEST",
            title="BTC price up in next 15 mins?",
            target_price=76142.06,
            close_time=datetime(2026, 4, 28, 19, 45, tzinfo=timezone.utc),
            seconds_remaining=449,
            yes_bid=0.56,
            yes_ask=0.58,
            no_bid=0.41,
            no_ask=0.43,
            spread=0.02,
            bid_depth=10,
            ask_depth=8,
            volume=100,
        )
        estimate = BTCEstimate(
            contract_id="KXBTC15M-TEST",
            prob_yes=0.585,
            confidence=0.82,
            current_price=76169.45,
            target_price=76142.06,
            seconds_remaining=449,
            realized_vol=0.35,
        )

        msg = _format_btc_trade_open_message(market, estimate, "no", 0.125, 6)

        assert msg == (
            "PAPER BET | KXBTC15M-TEST | NO 6 @ 43.0c | edge=12.5c model=0.585 conf=0.82 | "
            "btc=$76,169.45 target=$76,142.06 t=449s"
        )

    def test_settlement_message_formats_win_loss(self):
        msg = _format_btc_settlement_message(
            ticker="KXBTC15M-TEST",
            side="no",
            quantity=6,
            entry_price=0.43,
            result="no",
            pnl=3.42,
        )

        assert msg == "SETTLED [W] | KXBTC15M-TEST | NO 6 @ 43.0c -> NO | pnl=$+3.42"
