# Kalshi Trading Bot — Agent Program

## Your Role

You are an autonomous trading agent. You build, test, and iteratively improve a Kalshi prediction market trading bot. You default to NOT trading. You only trade when the math is overwhelming. Your goal is to find the path to $500+/day in net profit through high-frequency small-edge trades across 50+ markets. The autoresearch loop is how you get there.

## Three File Rule

You follow the Karpathy autoresearch pattern. Three files matter:

| File | You Edit? | Purpose |
|------|-----------|---------|
| strategy.py | YES — this is your experiment file | Signal weights, thresholds, filters, all tunable params |
| infra.py | NO — never touch | All fixed infrastructure |
| program.md | NO — never touch | Your instructions |

You ONLY modify strategy.py during the autoresearch loop. Everything else is built once in Phase 1 and left alone.

## Phase 1: Build Infrastructure (infra.py + bot.py)

Build these once. They do not change during the autoresearch loop.

Reference kalshi-openapi.yaml in the project root for all API endpoint details.

### bot.py — Entry Point
- --demo: paper trading on Kalshi demo (https://demo-api.kalshi.co/trade-api/v2)
- --simulate: backtest against historical resolved markets with realistic execution
- --live: real trades on Kalshi production (https://api.elections.kalshi.com/trade-api/v2)
- --status: current bankroll, phase, open positions, daily P&L, trade count

### infra.py — All Fixed Infrastructure

This is one file containing all modules. The agent never modifies it after Phase 1.

**Kalshi API Client:**
- Auth: RSA-PSS SHA256 signing. Three headers per request: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP
- Signature: concatenate timestamp_ms + HTTP_METHOD + path (without query params), sign with RSA-PSS, base64 encode
- SDK: kalshi-python-sync (NOT deprecated kalshi-python)
- REST: /markets, /events, /portfolio/orders, /portfolio/balance, /exchange/status
- WebSocket: wss://demo-api.kalshi.co/trade-api/ws/v2 (demo) or wss://api.elections.kalshi.com/trade-api/ws/v2 (production)
- Public WS channels (no auth): ticker, trade, market_lifecycle_v2, multivariate
- Private WS channels (auth required): order fills, portfolio updates
- Demo vs production environment switching via config
- Exchange hours awareness (check /exchange/status and /exchange/schedule)
- Order placement: POST /portfolio/orders with client_order_id (UUID) for deduplication

**Order Book Analysis:**
- Fetch order book from Kalshi API
- Compute weighted mid-price from book imbalance
- Measure depth at ±1, ±2, ±3 ticks from mid
- Detect spread compression/expansion
- Volume-weighted average price calculation

**LMSR Theoretical Prior:**
- Cost function: C(q) = b * ln(sum(e^(qi/b)))
- Price function: pi(q) = e^(qi/b) / sum(e^(qj/b))
- Used as shape constraint only — weight determined by strategy.py
- If autoresearch loop finds no predictive value, weight goes to zero

**Bayesian Engine:**
- Sequential updating in log-space: log P(H|D) = log P(H) + sum(log P(Dk|H)) - log Z
- Posterior probability with confidence interval
- Signal deduplication: hash headline + first paragraph, cluster within time window, each cluster = one signal
- Negative evidence handling: corrections/retractions trigger downweighting
- Confidence decay when no confirming evidence arrives
- Cooldown per market after signal (duration set by strategy.py)

**EV Calculator:**
- EV = p_hat * (1 - price) - (1 - p_hat) * price - fee - expected_slippage - exit_cost
- Kalshi fee model: ~$0.02/contract max taker, lower for maker. No settlement fees.
- Slippage model: estimated from order book depth at trade size
- All parameters from live market data, nothing hardcoded

**Risk Engine:**
- Half-Kelly position sizing: f* = 0.5 * (bp - q) / b
- Tier-aware risk caps (1-2% Phase 1, 2-3% Phase 2-3)
- Portfolio exposure cap: 25% of bankroll in open positions max
- Win rate monitor: if rolling win rate drops below 90%, tighten all filters by 1 tier
- Kill switch: 15% drawdown in rolling 7 days halts trading

**Kalshi Executor:**
- Limit orders only
- client_order_id (UUID) on every order for deduplication
- Minimum fill threshold: cancel if < 30% filled within timeout
- Stale order cancellation after configurable timeout
- Residual/dust position auto-exit
- Fill tracking and bankroll update on fill
- Exchange hours check before any order placement

**Bankroll Tracker:**
- Compute current bankroll from GET /portfolio/balance (returns cents, divide by 100)
- Auto-detect current phase/tier
- Calculate position sizes based on tier risk percentage
- Weekly P&L tracking
- Withdrawal calculator (Phase 3 only, 20% of weekly profits, only when 4-week Sharpe positive)

**Market Scanner:**
- Poll ALL active Kalshi events and markets via GET /markets?status=open
- Apply hard filters per scan:
  - 24h volume >= threshold (from strategy.py)
  - Book depth >= threshold (from strategy.py)
  - Spread <= threshold (from strategy.py)
- Estimate hold time / time to resolution
- Prefer markets where capital recycles in 2-4 hours
- Respect exchange trading hours

**RSS Feed Ingestion:**
- feedparser polling on configurable interval
- Source list: AP, Reuters, government RSS feeds, court filing feeds
- Event extraction: entity, event type, sentiment, magnitude
- Map extracted events to active Kalshi markets by matching event titles/descriptions
- Source clustering and deduplication before passing to Bayesian engine

**Kalshi WebSocket:**
- Subscribe to ticker channel for real-time price updates
- Subscribe to trade channel for executed trades
- Order book depth monitoring via orderbook channel
- Market lifecycle events

**Discord Notifications:**
- Trade executed (market ticker, direction, contracts, price, expected profit)
- Position closed/settled (P&L)
- Daily summary (trades, net P&L, win rate, bankroll, phase)
- Drawdown alert at 5%, 10%, 15%
- Phase transition
- Bot crash / API failure / restart
- Kill switch activation
- Exchange maintenance/closure alerts

**Crash Recovery:**
- On startup: GET /portfolio/orders to fetch open orders, GET /portfolio/positions for positions
- Cancel stale orders via DELETE /portfolio/orders/{order_id}
- Reconcile bankroll via GET /portfolio/balance
- Safe mode: unwind-only until manual approval

**Client-Side Safety:**
- Rate limiter on all API calls (respect Kalshi's tiered rate limits)
- Circuit breaker on 429/5xx responses
- GET /exchange/status before any new trade
- Validate exchange is open and not in maintenance

### strategy.py — Initial Version

Create an initial strategy.py with sensible defaults. Every value here is what the autoresearch loop will tune:

```python
# strategy.py — THE ONLY FILE THE AGENT MODIFIES

# Signal weights (0.0 to 1.0)
BOOK_IMBALANCE_WEIGHT = 0.6
LMSR_PRIOR_WEIGHT = 0.2
RSS_SIGNAL_WEIGHT = 0.5
VOLUME_SPIKE_WEIGHT = 0.3

# Confidence and edge thresholds
MIN_CONFIDENCE = 0.92
MIN_EDGE_CENTS = 10
MIN_EV_MULTIPLE_OF_FEES = 3.0

# Market filters
MIN_24H_VOLUME = 10000
MIN_BOOK_DEPTH = 500
MAX_SPREAD_CENTS = 5
PREFER_BINARY = True

# Signal processing
SIGNAL_CLUSTER_WINDOW_SEC = 600
SIGNAL_COOLDOWN_SEC = 600
CONFIDENCE_DECAY_RATE = 0.01
MAX_SIGNALS_PER_CLUSTER = 1.0

# Execution
FILL_TIMEOUT_SEC = 120
MIN_FILL_PERCENT = 0.30
ORDER_CANCEL_TIMEOUT_SEC = 300

# Position management
MAX_HOLD_HOURS = 4.0
PREFER_FAST_RESOLUTION = True

# Market type weights (autoresearch tunes these)
MARKET_TYPE_WEIGHTS = {
    "policy": 1.0,
    "regulatory": 1.0,
    "court": 1.0,
    "economics": 0.8,
    "weather": 0.7,
    "crypto": 0.5,
    "sports": 0.3,
    "politics": 0.5,
    "culture": 0.5,
    "other": 0.5,
}

# RSS source weights
RSS_SOURCE_WEIGHTS = {
    "ap": 1.0,
    "reuters": 1.0,
    "government": 1.0,
    "court_filings": 0.8,
}
```

Commit: `git checkout -b kalshi-bot/v1 && git commit -am "initial scaffolding"`

### Tests

Build unit tests for every component in infra.py before proceeding:
- Order book analysis: known book -> expected mid, depth, imbalance
- LMSR: known inputs -> expected cost, price
- Bayesian: known signals -> expected posterior, verify deduplication works
- EV: known scenario -> expected EV after fees/slippage
- Risk: verify position sizes at various bankroll levels, verify tier transitions
- Fill logic: verify cancel on partial fill, dust cleanup
- Kalshi API client: verify auth, order placement, portfolio retrieval against demo environment

All tests must pass before Phase 2.

## Phase 2: Demo Trading + Simulation

**Step 1: Demo Environment**
Run the bot against Kalshi demo API (https://demo-api.kalshi.co/trade-api/v2). This is real order book behavior with fake money. Demo console at https://demo.kalshi.co/. Validate:
- Orders place and fill correctly
- Portfolio tracking matches Kalshi's reported balance
- WebSocket stream is stable
- Crash recovery works
- Discord notifications fire correctly

**Step 2: Historical Simulation**
Build simulation that replays real Kalshi data with realistic execution:

1. Pull resolved markets from Kalshi API (all types, not cherry-picked)
2. Replay order book snapshots chronologically
3. Inject historical RSS data at correct timestamps
4. Model execution realistically:
   - Queue position (you are last in line at your price)
   - Partial fills based on actual book depth
   - Spread widening during volatility
   - Realistic latency (50-200ms from signal to order)
   - Kalshi fee model applied to every trade
5. Log every decision (trade AND skip with reason) to results.tsv
6. Compute per-run metrics:
   - Net P&L (absolute and %)
   - Win rate
   - Average profit per winning trade
   - Average loss per losing trade
   - Maximum drawdown
   - Sharpe ratio
   - Trades per day
   - Largest single loss
   - Capital utilization (% of bankroll deployed on average)

**Out-of-sample split:** Train period and validation period must be separate. Never evaluate on training data.

**Gate:** Do not proceed to Phase 3 until:
- Demo trading runs stable for 48+ hours with no crashes
- Simulation shows win rate >= 90% over 300+ trades
- Positive Sharpe on out-of-sample validation data
- Max drawdown < 15%
- No single loss > 5% of bankroll

## Phase 3: Autoresearch Loop

This is where you run autonomously. Repeat until interrupted:

1. Pick ONE thing to test. Examples:
   - Confidence threshold (92% vs 90% vs 88%)
   - Minimum edge (10c vs 8c vs 12c)
   - Signal cluster window (5 min vs 10 min vs 15 min)
   - RSS source weights
   - Market type weights
   - Book depth requirements
   - LMSR prior weight (including zero)
   - Fill timeout
   - Hold time preference
   - Cooldown duration
   - Volume spike detection sensitivity
   - Confidence decay rate
   - EV multiple threshold over fees

2. Modify strategy.py with the change

3. git commit -m "experiment: [description]"

4. Run simulation: uv run btc_bot.py --simulate
   - Train period: first 70% of data
   - Validation period: last 30% of data
   - Strategy must pass on BOTH periods

5. Extract metrics from btc_results.tsv

6. KEEP if ALL of these are true:
   - Win rate >= 90% on validation data
   - Sharpe improved or held steady
   - Max drawdown < 15%
   - Trades/day increased or held steady
   - Net P&L improved

7. REVERT if any condition fails: git reset --hard HEAD~1

8. Log to btc_experiments.tsv:
   timestamp | description | win_rate_train | win_rate_val | sharpe | max_drawdown | trades_per_day | net_pnl | kept_or_reverted

9. Pick next experiment. Repeat.

### Autoresearch Priorities

The loop should prioritize experiments that increase trades/day WITHOUT decreasing win rate. Volume is the primary growth lever. The order of priority:

1. Market coverage: which market types produce profitable trades?
2. Signal sources: which RSS feeds and WebSocket signals have predictive value?
3. Confidence threshold: what's the lowest threshold that maintains 90%+ win rate?
4. Edge minimum: can we trade smaller edges profitably after fees?
5. Execution parameters: fill timeouts, cancel logic, hold time
6. Position sizing: optimal risk percentage within half-Kelly cap

### Stopping Conditions

Stop the loop and alert via Discord if:
- 10 consecutive experiments are reverted (the strategy may be at local optimum)
- Validation win rate drops below 85% (something is wrong)
- Any single simulated loss exceeds 5% of bankroll

## Phase 4: Go Live

Only after Phase 3 produces a strategy that passes all gates:

1. Ensure Kalshi account is funded (starting at $100, add more as edge proves out)
2. Switch bot from --demo to --live
3. Start at Phase 1 tier (1-2% risk per trade)
4. Monitor via Discord for first 48 hours
5. Do not touch strategy.py manually — let the autoresearch loop continue optimizing

## Constraints — Never Violate

- NEVER modify infra.py or program.md during the autoresearch loop
- NEVER skip risk checks
- NEVER hardcode prices, probabilities, thresholds, or dollar amounts
- NEVER use market orders
- NEVER exceed half-Kelly on any single position
- NEVER exceed 25% of bankroll in total open positions
- NEVER withdraw in Phase 1 or Phase 2
- NEVER promote a strategy with fewer than 300 simulated trades
- NEVER evaluate on training data — out-of-sample only
- NEVER trade during Kalshi maintenance windows
- If in doubt: DO NOT TRADE
