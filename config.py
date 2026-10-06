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

def _bool_env(name, default):
    """Read a bool from env; anything not truthy (incl. blank) => default."""
    val = os.getenv(name)
    if val is None or str(val).strip() == "":
        return bool(default)
    return str(val).strip().lower() in ("1", "true", "yes", "on")

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

# ── ENTRY ENGINE SELECTOR (v1.9.1) ───────────────────────────
# "SR_H1"   = ENGINE C — the DUEL WINNER: daily/weekly S/R breakout on H1
#             + momentum + H4 trend + news filter (see core/sr_engine.py).
#             +0.015 R/trade, PF 1.15, max DD 4.0R on 2024-2025 real data.
# "SMC_MTF" = your original SMC sweep/BOS engine (v1.9 with H1 gate).
# "FLASH"   = the ORIGINAL MTF high-freq flow (no session filter, no H1 gate)
#             + 3 upgrades: tight-SL option, 50%-profit SL lock, opposite-side
#             hedge flip (flatten pair when either leg hits). See
#             core/flash_engine.py. 2024-2025 backtest (cost 0.30):
#             original PF 0.87, +lock 0.85, +hedge 0.77, full 0.67 — all
#             below 1.0 after costs, so this engine is OPT-IN, OFF by default.
ENTRY_ENGINE     = "SR_H1"

# ── FLASH engine parameters (ENTRY_ENGINE="FLASH" only) ──────────
# Backtest of record: /home/user/backtest_lab/flash_mtf.py (730 days, cost 0.30,
# SL-first, next-bar-open entries). R = initial stop distance of the pair.
FLASH_SL_MULT       = 0.0   # U1 lose-small: 0.0 = per-TF default (1.5/1.8/2.0);
                            #     1.0 = tighter initial stop (backtest: worse,
                            #     PF 0.83 vs 0.87 — kept configurable anyway)
FLASH_PROFIT_LOCK   = True  # U2: lock a share of profit once it runs
FLASH_LOCK_TRIGGER  = 0.5   # trigger when profit >= 50% of the distance to TP
FLASH_LOCK_PCT      = 0.5   # SL -> entry + 50% of current profit (ratchet)
FLASH_HEDGE         = True  # U3: open opposite side when main leg goes wrong
FLASH_HEDGE_TRIGGER = 0.5   # hedge when loss >= 50% of the main leg's risk
FLASH_TIME_STOP     = 0     # U4: close the pair after N minutes (0 = off)
FLASH_DAILY_STOP_R  = 3.0   # SAFETY: no new entries/hedges for the UTC day
                            #     after -3R realized (backtest: caps worst day
                            #     at ~-7R vs ~-70R uncapped)
FLASH_ML_GATE       = False # backtest parity (ML model AUC 0.512 ≈ coin flip)

# ── SR_H1 engine parameters (the tested build — don't change to retest) ──
SR_SWING_LEN       = 5      # pivot length for swing levels
SR_ATR_LEN         = 14
SR_H4_EMA_LEN      = 50     # H4 trend filter
SR_LEVEL_ATR_RANGE = 1.0    # only levels within 1*ATR of price matter
SR_CLUSTER_ATR     = 0.25   # cluster levels closer than 0.25*ATR
SR_MOMENTUM_BODY   = 0.5    # breakout bar body must be >= 0.5*ATR
SR_CLOSE_POS       = 0.6    # close in upper 40% of bar (false-breakout filter)
SR_SL_ATR          = 0.25   # SL = level ∓ 0.25*ATR
SR_RR              = 2.0    # take profit at 2R
SR_MAX_SL_ATR      = 2.2    # skip if SL distance > 2.2*ATR (too wide)
SR_MIN_SL_ATR      = 0.25   # skip if SL distance < 0.25*ATR (too tight)
SR_LOOKBACK_DAYS   = 10     # swing levels older than 10 days are ignored
SR_NEWS_START      = 12.5   # skip US news window (UTC hours)
SR_NEWS_END        = 14.5

# ── AI Entry Review (feature-flagged — OFF by default) ─────────
# An OPTIONAL AI risk-reviewer that sits in FRONT of the existing
# deterministic risk checks (see execute_trade in main.py). It reviews a
# candidate signal produced by the strategy and may APPROVE / REJECT / WAIT.
#
#   • The AI NEVER places or cancels orders, sizes positions, moves/removes
#     stops, or bypasses any risk gate. A valid APPROVE only lets the
#     candidate CONTINUE to the existing risk/order path below.
#   • REJECT / WAIT and ANY AI failure (timeout, invalid JSON/schema, missing
#     key, rate limit, provider error, open circuit) BLOCK the candidate —
#     fail-closed. There is deliberately NO rule-based auto-approval fallback.
#   • When AI_ENABLED=False the bot behaves EXACTLY as before and makes NO
#     provider call and writes NO journal row.
#
# Only the minimum sanitized decision-time context is sent (no secrets, no
# credentials, no account identifiers, no future bars). See core/ai_review.py.
#
# Enabling AI makes AI APPROVAL REQUIRED: if the provider/key is missing or
# failing (timeout, invalid reply, rate limit, open circuit) new entries are
# BLOCKED — fail-closed, never silently approved. Keep AI_ENABLED=false to
# trade exactly as before with no provider call.
AI_ENABLED               = _bool_env("AI_ENABLED", False)
AI_PROVIDER              = os.getenv("AI_PROVIDER", "disabled")   # "openai_compat" | "disabled"
# Any OpenAI-compatible chat-completions endpoint (OpenAI, Groq, Together,
# a local vLLM/Ollama /v1 endpoint, ...). Never hardcode the key.
AI_BASE_URL              = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
AI_API_KEY               = os.getenv("AI_API_KEY", "")
AI_MODEL                 = os.getenv("AI_MODEL", "gpt-4o-mini")
AI_RESPONSE_FORMAT       = os.getenv("AI_RESPONSE_FORMAT", "json_object")  # or "json_schema"
AI_TIMEOUT_SECONDS       = 8.0     # hard per-request timeout (bounded; never hangs the loop)
AI_MAX_RETRIES           = 1       # bounded retries on availability errors (total attempts = 1 + this)
AI_CIRCUIT_FAILURES      = 5       # open the circuit after this many consecutive availability failures
AI_CIRCUIT_COOLDOWN_SECONDS = 120.0
AI_MIN_CONFIDENCE        = 0.60    # an APPROVE below this confidence is treated as WAIT (fail-closed)
AI_MAX_OUTPUT_TOKENS     = 300     # bound the reply size
AI_JOURNAL_PATH          = f"{DATA_ROOT_PATH}/data/ai_decisions.csv"  # additive; same store as trade_log
# Versions stamped into every prompt and journal row (bump when either changes).
AI_SCHEMA_VERSION        = "entry_review_v1"
AI_FEATURE_VERSION       = "sr_h1_v1"

# ── Logging ───────────────────────────────────────────────────
LOG_PATH = "logs/bot.log"
CRASH_LOG_PATH = "logs/crash.log"
