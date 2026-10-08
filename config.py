# ============================================================
#   BOSS SMC BOT — ULTRA-FAST TRADING CONFIG v2.0
#   SPEED + PROFIT + MANY TRADES
# ============================================================

import os

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

def _int_env(name, default):
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)

def _bool_env(name, default):
    val = os.getenv(name)
    if val is None or str(val).strip() == "":
        return bool(default)
    return str(val).strip().lower() in ("1", "true", "yes", "on")

MT5_LOGIN    = _int_env("MT5_LOGIN", 0)
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER",   "")

# ── Symbols (4 pairs scanning every minute) ──────────────────
SYMBOLS = ["XAUUSDm", "EURUSDm", "GBPUSDm", "BTCUSDm"]

TIMEFRAME = "M1"

# ── SMC Signal Settings (FAST scalping mode) ─────────────────
SWING_LEN     = 5
TP_MULT       = 1.3   # tight targets = MANY FAST trades
SL_MULT       = 1.3
ATR_LEN       = 14
EMA_LEN       = 200
COOLDOWN_BARS = 3     # LOWERED: 3 bars = faster re-entry
MIN_ATR_MULT  = 0.6   # LOWERED: allow lower volatility entries
DISP_MULT     = 0.4

MAX_ATR_PCT   = 0.0

# ── Circuit breaker: RAISED limit = more trades before pause ──
CONSEC_LOSS_LIMIT      = 6      # was 4
CONSEC_LOSS_PAUSE_MIN  = 30     # was 60 — faster resume

TP_SCALE               = 1.0
ATR_EXPANSION_BLOCK    = 0      # OFF — don't block volatility spikes

# ── Trade Settings: AGGRESSIVE ────────────────────────────────
LOT_SIZE      = 0.01
MAX_POSITIONS = 3       # 3 positions per symbol = 12 total max
MAX_SPREAD    = 0.08    # WIDENED — allows fast brokers
ENABLE_BUY    = True
ENABLE_SELL   = True

# ── Risk Management: AGGRESSIVE FOR SPEED ─────────────────────
USE_RISK_BASED_LOT      = True
RISK_PERCENT_PER_TRADE  = 1.0
SMALL_BALANCE_THRESHOLD = 50
DAILY_MAX_LOSS_PERCENT  = 25.0   # RAISED — keep trading longer
MAX_DRAWDOWN_PERCENT    = 40.0   # RAISED — more room to trade
MIN_FREE_MARGIN_BUFFER  = 1.05   # LOWERED — less margin required

WITHDRAW_MILESTONE_ENABLED = True
PROTECTED_BASE_CAPITAL     = 10
WITHDRAW_MILESTONE_STEP    = 100

# ── Trailing: AGGRESSIVE lock profits FAST ────────────────────
PROGRESSIVE_TRAIL       = True
BREAK_EVEN_PROGRESS     = 0.30   # break-even at 30% = LOCK profit FAST
TRAIL_STAGE_1_PROGRESS  = 0.30
TRAIL_STAGE_2_PROGRESS  = 0.50   # LOWERED — trail sooner
TRAIL_STAGE_3_PROGRESS  = 0.70   # LOWERED — trail sooner
TRAIL_STAGE_1_ATR_MULT  = 2.0    # tighter trail = lock more profit
TRAIL_STAGE_2_ATR_MULT  = 1.2
TRAIL_STAGE_3_ATR_MULT  = 0.6

SL_BUFFER_ENABLED       = False

# ── Loop Speed: FASTEST POSSIBLE ──────────────────────────────
ANALYSIS_INTERVAL_SECONDS  = 0.5   # FASTER than 1.0 — scan twice per second
MIN_ORDER_INTERVAL_SECONDS = 0.3   # FASTER order placement

DATA_ROOT_PATH = "SMC_Bot_Data"

# ── ML Settings: PERMISSIVE = MORE TRADES ─────────────────────
ML_ENABLED              = True
FORCE_INITIAL_TRAINING  = False    # SKIP startup backtest = faster start
INITIAL_TRAIN_DAYS      = 30       # REDUCED — faster startup
ML_MIN_TRADES           = 20       # LOWERED — start trusting ML sooner
ML_MIN_CONFIDENCE       = 0.35     # VERY LOW — ML rarely blocks
ML_RETRAIN_EVERY        = 15
MAX_TRAINING_TRADES     = 500
RETRAIN_INTERVAL_HOURS  = 2
RETRAIN_MIN_NEW_TRADES  = 10

MODEL_PATH     = f"{DATA_ROOT_PATH}/models/smc_model.pkl"
SCALER_PATH    = f"{DATA_ROOT_PATH}/models/smc_scaler.pkl"
TRADE_LOG_PATH = f"{DATA_ROOT_PATH}/data/trade_log.csv"

# ── Pattern Learning: AGGRESSIVE whitelist boost ──────────────
PATTERN_CLUSTERING_ENABLED    = True
BLACKLIST_ENABLED             = False  # OFF — don't block any patterns
PATTERN_UPDATE_INTERVAL_HOURS = 12
WHITELIST_CONFIDENCE_BOOST    = 0.25   # RAISED — boost winners more
BLACKLIST_BLOCK_CONFIDENCE    = 0.30
PATTERN_DATA_PATH             = f"{DATA_ROOT_PATH}/data/patterns.json"

TELEGRAM_ENABLED = True
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Session: 24/7 NON-STOP TRADING ────────────────────────────
SESSION_FILTER = False  # OFF — trade every minute 24/7
SESSION_START  = 0
SESSION_END    = 24

# ── Engine Filters: MINIMAL (speed over safety) ───────────────
H1_TREND_FILTER  = True    # KEEP ON — this actually helps
H1_EMA_LEN       = 50
MTF_PRIORITY_M15 = False   # OFF — trade M5 AND M15 (more signals)

# ── RSI Filter: OFF (don't block signals) ─────────────────────
RSI_FILTER_ENABLED          = False
RSI_BULL_MIN                = 40
RSI_BEAR_MAX                = 60
RSI_CHOP_PATTERN_OVERRIDE   = 1

# ── Entry Engine: SMC_MTF (FASTEST) ───────────────────────────
ENTRY_ENGINE     = "SMC_MTF"

# ── FLASH engine (not used but kept) ──────────────────────────
FLASH_SL_MULT       = 0.0
FLASH_PROFIT_LOCK   = True
FLASH_LOCK_TRIGGER  = 0.4
FLASH_LOCK_PCT      = 0.6
FLASH_HEDGE         = True
FLASH_HEDGE_TRIGGER = 0.5
FLASH_TIME_STOP     = 0
FLASH_DAILY_STOP_R  = 5.0
FLASH_ML_GATE       = False

# ── SR_H1 engine (not used) ───────────────────────────────────
SR_SWING_LEN       = 5
SR_ATR_LEN         = 14
SR_H4_EMA_LEN      = 50
SR_LEVEL_ATR_RANGE = 1.0
SR_CLUSTER_ATR     = 0.25
SR_MOMENTUM_BODY   = 0.5
SR_CLOSE_POS       = 0.6
SR_SL_ATR          = 0.25
SR_RR              = 2.0
SR_MAX_SL_ATR      = 2.5
SR_MIN_SL_ATR      = 0.15
SR_LOOKBACK_DAYS   = 10
SR_NEWS_START      = 12.5
SR_NEWS_END        = 14.5

# ── AI Entry Review: OFF (SPEED MODE — no AI blocking) ────────
AI_ENABLED               = False
AI_PROVIDER              = os.getenv("AI_PROVIDER", "disabled")
AI_BASE_URL              = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
_ai_keys_raw = os.getenv("AI_API_KEYS", "") or os.getenv("AI_API_KEY", "")
AI_API_KEYS              = [k.strip() for k in _ai_keys_raw.split(",") if k.strip()]
AI_API_KEY               = AI_API_KEYS[0] if AI_API_KEYS else ""
AI_MODEL                 = os.getenv("AI_MODEL", "gpt-4o-mini")
AI_RESPONSE_FORMAT       = os.getenv("AI_RESPONSE_FORMAT", "json_object")
AI_TIMEOUT_SECONDS       = 8.0
AI_MAX_RETRIES           = 1
AI_CIRCUIT_FAILURES      = 5
AI_CIRCUIT_COOLDOWN_SECONDS = 120.0
AI_MIN_CONFIDENCE        = 0.60
AI_MAX_OUTPUT_TOKENS     = 300
AI_JOURNAL_PATH          = f"{DATA_ROOT_PATH}/data/ai_decisions.csv"
AI_SCHEMA_VERSION        = "entry_review_v1"
AI_FEATURE_VERSION       = "sr_h1_v1"

# ── AI Position Manager: ON — watches open trades every 5 min ─
AI_POSITION_ENABLED       = True   # ON: CLOSE losers early, EXTEND winners
AI_POSITION_INTERVAL      = 300.0  # review every 5 min (was 600)
AI_POSITION_MIN_CONFIDENCE = 0.60
AI_EXTEND_MIN_PROGRESS    = 0.85   # extend sooner
AI_EXTEND_TP_MULT         = 1.15   # smaller extension
AI_EXTEND_LOCK_PCT        = 0.80   # lock more profit
AI_POSITION_SCHEMA_VERSION = "position_review_v1"

AI_POSITION_VOTES         = 2      # 2 AI brains must agree before CLOSE or EXTEND
AI_POSITION_MODEL_2       = os.getenv("AI_POSITION_MODEL_2", "")
AI_POSITION_MAX_TOKENS    = 500

# ── News: ON (context only, no blocking) ──────────────────────
NEWS_ENABLED              = _bool_env("NEWS_ENABLED", True)
NEWS_RSS_URL              = os.getenv("NEWS_RSS_URL", "https://www.fxstreet.com/rss/news")
NEWS_POLL_SECONDS         = 600.0
NEWS_MAX_AGE_HOURS        = 2
NEWS_SEEN_PATH            = f"{DATA_ROOT_PATH}/data/news_seen.json"
NEWS_ALERT_KEYWORDS = ["fed", "fomc", "powell", "cpi", "nfp", "ecb", "boe"]
NEWS_MAX_ALERTS_PER_HOUR  = 10

# ── AI Regime: OFF PERMANENTLY (no self-tuning) ───────────────
AI_REGIME_ENABLED         = False
AI_REGIME_INTERVAL        = 14400.0
AI_REGIME_MODEL           = ""
AI_REGIME_SCHEMA_VERSION  = "market_adaptation_v1"
AI_REGIME_ALLOWED = {
    "RISK_PERCENT_PER_TRADE":   (1.0,   1.0),
    "AI_MIN_CONFIDENCE":        (0.60,  0.60),
    "MAX_POSITIONS":            (3,     3),
    "MAX_SPREAD":               (0.08,  0.08),
    "TP_SCALE":                 (1.0,   1.2),
}

# ── AI Appeal: OFF (SPEED) ────────────────────────────────────
AI_ENTRY_APPEAL               = False
AI_ENTRY_APPEAL_MIN_CONFIDENCE = 0.75

LOG_PATH = "logs/bot.log"
CRASH_LOG_PATH = "logs/crash.log"
