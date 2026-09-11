# ============================================================
#   BOSS SMC BOT — CONFIG  v1.7.2
# ============================================================

import os

# Load secrets from a local .env file if present (never commit .env).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass  # dotenv optional; falls back to defaults / real env vars below

# ── MT5 Login ────────────────────────────────────────────────
# FIX (security): real account credentials MUST NOT live in source code —
# the old defaults (account 415…000 + its password) were committed to git.
# Fill them in .env instead (see .env.example).
# ⚠️ The old password and Telegram token were exposed — CHANGE the MT5
# password and rotate the Telegram bot token before running again.
def _int_env(name, default):
    """Read an int from env, tolerating blanks/placeholders (e.g. a fresh .env)."""
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)

MT5_LOGIN    = _int_env("MT5_LOGIN", 0)
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER",   "")

# ── Symbols ──────────────────────────────────────────────────
SYMBOLS = [
    "XAUUSD",    # Gold on MetaQuotes-Demo (Exness uses "XAUUSDm" with an 'm')
    # "EURUSD",  # Euro
    # "BTCUSD",  # Bitcoin
    # "XAGUSD",  # Silver
]

TIMEFRAME = "M1"

# ── SMC Signal Settings ───────────────────────────────────────
SWING_LEN     = 5
TP_MULT       = 3.0
SL_MULT       = 1.5
ATR_LEN       = 14
EMA_LEN       = 200
COOLDOWN_BARS = 5
MIN_ATR_MULT  = 0.8
DISP_MULT     = 0.5

# ── Volatility-regime guard (defensive, OFF by default) ───────
# Skip NEW entries when volatility is abnormally high (news spikes,
# where SMC signals fail most). 0 = disabled. Try 0.30 for gold to test.
MAX_ATR_PCT   = 0.0

# ── Trade Settings ────────────────────────────────────────────
LOT_SIZE      = 0.01   # fallback lot if risk-based sizing is off / unavailable
MAX_POSITIONS = 3
MAX_SPREAD    = 0.05
ENABLE_BUY    = True
ENABLE_SELL   = True

# ── Risk Management (small-account protection) ────────────────
USE_RISK_BASED_LOT      = True    # size each trade from balance instead of fixed LOT_SIZE
RISK_PERCENT_PER_TRADE  = 1.0     # % of balance risked per trade (loss at SL)
SMALL_BALANCE_THRESHOLD = 50      # below this balance → max 1 position at a time
# NOTE: on $50 GOLD, min lot 0.01 risks ~9% per trade — these limits are raised
# so the bot can actually trade for demo testing. This is HIGH RISK; do NOT use
# these wide limits with real money. See EURUSD / bigger-balance for safe sizing.
DAILY_MAX_LOSS_PERCENT  = 20.0    # halt NEW trades for the day after this daily loss
MAX_DRAWDOWN_PERCENT    = 30.0    # halt NEW trades if equity falls this % from peak
MIN_FREE_MARGIN_BUFFER  = 1.1     # require this × the needed margin to be free

# ── Capital-Preservation Withdrawal Ladder ────────────────────
# The bot can't withdraw for you, but it alerts you when to bank profit.
# Example: base 10, step 100 → at +$100 profit it says "take out $100, keep $10".
WITHDRAW_MILESTONE_ENABLED = True
PROTECTED_BASE_CAPITAL     = 10     # your untouchable starting stake (USD)
WITHDRAW_MILESTONE_STEP    = 100    # alert to bank profit every +this much

# ── Progressive Trailing Stop ──────────────────────────────────
PROGRESSIVE_TRAIL       = True
BREAK_EVEN_PROGRESS     = 0.60   # Move to BE at 60% progress
TRAIL_STAGE_1_PROGRESS  = 0.50
TRAIL_STAGE_2_PROGRESS  = 0.70
TRAIL_STAGE_3_PROGRESS  = 0.90
TRAIL_STAGE_1_ATR_MULT  = 2.5
TRAIL_STAGE_2_ATR_MULT  = 1.5
TRAIL_STAGE_3_ATR_MULT  = 0.8

# Anti-stop-hunt SL widening. OFF by default: widening the SL enlarges the
# loss beyond the % you risked, which breaks risk-based sizing. Keep it off
# for small accounts / defensive trading.
SL_BUFFER_ENABLED       = False

# ── High-Frequency Loop & Safety ──────────────────────────────
ANALYSIS_INTERVAL_SECONDS  = 1.0   # Target loop speed
MIN_ORDER_INTERVAL_SECONDS = 0.5   # MT5 rate limit (seconds between modifications)

# ── Persistent Learning ───────────────────────────────────────
# Absolute path outside bot folder for data portability
DATA_ROOT_PATH = "SMC_Bot_Data" 

# ── ML Model Settings ─────────────────────────────────────────
ML_ENABLED              = True
FORCE_INITIAL_TRAINING  = True    # If True, always runs a backtest on startup to seed ML
INITIAL_TRAIN_DAYS      = 45      # how much history to learn from on startup (more = smarter)
ML_MIN_TRADES           = 30      # don't trust the model until it has real data
ML_MIN_CONFIDENCE       = 0.72
ML_RETRAIN_EVERY        = 10      # Retrain every N live trades
MAX_TRAINING_TRADES     = 500     # Rolling window size
RETRAIN_INTERVAL_HOURS  = 1       # Maximum retrain frequency
RETRAIN_MIN_NEW_TRADES  = 10      # Only retrain if at least this many new trades

MODEL_PATH     = f"{DATA_ROOT_PATH}/models/smc_model.pkl"
SCALER_PATH    = f"{DATA_ROOT_PATH}/models/smc_scaler.pkl"
TRADE_LOG_PATH = f"{DATA_ROOT_PATH}/data/trade_log.csv"

# ── Pattern Blacklist & Whitelist ─────────────────────────────
PATTERN_CLUSTERING_ENABLED    = True
BLACKLIST_ENABLED             = True   # Emergency off switch for blacklist
PATTERN_UPDATE_INTERVAL_HOURS = 24
WHITELIST_CONFIDENCE_BOOST    = 0.20
BLACKLIST_BLOCK_CONFIDENCE    = 0.40
PATTERN_DATA_PATH             = f"{DATA_ROOT_PATH}/data/patterns.json"

# ── Telegram Alerts ───────────────────────────────────────────
# FIX (security): the hardcoded token was committed to git — it is revoked
# material. Put a FRESH token in .env (BotFather → /revoke to rotate).
TELEGRAM_ENABLED = True
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Session Filter ────────────────────────────────────────────
# Engine duel (v1.9): Asia/low-liquidity hours hurt M1 signals.
# London+NY window (UTC) — the window the best gold bots trade in.
SESSION_FILTER = True
SESSION_START  = 7
SESSION_END    = 20

# ── Engine v1.9 architecture filters (from engine-vs-engine duel) ──
# Block new entries when they go AGAINST the H1 EMA trend.
# The duel showed counter-trend M1 entries are the main bleed.
H1_TREND_FILTER  = True
H1_EMA_LEN       = 50
# When an M15 signal exists, trade ONLY the M15 signal (ignore M1/M5 noise).
# Wider ATR → SL distance grows → spread becomes a smaller % of risk.
MTF_PRIORITY_M15 = True

# ── Logging ───────────────────────────────────────────────────
LOG_PATH = "logs/bot.log"
CRASH_LOG_PATH = "logs/crash.log"
