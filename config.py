# config.py — All tunable constants for the weather mispricing bot.

# ─── Risk ────────────────────────────────────────────────────────────────────
INITIAL_BANKROLL = 340.00
DAILY_MAX_LOSS_PCT = 0.20
MAX_RISK_PER_TRADE_PCT = 0.02
MAX_TOTAL_OPEN_RISK_PCT = 0.10
MAX_TRADES_PER_DAY = 6
MAX_CONSECUTIVE_LOSSES = 3
LOSS_STREAK_COOLDOWN_MINUTES = 120

# ─── Signal thresholds ───────────────────────────────────────────────────────
MIN_EDGE = 0.12
MIN_CONFIDENCE = 0.70
MAX_SPREAD = 0.06

# ─── Model ───────────────────────────────────────────────────────────────────
GAIN_RATE_F_PER_HOUR = 1.5
PEAK_HOUR_LOCAL = 15.5          # 3:30 PM local
TREND_ADJUST_F = 1.5            # +/- degrees for trend deviation
SIGMA_EARLY_F = 3.5             # >5 hours to peak
SIGMA_MID_F = 2.8               # 3-5 hours to peak
SIGMA_LATE_F = 2.0              # <3 hours to peak
MODEL_VERSION = "intraday_metar_v1"

# ─── Trading window (local time) ─────────────────────────────────────────────
TRADE_WINDOW_START_HOUR = 10.5  # 10:30 AM
TRADE_WINDOW_END_HOUR = 15.5    # 3:30 PM
MIN_HOURS_TO_PEAK = 1.0         # skip if <1hr to peak

# ─── Data freshness ─────────────────────────────────────────────────────────
MAX_METAR_AGE_MINUTES = 60
METAR_HOURS_BACK = 6            # hours of METAR history to fetch

# ─── Target ──────────────────────────────────────────────────────────────────
TARGET_CITY = "Denver"
TARGET_STATION = "KDEN"
CITY_TIMEZONE = "America/Denver"

# ─── Execution ───────────────────────────────────────────────────────────────
SLIPPAGE_CENTS = 2              # conservative fill penalty
SCAN_INTERVAL_SEC = 60

# ─── BTC 15-min bot ─────────────────────────────────────────────────────────
BTC_SCAN_INTERVAL_SEC = 30      # check every 30s (market is 15 min)
BTC_MIN_EDGE = 0.12             # require 12c model edge before any BTC trade
BTC_MIN_CONFIDENCE = 0.70       # only trade once vol estimate has enough samples
BTC_MAX_SPREAD = 0.06           # 6 cent max Kalshi spread
BTC_MAX_RISK_PER_TRADE = 3.00   # $3 max per trade (small bets, many trades)
BTC_MAX_DAILY_TRADES = 50       # cap at 50 trades/day
BTC_MAX_DAILY_LOSS = 25.00      # stop after $25 loss
BTC_VOL_WINDOW_MINUTES = 60     # rolling 1hr of 1-min candles for vol estimate
BTC_MIN_TIME_REMAINING_SEC = 120  # don't trade with < 2 min left
BTC_SERIES_TICKER = "KXBTC15M"  # BTC price up/down rolling 15-minute series
BTC_PAPER_WARMUP_HOURS = 48     # hard gate before live order execution
BTC_MAX_TARGET_DISTANCE = 0.005 # skip BTC 15m if spot is >50 bps from target
