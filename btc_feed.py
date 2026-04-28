"""
btc_feed.py — Real-time BTC price feed via Binance.US WebSocket streams.

Consumes three streams via a single multiplexed connection:
  - btcusd@trade        → last trade price
  - btcusd@bookTicker   → best bid/ask (BBO)
  - btcusd@kline_1m     → 1-minute candle closes for vol estimation

Produces a synthetic fair price:
  fair = 0.6 * mid + 0.4 * last_trade
  where mid = (best_bid + best_ask) / 2

Feed health states:
  LIVE      — updated within 2s
  DEGRADED  — 2-5s since last update (widen required edge)
  STALE     — >5s since last update (do NOT open new positions)
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from enum import Enum
from typing import Optional

import websocket

from config import BTC_VOL_WINDOW_MINUTES
from strategy import BTCStrategy, DEFAULT_BTC_STRATEGY

log = logging.getLogger("btc_feed")

# Binance.US combined stream — multiplexes trade, bookTicker, and kline
BINANCE_US_COMBINED = "wss://stream.binance.us:9443/stream?streams=btcusd@trade/btcusd@bookTicker/btcusd@kline_1m"


class FeedHealth(Enum):
    LIVE = "LIVE"           # updated within 2s
    DEGRADED = "DEGRADED"   # 2-5s stale — widen edge requirement
    STALE = "STALE"         # >5s stale — do not trade


class BTCFeed:
    """
    Streaming BTC/USD feed from Binance.US WebSocket.

    Tracks best bid, best ask, last trade, and computes a synthetic
    fair price. Maintains rolling 1-min candle closes for vol estimation.
    """

    def __init__(self, vol_window_minutes: int = BTC_VOL_WINDOW_MINUTES):
        self._vol_window = vol_window_minutes
        self._candle_closes: deque[float] = deque(maxlen=vol_window_minutes)

        # Price components
        self._best_bid: Optional[float] = None
        self._best_ask: Optional[float] = None
        self._last_trade: Optional[float] = None
        self._last_update_ts: float = 0.0

        self._lock = threading.Lock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False

    # ─── Public interface ────────────────────────────────────────────────

    @property
    def fair_price(self) -> Optional[float]:
        """Synthetic fair: 0.6 * mid + 0.4 * last_trade."""
        with self._lock:
            return self._compute_fair()

    @property
    def latest_price(self) -> Optional[float]:
        """Alias for fair_price — backward compat with btc_bot.py."""
        return self.fair_price

    @property
    def mid(self) -> Optional[float]:
        with self._lock:
            if self._best_bid and self._best_ask:
                return (self._best_bid + self._best_ask) / 2.0
            return None

    @property
    def spread_bps(self) -> Optional[float]:
        """Spread in basis points."""
        with self._lock:
            if self._best_bid and self._best_ask and self._best_bid > 0:
                return ((self._best_ask - self._best_bid) / self._best_bid) * 10000
            return None

    @property
    def price_age_sec(self) -> float:
        with self._lock:
            if self._last_update_ts == 0:
                return float("inf")
            return time.time() - self._last_update_ts

    @property
    def health(self) -> FeedHealth:
        age = self.price_age_sec
        if age <= 2.0:
            return FeedHealth.LIVE
        elif age <= 5.0:
            return FeedHealth.DEGRADED
        else:
            return FeedHealth.STALE

    @property
    def is_connected(self) -> bool:
        return self._connected

    def realized_vol(
        self,
        strategy: BTCStrategy = DEFAULT_BTC_STRATEGY,
    ) -> tuple[float, int]:
        """Returns (annualized_vol, sample_count) from recent candle closes."""
        from btc_model import compute_realized_vol
        with self._lock:
            closes = list(self._candle_closes)
        return compute_realized_vol(
            closes,
            candle_interval_sec=60,
            vol_floor_annual=strategy.vol_floor_annual,
        )

    def recent_closes(self) -> list[float]:
        """Return copy of candle close buffer for momentum calculation."""
        with self._lock:
            return list(self._candle_closes)

    def snapshot(self) -> dict:
        """Full feed state for logging."""
        with self._lock:
            fair = self._compute_fair()
            mid = (self._best_bid + self._best_ask) / 2.0 if self._best_bid and self._best_ask else None
            spread = ((self._best_ask - self._best_bid) / self._best_bid * 10000) if self._best_bid and self._best_ask and self._best_bid > 0 else None
            age = time.time() - self._last_update_ts if self._last_update_ts else None
            # Compute health inline (avoid re-acquiring lock via self.health)
            if age is not None and age <= 2.0:
                h = FeedHealth.LIVE
            elif age is not None and age <= 5.0:
                h = FeedHealth.DEGRADED
            else:
                h = FeedHealth.STALE
            return {
                "fair": fair,
                "mid": mid,
                "last_trade": self._last_trade,
                "best_bid": self._best_bid,
                "best_ask": self._best_ask,
                "spread_bps": spread,
                "age_sec": age,
                "health": h.value,
                "candle_count": len(self._candle_closes),
            }

    # ─── Internal ────────────────────────────────────────────────────────

    def _compute_fair(self) -> Optional[float]:
        """Must hold self._lock when calling."""
        mid = None
        if self._best_bid and self._best_ask:
            mid = (self._best_bid + self._best_ask) / 2.0

        if mid and self._last_trade:
            return 0.6 * mid + 0.4 * self._last_trade
        elif mid:
            return mid
        elif self._last_trade:
            return self._last_trade
        return None

    # ─── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        # WS thread for streaming updates
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        # Parallel REST poll thread as safety net — keeps price fresh if WS is silent
        self._rest_thread = threading.Thread(target=self._rest_keepalive, daemon=True)
        self._rest_thread.start()
        log.info("BTCFeed started (vol_window=%d min)", self._vol_window)

    def stop(self) -> None:
        self._running = False
        if self._ws:
            self._ws.close()
        log.info("BTCFeed stopped")

    def _run_forever(self) -> None:
        """Connect to Binance.US combined stream with auto-reconnect.
        Falls back to REST polling if WS fails."""
        ws_failures = 0
        while self._running:
            try:
                self._connect()
            except Exception as e:
                log.warning("WS error: %s", e)
            ws_failures += 1
            if ws_failures >= 3:
                log.info("WS failed %d times — falling back to REST polling", ws_failures)
                self._poll_rest_forever()
                return
            if self._running:
                log.info("Reconnecting WS in 3s (attempt %d)", ws_failures)
                time.sleep(3)

    def _poll_rest_forever(self) -> None:
        """Fallback REST polling every 10s."""
        import httpx
        last_candle = time.time()
        while self._running:
            try:
                resp = httpx.get(
                    "https://api.binance.us/api/v3/ticker/price",
                    params={"symbol": "BTCUSD"}, timeout=5.0,
                )
                resp.raise_for_status()
                price = float(resp.json()["price"])
                now = time.time()
                with self._lock:
                    self._last_trade = price
                    self._last_update_ts = now
                    if now - last_candle >= 60:
                        self._candle_closes.append(price)
                        last_candle = now
            except Exception as e:
                log.debug("REST poll failed: %s", e)
            time.sleep(10)

    def _connect(self) -> None:
        """Connect to Binance.US combined WebSocket stream."""

        def on_message(ws, raw):
            try:
                msg = json.loads(raw)
                stream = msg.get("stream", "")
                data = msg.get("data", {})

                with self._lock:
                    if "trade" in stream:
                        # Trade stream: {"p": "71234.56", "q": "0.001", ...}
                        price = float(data.get("p", 0))
                        if price > 0:
                            self._last_trade = price
                            self._last_update_ts = time.time()

                    elif "bookTicker" in stream:
                        # BBO stream: {"b": "71234.00", "B": "1.5", "a": "71235.00", "A": "0.8"}
                        bid = float(data.get("b", 0))
                        ask = float(data.get("a", 0))
                        if bid > 0 and ask > 0:
                            self._best_bid = bid
                            self._best_ask = ask
                            self._last_update_ts = time.time()

                    elif "kline" in stream:
                        # Kline stream: {"k": {"c": "71234.56", "x": true, ...}}
                        kline = data.get("k", {})
                        if kline.get("x", False):  # candle closed
                            close_price = float(kline.get("c", 0))
                            if close_price > 0:
                                self._candle_closes.append(close_price)

            except Exception as e:
                log.debug("WS parse error: %s", e)

        def on_open(ws):
            self._connected = True
            log.info("Binance.US WS connected (combined stream)")

        def on_close(ws, status, msg):
            self._connected = False
            log.info("Binance.US WS closed: %s", status)

        def on_error(ws, error):
            log.warning("Binance.US WS error: %s", error)

        self._ws = websocket.WebSocketApp(
            BINANCE_US_COMBINED,
            on_message=on_message,
            on_open=on_open,
            on_close=on_close,
            on_error=on_error,
        )
        # If no data after 30s, close and let reconnect logic handle it
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def _rest_keepalive(self) -> None:
        """Safety net: poll REST every 10s if WS hasn't updated in 3s."""
        import httpx
        last_candle = time.time()
        while self._running:
            age = self.price_age_sec
            if age > 3.0:
                try:
                    resp = httpx.get(
                        "https://api.binance.us/api/v3/ticker/price",
                        params={"symbol": "BTCUSD"}, timeout=5.0,
                    )
                    resp.raise_for_status()
                    price = float(resp.json()["price"])
                    now = time.time()
                    with self._lock:
                        self._last_trade = price
                        self._last_update_ts = now
                        if now - last_candle >= 60:
                            self._candle_closes.append(price)
                            last_candle = now
                except Exception:
                    pass
            time.sleep(10)

    # ─── Seed from REST (startup only) ───────────────────────────────────

    def seed_from_rest(self) -> None:
        """Seed candle buffer and initial price from REST API at startup."""
        import httpx

        sources = [
            ("Binance", "https://api.binance.com/api/v3/klines",
             {"symbol": "BTCUSDT", "interval": "1m", "limit": self._vol_window}),
            ("Binance.US", "https://api.binance.us/api/v3/klines",
             {"symbol": "BTCUSD", "interval": "1m", "limit": self._vol_window}),
        ]

        for name, url, params in sources:
            try:
                resp = httpx.get(url, params=params, timeout=10.0)
                resp.raise_for_status()
                klines = resp.json()
                with self._lock:
                    self._candle_closes.clear()
                    for k in klines:
                        self._candle_closes.append(float(k[4]))
                    if klines:
                        self._last_trade = float(klines[-1][4])
                        self._last_update_ts = time.time()
                log.info(
                    "Seeded %d candles from %s (latest=$%.2f)",
                    len(self._candle_closes), name, self._last_trade or 0,
                )
                return
            except Exception as e:
                log.debug("%s seed failed: %s", name, e)

        log.warning("REST seed failed — starting with empty candle buffer")
