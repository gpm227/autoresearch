"""
infra.py — Fixed Kalshi trading infrastructure.
THIS FILE IS NEVER MODIFIED after Phase 1.
All tunable parameters live in strategy.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlencode
import math

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────────

class Env(Enum):
    DEMO = "demo"
    LIVE = "live"


DEMO_REST_BASE = "https://demo-api.kalshi.co/trade-api/v2"
LIVE_REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_WS_BASE = "wss://demo-api.kalshi.co/trade-api/ws/v2"
LIVE_WS_BASE = "wss://api.elections.kalshi.com/trade-api/ws/v2"


# ─── Kalshi API Client ─────────────────────────────────────────────────────────

class KalshiClient:
    """
    Authenticated HTTP client for Kalshi Trade API v2.
    Auth: RSA-PSS SHA-256 signing.
    Signature covers: timestamp_ms + HTTP_METHOD + /trade-api/v2 + path (no query string).
    Credentials from env: KALSHI_API_KEY or KALSHI_API_KEY_ID, plus
    KALSHI_PRIVATE_KEY_PATH.
    """

    def __init__(self, env: Env = Env.DEMO):
        self.env = env
        self.base_url = DEMO_REST_BASE if env == Env.DEMO else LIVE_REST_BASE
        self._api_key = (
            os.environ.get("KALSHI_API_KEY", "")
            or os.environ.get("KALSHI_API_KEY_ID", "")
        )
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        self._private_key = None
        if self._api_key and key_path:
            with open(key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        self._http = httpx.Client(timeout=10.0)
        self._rate_limiter = _RateLimiter(requests_per_second=10)
        self._circuit_open = False
        self._circuit_failures = 0
        self._circuit_threshold = 5
        self._circuit_opened_at = 0.0
        self._circuit_cooldown_sec = 300  # auto-reset after 5 minutes

    def _sign(self, method: str, path: str) -> tuple[str, str]:
        """Returns (timestamp_ms_str, signature_b64). path is /trade-api/v2 + endpoint."""
        ts_ms = str(int(time.time() * 1000))
        msg = (ts_ms + method.upper() + path).encode("utf-8")
        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return ts_ms, base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict[str, str]:
        if not self._api_key or self._private_key is None:
            raise RuntimeError("Kalshi credentials are required for authenticated requests")
        ts, sig = self._sign(method, path)
        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """
        endpoint: e.g. "/exchange/status" or "/markets?status=open"
        Signing path excludes query string and prepends /trade-api/v2.
        """
        if self._circuit_open:
            if time.time() - self._circuit_opened_at >= self._circuit_cooldown_sec:
                log.info("Circuit breaker auto-reset after %ds cooldown", self._circuit_cooldown_sec)
                self.reset_circuit()
            else:
                raise RuntimeError("Circuit breaker open — API unavailable")
        self._rate_limiter.acquire()
        url = self.base_url + endpoint
        # Path for signing: /trade-api/v2 + endpoint without query string
        sign_path = "/trade-api/v2" + endpoint.split("?")[0]
        headers = {"Content-Type": "application/json"}
        if self._api_key and self._private_key is not None:
            headers = self._headers(method, sign_path)
        elif endpoint.startswith("/portfolio"):
            raise RuntimeError("Kalshi credentials are required for portfolio requests")
        resp = self._http.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            self._circuit_failures += 1
            if self._circuit_failures >= self._circuit_threshold:
                self._circuit_open = True
                self._circuit_opened_at = time.time()
                log.error("Circuit breaker tripped after %d failures — will auto-reset in %ds",
                          self._circuit_failures, self._circuit_cooldown_sec)
            resp.raise_for_status()
        self._circuit_failures = 0
        return resp.json()

    def reset_circuit(self) -> None:
        self._circuit_open = False
        self._circuit_failures = 0

    # ── Exchange ───────────────────────────────────────────────────────────────

    def get_exchange_status(self) -> dict:
        return self._request("GET", "/exchange/status")

    def is_exchange_open(self) -> bool:
        try:
            status = self.get_exchange_status()
            return status.get("trading_active", False)
        except Exception as e:
            log.warning("Could not fetch exchange status: %s", e)
            return False

    def get_exchange_schedule(self) -> dict:
        return self._request("GET", "/exchange/schedule")

    # ── Markets ────────────────────────────────────────────────────────────────

    def get_markets(self, status: str = "open", limit: int = 200, cursor: str = "",
                    mve_filter: str = "", max_close_ts: str = "",
                    min_close_ts: str = "", tickers: str = "",
                    series_ticker: str = "", event_ticker: str = "") -> dict:
        params: dict[str, str | int] = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if mve_filter:
            params["mve_filter"] = mve_filter
        if max_close_ts:
            params["max_close_ts"] = max_close_ts
        if min_close_ts:
            params["min_close_ts"] = min_close_ts
        if tickers:
            params["tickers"] = tickers
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._request("GET", f"/markets?{urlencode(params)}")

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook?depth={depth}")

    def get_candlesticks(self, ticker: str, start_ts: int, end_ts: int,
                         period_interval: int = 60) -> list[dict]:
        """Returns list of candlestick dicts for a single ticker."""
        resp = self._request(
            "GET",
            f"/markets/candlesticks?market_tickers={ticker}"
            f"&start_ts={start_ts}&end_ts={end_ts}"
            f"&period_interval={period_interval}",
        )
        # Response: {"markets": [{"candlesticks": [...], "ticker": "..."}]}
        for m in resp.get("markets", []):
            if m.get("ticker") == ticker:
                return m.get("candlesticks", [])
        return []

    def get_historical_candlesticks(self, ticker: str, start_ts: int, end_ts: int,
                                    period_interval: int = 60) -> list[dict]:
        resp = self._request(
            "GET",
            f"/historical/markets/{ticker}/candlesticks"
            f"?start_ts={start_ts}&end_ts={end_ts}"
            f"&period_interval={period_interval}",
        )
        return resp.get("candlesticks", [])

    def get_historical_markets(self, limit: int = 200, cursor: str = "",
                               event_ticker: str = "") -> dict:
        params = f"?limit={limit}"
        if cursor:
            params += f"&cursor={cursor}"
        if event_ticker:
            params += f"&event_ticker={event_ticker}"
        return self._request("GET", f"/historical/markets{params}")

    def get_historical_market(self, ticker: str) -> dict:
        return self._request("GET", f"/historical/markets/{ticker}")

    # ── Portfolio ──────────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> dict:
        return self._request("GET", "/portfolio/positions")

    def get_orders(self, status: str = "resting") -> dict:
        return self._request("GET", f"/portfolio/orders?status={status}")

    def create_order(self, ticker: str, side: str, contracts: int,
                     price_cents: int, client_order_id: str | None = None) -> dict:
        if client_order_id is None:
            client_order_id = str(uuid.uuid4())
        body: dict = {
            "ticker": ticker,
            "action": "buy",
            "side": side,                     # "yes" or "no"
            "count": contracts,
            "client_order_id": client_order_id,
        }
        # API requires exactly ONE price field matching the side
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents
        return self._request("POST", "/portfolio/orders", content=json.dumps(body))

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/portfolio/orders/{order_id}")


class _RateLimiter:
    """Token bucket rate limiter — enforces minimum interval between calls."""

    def __init__(self, requests_per_second: float):
        self._min_interval = 1.0 / requests_per_second
        self._last_call = 0.0

    def acquire(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


# ─── Order Book Analysis ───────────────────────────────────────────────────────

@dataclass
class OrderBook:
    """
    Kalshi order book for a single YES outcome.
    yes_bids: list of (price_cents, size_contracts), sorted descending by price
    yes_asks: list of (price_cents, size_contracts), sorted ascending by price
    """
    yes_bids: list[tuple[int, int]]
    yes_asks: list[tuple[int, int]]

    @classmethod
    def from_api(cls, data: dict) -> "OrderBook":
        """Parse from GET /markets/{ticker}/orderbook response."""
        ob = data.get("orderbook", data)
        # YES bids
        bids = [(int(entry[0]), int(entry[1])) for entry in ob.get("yes", [])]
        # NO side prices are in NO terms; flip to YES: yes_price = 100 - no_price
        asks_raw = [(int(entry[0]), int(entry[1])) for entry in ob.get("no", [])]
        asks_flipped = [(100 - p, s) for p, s in asks_raw]
        bids_sorted = sorted(bids, key=lambda x: -x[0])
        asks_sorted = sorted(asks_flipped, key=lambda x: x[0])
        return cls(yes_bids=bids_sorted, yes_asks=asks_sorted)

    def best_bid(self) -> int:
        if not self.yes_bids:
            raise ValueError("Empty bid side")
        return self.yes_bids[0][0]

    def best_ask(self) -> int:
        if not self.yes_asks:
            raise ValueError("Empty ask side")
        return self.yes_asks[0][0]

    def spread_cents(self) -> int:
        return self.best_ask() - self.best_bid()

    def weighted_mid(self) -> float:
        """
        Imbalance-weighted mid price.
        Weights toward whichever side has more depth at the top of book.
        If bid depth >> ask depth → mid pulled toward ask (buy pressure).
        """
        if not self.yes_bids or not self.yes_asks:
            raise ValueError("Cannot compute mid on empty book")
        bid_p, bid_s = self.yes_bids[0]
        ask_p, ask_s = self.yes_asks[0]
        total = bid_s + ask_s
        if total == 0:
            return (bid_p + ask_p) / 2.0
        # Weight the opposite side's price by this side's depth
        return (bid_p * ask_s + ask_p * bid_s) / total

    def depth_at_ticks(self, n: int = 3) -> dict[str, float]:
        """Total size within n ticks of the arithmetic mid on each side."""
        mid = (self.best_bid() + self.best_ask()) / 2.0
        bid_depth = sum(s for p, s in self.yes_bids if abs(p - mid) <= n)
        ask_depth = sum(s for p, s in self.yes_asks if abs(p - mid) <= n)
        return {"bid": float(bid_depth), "ask": float(ask_depth)}

    def vwap(self, side: str = "ask", max_contracts: int = 100) -> float:
        """Volume-weighted average price for first max_contracts on given side."""
        book = self.yes_asks if side == "ask" else self.yes_bids
        total_cost = 0.0
        filled = 0
        for price, size in book:
            take = min(size, max_contracts - filled)
            total_cost += price * take
            filled += take
            if filled >= max_contracts:
                break
        return total_cost / filled if filled else 0.0

    def imbalance(self) -> float:
        """
        Order book imbalance at best bid/ask.
        +1.0 = all depth on bid (strong buy pressure).
        -1.0 = all depth on ask (strong sell pressure).
        0.0 = balanced.
        """
        if not self.yes_bids or not self.yes_asks:
            return 0.0
        bid_p, bid_s = self.yes_bids[0]
        ask_p, ask_s = self.yes_asks[0]
        total = bid_s + ask_s
        if total == 0:
            return 0.0
        return (bid_s - ask_s) / total


# ─── LMSR Theoretical Prior ────────────────────────────────────────────────────

def lmsr_cost(q_yes_before: float, q_no_before: float,
              q_yes_after: float, q_no_after: float,
              b: float) -> float:
    """
    Cost to move from (q_yes_before, q_no_before) to (q_yes_after, q_no_after).
    C(q) = b * ln(e^(q_yes/b) + e^(q_no/b))
    Cost = C(after) - C(before)
    Uses log-sum-exp trick for numerical stability.
    """
    def _c(qy: float, qn: float) -> float:
        a, b_ = qy / b, qn / b
        m = max(a, b_)
        return b * (m + math.log(math.exp(a - m) + math.exp(b_ - m)))

    return _c(q_yes_after, q_no_after) - _c(q_yes_before, q_no_before)


def lmsr_price(q_yes: float, q_no: float, b: float) -> float:
    """
    LMSR instantaneous price for YES outcome.
    p_yes = e^(q_yes/b) / (e^(q_yes/b) + e^(q_no/b))
    Numerically stable sigmoid: p = sigmoid((q_yes - q_no) / b)
    """
    diff = (q_yes - q_no) / b
    if diff >= 0:
        return 1.0 / (1.0 + math.exp(-diff))
    else:
        e = math.exp(diff)
        return e / (1.0 + e)


def time_decay_signal(close_time: Optional[datetime], yes_bid: int, yes_ask: int,
                      max_hours: float = 4.0) -> float:
    """
    Markets closing soon with prices not near 0 or 100 are potentially mispriced.
    Returns signal in [-1, 1]:
      Positive = lean YES (price > 50 and closing soon, momentum likely continues)
      Negative = lean NO (price < 50 and closing soon)
      Near zero = price already near extreme or market far from close
    """
    if close_time is None:
        return 0.0
    now = datetime.now(timezone.utc)
    hours_remaining = (close_time - now).total_seconds() / 3600.0
    if hours_remaining <= 0 or hours_remaining > max_hours:
        return 0.0
    mid = (yes_bid + yes_ask) / 2.0
    # How "unresolved" is the price? Max at 50, zero at 0 or 100
    unresolved = 1.0 - abs(mid - 50.0) / 50.0
    # Urgency: higher when close is near
    urgency = max(0.0, 1.0 - hours_remaining / max_hours)
    # Direction: which side of 50
    direction = (mid - 50.0) / 50.0  # -1 to +1
    return direction * unresolved * urgency


def volume_spike_signal(volume_24h: float, median_volume: float,
                        yes_bid: int, yes_ask: int,
                        threshold: float = 3.0) -> float:
    """
    Detect unusual volume relative to a baseline.
    Returns signal in [-1, 1]:
      Direction comes from which side of 50 the price sits
      (high volume + price moving away from 50 = information entering)
    Magnitude scales with how extreme the volume ratio is.
    """
    if median_volume <= 0 or volume_24h <= 0:
        return 0.0
    ratio = volume_24h / median_volume
    if ratio < threshold:
        return 0.0
    # Magnitude: log scale, capped at 1.0
    magnitude = min(1.0, math.log(ratio) / math.log(10.0))
    # Direction: price displacement from 50
    mid = (yes_bid + yes_ask) / 2.0
    direction = (mid - 50.0) / 50.0
    return direction * magnitude


# ─── Bayesian Signal Engine ────────────────────────────────────────────────────

@dataclass
class Signal:
    market_id: str
    source: str
    headline: str
    body: str
    sentiment: float   # -1.0 (very negative) to +1.0 (very positive)
    magnitude: float   # 0.0 to 1.0 confidence in the signal
    timestamp: float   # unix timestamp


@dataclass
class _SignalCluster:
    fingerprint: str
    signals: list[Signal] = field(default_factory=list)
    first_seen: float = field(default_factory=time.time)


class BayesianEngine:
    """
    Sequential Bayesian updating in log-space for numerical stability.
    log P(H|D) = log P(H) + Σ log P(Dk|H) - log Z

    Signal deduplication:
    - Fingerprint = SHA256(headline + body[:200])
    - Fingerprints within cluster_window_sec are deduplicated (cluster counts once)

    Confidence decay:
    - Log-odds decay exponentially when no new evidence arrives.

    Negative evidence:
    - Negative sentiment signals push log-odds toward NO.
    """

    def __init__(self, cluster_window_sec: float = 600,
                 confidence_decay_rate: float = 0.01):
        self._cluster_window = cluster_window_sec
        self._decay_rate = confidence_decay_rate
        # market_id → log_odds (0.0 = 50/50)
        self._log_odds: dict[str, float] = {}
        # fingerprint → _SignalCluster
        self._clusters: dict[str, _SignalCluster] = {}
        # market_id → last update timestamp
        self._last_update: dict[str, float] = {}

    @staticmethod
    def _fingerprint(headline: str, body: str) -> str:
        content = (headline.strip() + body[:200].strip()).lower()
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _is_duplicate(self, fp: str) -> bool:
        if fp not in self._clusters:
            return False
        cluster = self._clusters[fp]
        age = time.time() - cluster.first_seen
        return age < self._cluster_window

    def update(self, signal: Signal) -> None:
        """Update posterior for signal.market_id. Deduplicated by fingerprint."""
        fp = self._fingerprint(signal.headline, signal.body)
        if self._is_duplicate(fp):
            log.debug("Deduped signal: %s", signal.headline[:60])
            return
        # Register cluster
        if fp not in self._clusters:
            self._clusters[fp] = _SignalCluster(fingerprint=fp)
        self._clusters[fp].signals.append(signal)

        # Log-likelihood ratio update
        # LLR = sentiment * magnitude * scale
        # positive sentiment → evidence for YES (increases log_odds)
        # negative sentiment → evidence against YES (decreases log_odds)
        strength = signal.magnitude * abs(signal.sentiment)
        llr = signal.sentiment * strength * 2.0

        current = self._log_odds.get(signal.market_id, 0.0)
        self._log_odds[signal.market_id] = current + llr
        self._last_update[signal.market_id] = time.time()

    def posterior(self, market_id: str) -> tuple[float, float]:
        """
        Returns (p_yes, confidence).
        p_yes: posterior probability [0, 1].
        confidence: 0.5 (no info) → 1.0 (certain). = 0.5 + |p - 0.5|
        """
        log_odds = self._log_odds.get(market_id, 0.0)

        # Confidence decay: multiply log_odds by exp(-decay_rate * age)
        last = self._last_update.get(market_id, time.time())
        age_sec = time.time() - last
        log_odds = log_odds * math.exp(-self._decay_rate * age_sec)

        # Sigmoid to probability (numerically stable)
        if log_odds >= 0:
            p = 1.0 / (1.0 + math.exp(-log_odds))
        else:
            e = math.exp(log_odds)
            p = e / (1.0 + e)

        confidence = 0.5 + abs(p - 0.5)
        return p, confidence

    def has_signal(self, market_id: str) -> bool:
        """Returns True if this market has received at least one signal update."""
        return market_id in self._log_odds and self._log_odds[market_id] != 0.0

    def apply_cooldown(self, market_id: str) -> None:
        """Reset posteriors for a market after cooldown period."""
        self._log_odds.pop(market_id, None)


# ─── EV Calculator ─────────────────────────────────────────────────────────────

@dataclass
class TradeParams:
    p_hat: float         # estimated probability (0-1)
    price_cents: int     # market ask price in cents (1-99)
    contracts: int       # number of contracts
    fee_per_contract: float  # in dollars (e.g. 0.02)
    slippage_cents: float    # expected slippage in cents per contract


def calculate_ev(params: TradeParams) -> float:
    """
    Expected value of the trade in dollars.
    EV = contracts * [p_hat * (1 - price) - (1 - p_hat) * price - fee - slippage]
    Prices converted from cents to dollars.
    """
    price = params.price_cents / 100.0
    profit_if_win = 1.0 - price         # per contract, dollars
    loss_if_lose = price                # per contract, dollars
    fee = params.fee_per_contract
    slippage = params.slippage_cents / 100.0

    ev_per_contract = (
        params.p_hat * profit_if_win
        - (1.0 - params.p_hat) * loss_if_lose
        - fee
        - slippage
    )
    return ev_per_contract * params.contracts


def estimate_slippage(book: "OrderBook", contracts: int) -> float:
    """
    Estimate slippage in cents per contract by walking the ask side.
    Returns difference between VWAP and best ask, or a conservative default.
    """
    if not book.yes_asks:
        return 5.0
    best = book.best_ask()
    vwap = book.vwap(side="ask", max_contracts=contracts)
    return max(0.0, vwap - best)


# ─── Risk Engine ───────────────────────────────────────────────────────────────

class TradingPhase(Enum):
    PROVE = "prove"     # $100 – $5,000
    SCALE = "scale"     # $5,000 – $25,000
    INCOME = "income"   # $25,000+


# Risk percentage per trade by phase
PHASE_RISK_PCT = {
    TradingPhase.PROVE: 0.02,
    TradingPhase.SCALE: 0.02,
    TradingPhase.INCOME: 0.02,
}

# (upper_bound_cents_exclusive, phase) — checked in order
PHASE_THRESHOLDS = [
    (500_000, TradingPhase.PROVE),      # < $5,000
    (2_500_000, TradingPhase.SCALE),    # < $25,000
]


class RiskEngine:
    """
    Half-Kelly position sizing with tier-aware risk caps.

    Half-Kelly: f* = 0.5 * (b*p - q) / b
      where b = net odds = (1 - price) / price
            p = estimated probability
            q = 1 - p

    Caps:
    - Tier max risk percentage (PHASE_RISK_PCT)
    - Portfolio exposure cap: 25% of bankroll in open positions

    Kill switch: 15% rolling drawdown halts all trading.
    """

    PORTFOLIO_CAP = 0.25
    KILL_DRAWDOWN_PCT = 0.15       # 15% rolling 7-day drawdown — HARD CODED
    DAILY_STOP_LOSS_PCT = 0.15     # 15% daily loss limit — HARD CODED
    MAX_BET_PCT = 0.05             # 5% max per trade — HARD CODED, not tunable

    def __init__(self, bankroll_cents: int):
        self._bankroll = bankroll_cents
        self._peak_bankroll = bankroll_cents
        self._rolling_loss = 0
        self._daily_loss_cents = 0
        self._daily_stop_active = False
        self._daily_stop_date: Optional[date] = None
        self._open_positions: dict[str, int] = {}  # market_id → value_cents
        self._kill_switch = False

    @property
    def phase(self) -> TradingPhase:
        for threshold, phase in PHASE_THRESHOLDS:
            if self._bankroll < threshold:
                return phase
        return TradingPhase.INCOME

    @property
    def bankroll_dollars(self) -> float:
        return self._bankroll / 100.0

    def position_size(self, p_hat: float, price_cents: int) -> int:
        """
        Returns number of contracts to buy (0 if no edge).
        """
        price = price_cents / 100.0
        if price <= 0 or price >= 1:
            return 0
        b = (1.0 - price) / price    # net odds
        q = 1.0 - p_hat
        kelly_f = (b * p_hat - q) / b
        half_kelly_f = 0.5 * kelly_f
        if half_kelly_f <= 0:
            return 0

        max_risk_pct = PHASE_RISK_PCT[self.phase]
        risk_fraction = min(half_kelly_f, max_risk_pct, self.MAX_BET_PCT)
        risk_dollars = self.bankroll_dollars * risk_fraction
        # cost per contract = price dollars
        contracts = int(risk_dollars / price)
        return max(0, contracts)

    def can_open_position(self, value_cents: int) -> bool:
        """Returns True if adding this position keeps exposure <= 25% of bankroll."""
        current_exposure = sum(self._open_positions.values())
        cap = int(self._bankroll * self.PORTFOLIO_CAP)
        return (current_exposure + value_cents) <= cap

    def add_open_position(self, market_id: str, value_cents: int) -> None:
        self._open_positions[market_id] = value_cents

    def close_position(self, market_id: str) -> None:
        self._open_positions.pop(market_id, None)

    def record_loss(self, cents: int) -> None:
        self._rolling_loss += cents
        self._daily_loss_cents += cents
        self._bankroll -= cents
        # Daily stop-loss (hard-coded, not tunable)
        if self._peak_bankroll > 0:
            daily_pct = self._daily_loss_cents / self._peak_bankroll
            if daily_pct >= self.DAILY_STOP_LOSS_PCT:
                self._daily_stop_active = True
                self._daily_stop_date = datetime.now(
                    timezone(timedelta(hours=-5))
                ).date()
                log.critical("DAILY STOP-LOSS: lost %d cents today (%.1f%%)",
                             self._daily_loss_cents, daily_pct * 100)
        # Rolling kill switch
        if self._peak_bankroll > 0:
            drawdown_pct = self._rolling_loss / self._peak_bankroll
            if drawdown_pct >= self.KILL_DRAWDOWN_PCT:
                self._kill_switch = True
                log.critical("KILL SWITCH ACTIVATED: %.1f%% drawdown", drawdown_pct * 100)

    def record_win(self, cents: int) -> None:
        self._bankroll += cents
        if self._bankroll > self._peak_bankroll:
            self._peak_bankroll = self._bankroll

    def daily_stop_active(self) -> bool:
        """Check if daily stop-loss is hit. Auto-resets at midnight ET."""
        if not self._daily_stop_active:
            return False
        today = datetime.now(timezone(timedelta(hours=-5))).date()
        if self._daily_stop_date != today:
            self._daily_stop_active = False
            self._daily_loss_cents = 0
            return False
        return True

    def daily_loss_dollars(self) -> float:
        return self._daily_loss_cents / 100.0

    def kill_switch_active(self) -> bool:
        return self._kill_switch


# ─── Market Scanner ────────────────────────────────────────────────────────────

@dataclass
class MarketOpportunity:
    ticker: str
    event_ticker: str
    title: str
    yes_bid: int
    yes_ask: int
    spread_cents: int
    volume_24h: float
    book_depth_dollars: float
    close_time: Optional[datetime]
    category: str
    book: Optional["OrderBook"] = None


class MarketScanner:
    """
    Two-phase scanner:
    1. Discovery: full catalog scan (runs once, then every DISCOVERY_INTERVAL_SEC).
       Caches tickers of all markets that pass hard filters.
    2. Refresh: fetches only cached tickers via single-market endpoint.
       Runs every tick (~30s) and is fast (one API call per liquid market).
    """

    def __init__(self, client: KalshiClient):
        self._client = client
        self._liquid_tickers: list[str] = []
        self._last_discovery: float = 0.0
        self._median_volume: float = 0.0

    def scan(self, strategy: Any) -> list[MarketOpportunity]:
        discovery_interval = getattr(strategy, "DISCOVERY_INTERVAL_SEC", 600)
        now = time.time()

        # Phase 1: Discovery — full catalog scan (first call + periodically)
        if not self._liquid_tickers or (now - self._last_discovery >= discovery_interval):
            self._discover(strategy)
            self._last_discovery = now

        # Phase 2: Refresh — batch-fetch fresh data for known liquid tickers
        opportunities = []
        # Kalshi tickers param is comma-separated, fetch in chunks of 100
        for i in range(0, len(self._liquid_tickers), 100):
            chunk = self._liquid_tickers[i:i+100]
            tickers_param = ",".join(chunk)
            try:
                resp = self._client.get_markets(
                    status="open", limit=200, tickers=tickers_param,
                )
                for m in resp.get("markets", []):
                    opp = self._evaluate(m, strategy)
                    if opp is not None:
                        opportunities.append(opp)
            except Exception as e:
                log.warning("Batch refresh failed: %s", e)

        log.info("Scanner: refreshed %d liquid tickers, %d passed filters",
                 len(self._liquid_tickers), len(opportunities))
        return opportunities

    def _discover(self, strategy: Any) -> None:
        """Full catalog scan to find all liquid tickers."""
        tickers = []
        all_volumes: list[float] = []
        cursor = ""
        total_fetched = 0
        pages = 0
        while True:
            resp = self._client.get_markets(
                status="open", limit=200, cursor=cursor,
                mve_filter="exclude",
            )
            markets = resp.get("markets", [])
            if not markets:
                break
            total_fetched += len(markets)
            pages += 1
            for m in markets:
                vol = float(m.get("volume_24h", 0) or 0)
                if vol > 0:
                    all_volumes.append(vol)
                opp = self._evaluate(m, strategy)
                if opp is not None:
                    tickers.append(m["ticker"])
            cursor = resp.get("cursor", "")
            if not cursor:
                break
        self._liquid_tickers = tickers
        if all_volumes:
            all_volumes.sort()
            self._median_volume = all_volumes[len(all_volumes) // 2]
        log.info("Discovery: scanned %d markets (%d pages), found %d liquid tickers, median_vol=%.0f",
                 total_fetched, pages, len(tickers), self._median_volume)

    def _evaluate(self, m: dict, strategy: Any) -> Optional[MarketOpportunity]:
        """Apply hard filters. Return None if market fails any filter."""
        try:
            yes_bid = m.get("yes_bid", 0) or 0
            yes_ask = m.get("yes_ask", 0) or 0
            spread = yes_ask - yes_bid
            volume = m.get("volume_24h", 0) or 0

            passed, _reason = strategy.filter_market(
                market_type=m.get("market_type", ""),
                spread=spread,
                volume=volume,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
            )
            if not passed:
                return None

            close_dt: Optional[datetime] = None
            close_ts = m.get("close_time")
            if close_ts:
                try:
                    close_dt = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
                except Exception:
                    pass

            category = (m.get("category") or "other").lower()

            return MarketOpportunity(
                ticker=m["ticker"],
                event_ticker=m.get("event_ticker", ""),
                title=m.get("title", ""),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                spread_cents=spread,
                volume_24h=float(volume),
                book_depth_dollars=0.0,
                close_time=close_dt,
                category=category,
            )
        except Exception as e:
            log.debug("Error evaluating market %s: %s", m.get("ticker"), e)
            return None


# ─── RSS Feed Ingestion ────────────────────────────────────────────────────────

import feedparser
import re

RSS_SOURCES = {
    # General / Breaking (via Google News proxies for AP & Reuters)
    "ap": "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en",
    "reuters": "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en",
    # Politics / Policy
    "politico": "https://rss.politico.com/politics-news.xml",
    "hill": "https://thehill.com/feed/",
    # Economics
    "fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "econ": "https://news.google.com/rss/search?q=economic+data+GDP+inflation+jobs+report&hl=en-US&gl=US&ceid=US:en",
    # Weather
    "weather": "https://news.google.com/rss/search?q=hurricane+tornado+weather+forecast+NOAA&hl=en-US&gl=US&ceid=US:en",
    # Crypto
    "crypto": "https://cointelegraph.com/rss",
    # Sports
    "sports": "https://www.espn.com/espn/rss/news",
}


class RSSIngester:
    """
    Polls RSS feeds and maps headlines to active Kalshi markets.
    Deduplication happens in BayesianEngine.
    """

    def __init__(self, poll_interval_sec: float = 60.0):
        self._poll_interval = poll_interval_sec
        self._seen_ids: set[str] = set()

    def fetch_signals(self, market_opportunities: list[MarketOpportunity],
                      source_weights: dict[str, float]) -> list[Signal]:
        """
        Fetch all RSS feeds, map entries to markets, return Signal list.
        Only returns new entries (not seen in prior calls).
        """
        signals: list[Signal] = []
        for source_name, url in RSS_SOURCES.items():
            weight = source_weights.get(source_name, 0.5)
            if weight == 0:
                continue
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    entry_id = entry.get("id") or entry.get("link", "")
                    if entry_id in self._seen_ids:
                        continue
                    self._seen_ids.add(entry_id)

                    title = entry.get("title", "")
                    summary = entry.get("summary", "")

                    for opp in market_opportunities:
                        if self._matches_market(title, summary, opp):
                            sentiment, magnitude = self._analyze_sentiment(title, summary)
                            signals.append(Signal(
                                market_id=opp.ticker,
                                source=source_name,
                                headline=title,
                                body=summary,
                                sentiment=sentiment * weight,
                                magnitude=magnitude,
                                timestamp=time.time(),
                            ))
            except Exception as e:
                log.warning("RSS fetch failed for %s: %s", source_name, e)
        return signals

    _STOP_WORDS = frozenset({
        "the", "and", "for", "are", "but", "not", "you", "all", "can",
        "her", "was", "one", "our", "out", "his", "has", "its", "had",
        "win", "won", "will", "what", "when", "where", "which", "who",
        "whom", "how", "that", "this", "these", "those", "than", "then",
        "there", "their", "they", "them", "with", "from", "into", "about",
        "after", "before", "between", "under", "over", "above", "below",
        "during", "next", "have", "been", "being", "does", "done", "make",
        "made", "each", "more", "most", "some", "such", "only", "very",
        "also", "just", "say", "says", "said",
    })

    def _matches_market(self, title: str, summary: str, opp: MarketOpportunity) -> bool:
        """Keyword overlap between RSS entry and market title (>= 2 non-stop words)."""
        market_words = {w for w in re.findall(r'[a-z]+', opp.title.lower())
                        if len(w) > 3 and w not in self._STOP_WORDS}
        text_words = {w for w in re.findall(r'[a-z]+', (title + " " + summary).lower())
                      if len(w) > 3 and w not in self._STOP_WORDS}
        return len(market_words & text_words) >= 2

    def _analyze_sentiment(self, title: str, body: str) -> tuple[float, float]:
        """
        Keyword-based sentiment analysis.
        Returns (sentiment, magnitude): sentiment in [-1, 1], magnitude in [0, 1].
        """
        text = (title + " " + body).lower()
        positive = ["confirmed", "approved", "passed", "ruled yes",
                    "wins", "victory", "upheld", "signed"]
        negative = ["denied", "rejected", "reversed", "ruled no",
                    "loses", "overturned", "vetoed", "dismissed"]
        pos_count = sum(1 for w in positive if w in text)
        neg_count = sum(1 for w in negative if w in text)
        total = pos_count + neg_count
        if total == 0:
            return 0.0, 0.1
        sentiment = (pos_count - neg_count) / total
        magnitude = min(1.0, total / 3.0)
        return sentiment, magnitude


# ─── Discord Notifications ─────────────────────────────────────────────────────

class Discord:
    """
    Sends trading notifications to a Discord webhook.

    Only posts ACTIONABLE events:
    - Trade placed / filled
    - Position settled (win/loss)
    - Drawdown threshold hit
    - Kill switch triggered
    - Bot crash (with 4-hour cooldown) or restart
    - Phase transition
    - Daily summary

    Routine scan results, API errors, and "0 trades" messages
    stay in journald only.
    """

    _ET = timezone(timedelta(hours=-5))

    def __init__(self, webhook_url: Optional[str] = None):
        self._url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "")
        self._enabled = bool(self._url)
        self._last_crash_ts: float = 0.0
        self._crash_cooldown_sec: float = 14400  # 4 hours between crash msgs

    def _in_quiet_hours(self) -> bool:
        """No alerts between 11pm and 8am ET."""
        hour = datetime.now(self._ET).hour
        return hour >= 23 or hour < 8

    def _send(self, message: str) -> None:
        if not self._enabled:
            log.info("Discord (disabled): %s", message)
            return
        if self._in_quiet_hours():
            log.info("Discord (quiet hours): %s", message[:200])
            return
        try:
            httpx.post(self._url, json={"content": message}, timeout=5.0)
        except Exception as e:
            log.warning("Discord send failed: %s", e)

    def bot_started(self, balance_dollars: float, cancelled_orders: int,
                    mode: str = "demo") -> None:
        self._send(
            f"Bot started ({mode}) | Balance: ${balance_dollars:.2f} | "
            f"Cancelled {cancelled_orders} stale orders"
        )

    def trade_placed(self, ticker: str, side: str, contracts: int,
                     price_cents: int, ev: float) -> None:
        self._send(
            f"ORDER | {ticker} | {side.upper()} {contracts} @ {price_cents}c "
            f"| EV: ${ev:.2f}"
        )

    def trade_filled(self, ticker: str, side: str, contracts: int,
                     price_cents: int) -> None:
        self._send(
            f"FILLED | {ticker} | {side.upper()} {contracts} @ {price_cents}c"
        )

    def position_closed(self, ticker: str, pnl_dollars: float,
                        won: bool = True) -> None:
        icon = "W" if won else "L"
        sign = "+" if pnl_dollars >= 0 else ""
        self._send(f"SETTLED [{icon}] | {ticker} | P&L: {sign}${pnl_dollars:.2f}")

    def daily_summary(self, trades: int, net_pnl: float, win_rate: float,
                      bankroll: float, phase: str) -> None:
        self._send(
            f"DAILY | Trades: {trades} | P&L: ${net_pnl:+.2f} | "
            f"Win rate: {win_rate:.1%} | Bankroll: ${bankroll:.2f} | Phase: {phase}"
        )

    def drawdown_alert(self, pct: float) -> None:
        self._send(f"DRAWDOWN WARNING | {pct:.1%} rolling 7-day loss")

    def kill_switch(self, reason: str) -> None:
        self._send(f"KILL SWITCH | {reason}")

    def crash(self, error: str) -> None:
        now = time.time()
        if now - self._last_crash_ts < self._crash_cooldown_sec:
            log.info("Discord crash suppressed (cooldown): %s", error[:200])
            return
        self._last_crash_ts = now
        self._send(f"CRASH | {error[:500]}")

    def phase_transition(self, old_phase: str, new_phase: str, bankroll: float) -> None:
        self._send(f"PHASE UP | {old_phase} -> {new_phase} | Bankroll: ${bankroll:.2f}")


# ─── Bankroll Tracker ──────────────────────────────────────────────────────────

class BankrollTracker:
    """
    Tracks bankroll, phase, and session P&L.
    Source of truth is the Kalshi API balance endpoint.
    """

    def __init__(self, client: KalshiClient):
        self._client = client
        self._start_of_day_cents: Optional[int] = None
        self._trade_count = 0
        self._wins = 0

    def refresh(self) -> int:
        """Fetch current balance from Kalshi API. Returns cents."""
        resp = self._client.get_balance()
        balance = resp.get("balance", 0)
        if self._start_of_day_cents is None:
            self._start_of_day_cents = balance
        return balance

    def daily_pnl_dollars(self, current_cents: int) -> float:
        if self._start_of_day_cents is None:
            return 0.0
        return (current_cents - self._start_of_day_cents) / 100.0

    def record_trade(self, won: bool) -> None:
        self._trade_count += 1
        if won:
            self._wins += 1

    @property
    def win_rate(self) -> float:
        if self._trade_count == 0:
            return 1.0
        return self._wins / self._trade_count


# ─── Kalshi Executor ───────────────────────────────────────────────────────────

@dataclass
class TradeDecision:
    ticker: str
    side: str          # "yes" or "no"
    contracts: int
    price_cents: int
    expected_ev: float
    reason: str


class KalshiExecutor:
    """
    Executes trade decisions as Kalshi limit orders.

    - Always uses limit orders (never market orders).
    - client_order_id (UUID) on every order for deduplication.
    - Cancels orders with < MIN_FILL_PERCENT after FILL_TIMEOUT_SEC.
    - Tracks pending orders and updates risk engine on fill/cancel.
    """

    def __init__(self, client: KalshiClient, risk: RiskEngine,
                 discord: Discord, strategy: Any):
        self._client = client
        self._risk = risk
        self._discord = discord
        self._strategy = strategy
        # order_id → (TradeDecision, placed_at_timestamp)
        self._pending: dict[str, tuple[TradeDecision, float]] = {}

    def execute(self, decision: TradeDecision) -> Optional[str]:
        """
        Place a limit order. Returns order_id or None if rejected/failed.
        """
        if self._risk.kill_switch_active():
            log.warning("Kill switch active — not trading")
            return None

        if not self._client.is_exchange_open():
            log.warning("Exchange not open — skipping trade on %s", decision.ticker)
            return None

        position_value_cents = decision.price_cents * decision.contracts
        if not self._risk.can_open_position(position_value_cents):
            log.info("Portfolio cap reached — skipping %s", decision.ticker)
            return None

        client_order_id = str(uuid.uuid4())
        try:
            resp = self._client.create_order(
                ticker=decision.ticker,
                side=decision.side,
                contracts=decision.contracts,
                price_cents=decision.price_cents,
                client_order_id=client_order_id,
            )
            order = resp.get("order", {})
            order_id = order.get("order_id", "")
            if not order_id:
                log.error("Order placement returned no order_id: %s", resp)
                return None

            self._pending[order_id] = (decision, time.time())
            self._risk.add_open_position(decision.ticker, position_value_cents)
            self._discord.trade_placed(
                decision.ticker, decision.side, decision.contracts,
                decision.price_cents, decision.expected_ev,
            )
            log.info(
                "Order placed: %s %s x%d @ %dc | id=%s",
                decision.ticker, decision.side, decision.contracts,
                decision.price_cents, order_id[:8],
            )
            return order_id
        except Exception as e:
            log.error("Order execution error on %s: %s", decision.ticker, e)
            return None

    def check_fills(self) -> None:
        """
        Poll all pending orders. Cancel those that haven't filled enough
        within FILL_TIMEOUT_SEC. Handle fills for completed orders.
        """
        now = time.time()
        to_cancel: list[str] = []

        for order_id, (decision, placed_at) in list(self._pending.items()):
            age = now - placed_at
            if age < 10:  # don't poll too soon after placement
                continue
            try:
                resp = self._client.get_order(order_id)
                order = resp.get("order", {})
                fill_count = order.get("fill_count", 0)
                remaining = order.get("remaining_count", 0)
                total = fill_count + remaining
                status = order.get("status", "")

                if status == "executed":
                    self._on_fill(order_id, decision, fill_count)
                elif age > self._strategy.FILL_TIMEOUT_SEC:
                    fill_pct = fill_count / total if total > 0 else 0.0
                    if fill_pct < self._strategy.MIN_FILL_PERCENT:
                        to_cancel.append(order_id)
                    else:
                        # Partial fill is acceptable — treat as filled
                        self._on_fill(order_id, decision, fill_count)
            except Exception as e:
                log.warning("Error checking order %s: %s", order_id[:8], e)

        for order_id in to_cancel:
            self._cancel(order_id)

    def _on_fill(self, order_id: str, decision: TradeDecision, filled: int) -> None:
        """Handle a confirmed fill."""
        self._discord.trade_filled(
            decision.ticker, decision.side, filled, decision.price_cents,
        )
        self._pending.pop(order_id, None)
        log.info("Fill confirmed: %s %d contracts", decision.ticker, filled)

    def _cancel(self, order_id: str) -> None:
        """Cancel an underfilled order and release portfolio capacity."""
        try:
            self._client.cancel_order(order_id)
            log.info("Cancelled underfilled order %s", order_id[:8])
        except Exception as e:
            log.warning("Cancel failed for order %s: %s", order_id[:8], e)
        decision, _ = self._pending.pop(order_id, (None, None))
        if decision:
            self._risk.close_position(decision.ticker)


# ─── Crash Recovery ────────────────────────────────────────────────────────────

class CrashRecovery:
    """
    On startup: reconciles open orders and positions with Kalshi API.
    Cancels all resting orders to start clean.
    Enters safe mode until explicitly approved.

    Safe mode = unwind only, no new trades.
    """

    def __init__(self, client: KalshiClient, discord: Discord, mode: str = "demo"):
        self._client = client
        self._discord = discord
        self._mode = mode
        self.safe_mode = True

    def reconcile(self) -> dict:
        """
        Fetch and cancel all resting orders, get current positions and balance.
        Returns {"balance_cents": int, "open_positions": list, "cancelled_orders": int}
        """
        cancelled = 0
        positions: list[dict] = []
        balance = 0

        try:
            # Cancel all resting orders
            orders_resp = self._client.get_orders(status="resting")
            orders = orders_resp.get("orders", [])
            for order in orders:
                order_id = order.get("order_id", "")
                if not order_id:
                    continue
                try:
                    self._client.cancel_order(order_id)
                    cancelled += 1
                except Exception as e:
                    log.warning("Could not cancel order %s: %s", order_id[:8], e)

            # Get current positions
            positions_resp = self._client.get_positions()
            positions = positions_resp.get("market_positions", [])

            # Get current balance
            balance_resp = self._client.get_balance()
            balance = balance_resp.get("balance", 0)

            log.info(
                "Crash recovery: cancelled %d orders, %d positions, balance $%.2f",
                cancelled, len(positions), balance / 100,
            )
            self._discord.bot_started(balance / 100, cancelled,
                                       mode=self._mode)
        except Exception as e:
            log.error("Crash recovery error: %s", e)
            self._discord.crash(f"Crash recovery FAILED: {e}")

        return {
            "balance_cents": balance,
            "open_positions": positions,
            "cancelled_orders": cancelled,
        }

    def approve_trading(self) -> None:
        """Exit safe mode. Call after manual review or automatically in demo."""
        self.safe_mode = False
        log.info("Safe mode OFF — trading resumed")


# ─── Trading Bot Orchestrator ──────────────────────────────────────────────────

class TradingBot:
    """
    Main trading loop. Orchestrates all infra components.
    Called by bot.py with the --demo or --live flag.

    Loop: scan markets → ingest RSS signals → Bayesian update →
          evaluate each opportunity → EV check → execute if warranted

    In demo mode: safe mode is auto-approved.
    In live mode: safe mode requires manual approval via approve_trading().
    """

    def __init__(self, env: Env, strategy: Any):
        self._env = env
        self._strategy = strategy
        self._client = KalshiClient(env=env)
        self._discord = Discord()
        self._bayesian = BayesianEngine(
            cluster_window_sec=strategy.SIGNAL_CLUSTER_WINDOW_SEC,
            confidence_decay_rate=strategy.CONFIDENCE_DECAY_RATE,
        )
        self._rss = RSSIngester()
        self._scanner = MarketScanner(self._client)
        self._recovery = CrashRecovery(self._client, self._discord,
                                       mode=env.value)

        self._bankroll_tracker: Optional[BankrollTracker] = None
        self._risk: Optional[RiskEngine] = None
        self._executor: Optional[KalshiExecutor] = None

    def start(self) -> None:
        log.info("Starting bot in %s mode", self._env.value.upper())
        result = self._recovery.reconcile()
        balance_cents = result["balance_cents"]

        self._bankroll_tracker = BankrollTracker(self._client)

        # In demo mode, the API often returns $0 (no funded balance).
        # Fall back to the strategy's starting bankroll so paper trading works.
        if balance_cents == 0 and self._env == Env.DEMO:
            balance_cents = int(self._strategy.DEMO_STARTING_BALANCE_DOLLARS * 100)
            log.info(
                "Demo balance is $0 (funding unavailable) — using paper bankroll $%.2f",
                balance_cents / 100,
            )

        self._risk = RiskEngine(bankroll_cents=balance_cents)
        self._executor = KalshiExecutor(
            self._client, self._risk, self._discord, self._strategy
        )

        log.info(
            "Bankroll: $%.2f | Phase: %s | Safe mode: %s",
            balance_cents / 100,
            self._risk.phase.value,
            self._recovery.safe_mode,
        )

        # Auto-approve safe mode in demo; live requires manual review
        if self._env == Env.DEMO:
            self._recovery.approve_trading()

        self._run_loop()

    def _run_loop(self) -> None:
        """Main loop: scan, signal, execute. Runs until interrupted or kill switch."""
        scan_interval = 30  # seconds between full market scans
        last_daily_summary = time.time()

        while True:
            try:
                if self._risk.kill_switch_active():
                    log.critical("Kill switch active — halting all trading")
                    self._discord.kill_switch(
                        "15% rolling drawdown exceeded. Manual review required."
                    )
                    break

                if not self._recovery.safe_mode:
                    self._tick()

                self._executor.check_fills()

                # Daily summary (every 24h)
                if time.time() - last_daily_summary >= 86400:
                    balance = self._bankroll_tracker.refresh()
                    self._discord.daily_summary(
                        trades=self._bankroll_tracker._trade_count,
                        net_pnl=self._bankroll_tracker.daily_pnl_dollars(balance),
                        win_rate=self._bankroll_tracker.win_rate,
                        bankroll=balance / 100,
                        phase=self._risk.phase.value,
                    )
                    last_daily_summary = time.time()

                time.sleep(scan_interval)

            except KeyboardInterrupt:
                log.info("Interrupted by user — shutting down cleanly")
                break
            except Exception as e:
                log.exception("Tick error: %s", e)
                self._discord.crash(str(e))
                time.sleep(60)  # pause before retrying

    def _tick(self) -> None:
        """One trading cycle: scan → RSS → Bayesian update → trade decisions."""
        opportunities = self._scanner.scan(self._strategy)
        if not opportunities:
            log.debug("No opportunities found this scan")
            return

        # Ingest RSS signals and update Bayesian posteriors
        rss_signals = self._rss.fetch_signals(
            opportunities, self._strategy.RSS_SOURCE_WEIGHTS
        )
        for sig in rss_signals:
            self._bayesian.update(sig)

        # Evaluate each opportunity
        for opp in opportunities:
            self._consider_trade(opp)

    def _consider_trade(self, opp: MarketOpportunity) -> None:
        """Evaluate one market. Delegates signal logic to strategy.py."""
        from strategy import SignalInputs, compute_signals, should_trade

        # Fetch fresh order book
        try:
            ob_data = self._client.get_orderbook(opp.ticker)
            book = OrderBook.from_api(ob_data)
        except Exception as e:
            log.debug("Could not fetch book for %s: %s", opp.ticker, e)
            return

        opp.book = book

        # Book depth filter (dollars) — hard safety rail stays in infra
        depth = book.depth_at_ticks(n=3)
        depth_dollars = (depth["bid"] + depth["ask"]) * opp.yes_bid / 100.0
        if depth_dollars < self._strategy.MIN_BOOK_DEPTH:
            return

        # Gather raw signals from infra
        lmsr_p = lmsr_price(q_yes=0.0, q_no=0.0, b=100.0)

        td_raw = 0.0
        if opp.close_time:
            td_raw = time_decay_signal(
                opp.close_time, opp.yes_bid, opp.yes_ask,
                max_hours=getattr(self._strategy, 'TIME_DECAY_MAX_HOURS', 4.0),
            )

        vs_raw = 0.0
        if opp.volume_24h > 0:
            vs_raw = volume_spike_signal(
                opp.volume_24h, self._scanner._median_volume,
                opp.yes_bid, opp.yes_ask,
                threshold=getattr(self._strategy, 'VOLUME_SPIKE_THRESHOLD', 3.0),
            )

        bayes_p, bayes_confidence = self._bayesian.posterior(opp.ticker)

        # Strategy decides how to combine signals
        signal = compute_signals(SignalInputs(
            book_imbalance=book.imbalance(),
            lmsr_prior=lmsr_p,
            bayes_posterior=bayes_p,
            bayes_confidence=bayes_confidence,
            time_decay_raw=td_raw,
            volume_spike_raw=vs_raw,
            yes_bid=opp.yes_bid,
            yes_ask=opp.yes_ask,
            category=opp.category,
        ))

        # Position sizing (half-Kelly cap enforced by RiskEngine)
        contracts = self._risk.position_size(signal.p_hat, signal.price_cents)
        if contracts <= 0:
            return

        # EV calculation (infra math)
        slippage = estimate_slippage(book, contracts)
        params = TradeParams(
            p_hat=signal.p_hat,
            price_cents=signal.price_cents,
            contracts=contracts,
            fee_per_contract=0.02,
            slippage_cents=slippage,
        )
        ev = calculate_ev(params)
        fee_total = contracts * 0.02
        edge_cents = signal.p_hat * 100 - signal.price_cents

        # Strategy decides whether to trade
        passed, reason = should_trade(signal.confidence, edge_cents, ev, fee_total)
        if not passed:
            return

        decision = TradeDecision(
            ticker=opp.ticker,
            side=signal.side,
            contracts=contracts,
            price_cents=signal.price_cents,
            expected_ev=ev,
            reason=(
                f"p_hat={signal.p_hat:.3f} edge={edge_cents:.1f}c "
                f"conf={signal.confidence:.3f} ev=${ev:.3f}"
            ),
        )
        self._executor.execute(decision)

    def print_status(self) -> None:
        """Print current bot status to stdout."""
        balance_resp = self._client.get_balance()
        balance = balance_resp.get("balance", 0)
        positions_resp = self._client.get_positions()
        positions = positions_resp.get("market_positions", [])
        open_pos = [p for p in positions if p.get("position", 0) != 0]
        orders_resp = self._client.get_orders(status="resting")
        open_orders = orders_resp.get("orders", [])
        phase = self._risk.phase.value if self._risk else "unknown"

        print(f"Bankroll:        ${balance/100:.2f}")
        print(f"Phase:           {phase}")
        print(f"Open positions:  {len(open_pos)}")
        print(f"Resting orders:  {len(open_orders)}")
        if self._bankroll_tracker:
            print(f"Win rate:        {self._bankroll_tracker.win_rate:.1%}")
            print(f"Trade count:     {self._bankroll_tracker._trade_count}")
