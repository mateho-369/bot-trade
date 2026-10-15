# ============================================================
#   BOSS SMC BOT — AI + BOT FLOW CONFIG  v2.1
#
#   v2.1 changes (AI ⇄ Bot flow repair, see docs/FLOW_AI_BOT.md):
#     • Profiles: BOT_PROFILE="balanced" (default, edge-validated) or
#       "ultra_fast" (the old v2.0 speed settings — same numbers as before).
#     • AI flags are now HONEST and independent:
#         AI_ENABLED          → AI entry gate (veto before ordering)
#         AI_POSITION_ENABLED → AI position manager (HOLD/CLOSE/EXTEND)
#         AI_REGIME_ENABLED   → AI market adaptation (bot directives)
#       All three require plumbing (AI_PROVIDER + AI_API_KEYS). A flag that
#       cannot run is reported at startup instead of dying silently.
#     • AI calls never block the hot loop (bounded worker thread, ai_bus).
#     • The AI can advise the bot inside hard clamps (AI_REGIME_ALLOWED).
#
#   Secrets come ONLY from .env — never commit real credentials.
# ============================================================

import os

# ── Where secrets are read from (first file that exists wins) ───────────────
#   1. $BOT_ENV_FILE                 explicit path, anywhere on disk
#   2. ~/.boss-bot/.env              OUTSIDE the repo: `git add .` can never
#                                    pick it up, and deleting/re-cloning the
#                                    repo cannot touch it. RECOMMENDED.
#   3. <repo>/.env                   legacy location (git-ignored)
# Real environment variables always beat any file (override=False), so a
# server / CI can inject secrets without writing a file at all.
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_CANDIDATES = [
    os.getenv("BOT_ENV_FILE", "").strip(),
    os.path.join(os.path.expanduser("~"), ".boss-bot", ".env"),
    os.path.join(_REPO_DIR, ".env"),
]
ENV_FILE_USED = next((p for p in _ENV_CANDIDATES if p and os.path.isfile(p)), "")

try:
    from dotenv import load_dotenv
    if ENV_FILE_USED:
        load_dotenv(ENV_FILE_USED, override=False)
except Exception:
    pass


def _int_env(name, default):
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)


def _float_env(name, default):
    try:
        return float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return float(default)


def _bool_env(name, default):
    val = os.getenv(name)
    if val is None or str(val).strip() == "":
        return bool(default)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _str_env(name, default):
    val = os.getenv(name)
    return default if val is None or str(val).strip() == "" else str(val).strip()


MT5_LOGIN    = _int_env("MT5_LOGIN", 0)
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER",   "")

# ── Symbols (4 pairs scanned every loop) ──────────────────────
SYMBOLS = [s.strip() for s in
           _str_env("SYMBOLS", "XAUUSDm,EURUSDm,GBPUSDm,BTCUSDm").split(",")
           if s.strip()]

TIMEFRAME = "M1"

# ════════════════════════════════════════════════════════════
#   PROFILE — one switch, two coherent settings sets
# ════════════════════════════════════════════════════════════
# "balanced"   : the settings this repo's OWN research validated
#                (REAL_WORLD_TEST.md: M1/M5 MTF scalping = PF 0.87 = loses
#                after spread; SR_H1 = duel winner, PF 1.15). Cost-aware:
#                tight risk, volatility guard, blacklist on.
# "ultra_fast" : the v2.0 speed profile (M1 engine, permissive filters,
#                0.5s loop). Kept byte-for-byte so nothing is lost — but it
#                is documented as a LOSING edge profile, use on demo only.
# "scalper"    : ultra_fast's trading settings PLUS an AI lane tuned for speed:
#                short AI timeouts, a hard per-entry latency budget (a slow or
#                dead AI can no longer cost you the entry — the bot proceeds on
#                its own signal), 1-vote position reviews every 30s, 0.3s loop.
#                The AI still vetoes when it answers in time; it just never
#                holds the trade hostage. Demo first: it is built to trade a lot.
BOT_PROFILE = _str_env("BOT_PROFILE", "balanced").lower()
if BOT_PROFILE not in ("balanced", "ultra_fast", "scalper"):
    BOT_PROFILE = "balanced"

SCALPER    = (BOT_PROFILE == "scalper")
# every existing `ULTRA_FAST` switch below also applies to the scalper profile
ULTRA_FAST = BOT_PROFILE in ("ultra_fast", "scalper")

# ── SMC Signal Settings ──────────────────────────────────────
SWING_LEN     = 5
TP_MULT       = 1.3 if ULTRA_FAST else 2.0
SL_MULT       = 1.3 if ULTRA_FAST else 1.6
ATR_LEN       = 14
EMA_LEN       = 200
COOLDOWN_BARS = 2 if SCALPER else (3 if ULTRA_FAST else 5)
MIN_ATR_MULT  = 0.6 if ULTRA_FAST else 0.8
DISP_MULT     = 0.4

# Cost guard: block an entry when the spread eats more than this % of the
# stop distance. The AI is asked for the same judgement — now the bot enforces
# it deterministically too, so a missing/slow AI never pays 40% of R in spread.
MAX_ATR_PCT = 0.0 if ULTRA_FAST else 0.9
MAX_SPREAD_COST_PCT = _float_env("MAX_SPREAD_COST_PCT", 0.0 if ULTRA_FAST else 18.0)

# ── Circuit breaker ─────────────────────────────────────────
CONSEC_LOSS_LIMIT     = 6 if ULTRA_FAST else 3
CONSEC_LOSS_PAUSE_MIN = 30 if ULTRA_FAST else 90

TP_SCALE            = 1.0
ATR_EXPANSION_BLOCK = 0.0 if ULTRA_FAST else 1.9   # x the 50-bar ATR average

# ── Trade Settings ──────────────────────────────────────────
LOT_SIZE      = _float_env("LOT_SIZE", 0.01)
MAX_POSITIONS = _int_env("MAX_POSITIONS", 3 if ULTRA_FAST else 2)
MAX_SPREAD    = _float_env("MAX_SPREAD", 0.08 if ULTRA_FAST else 0.05)   # % of price
ENABLE_BUY    = _bool_env("ENABLE_BUY", True)
ENABLE_SELL   = _bool_env("ENABLE_SELL", True)

# ── Risk Management ─────────────────────────────────────────
USE_RISK_BASED_LOT     = True
RISK_PERCENT_PER_TRADE = _float_env("RISK_PERCENT_PER_TRADE",
                             1.0 if ULTRA_FAST else 0.5)
SMALL_BALANCE_THRESHOLD = 50
DAILY_MAX_LOSS_PERCENT = 25.0 if ULTRA_FAST else 6.0
MAX_DRAWDOWN_PERCENT   = 40.0 if ULTRA_FAST else 15.0
MIN_FREE_MARGIN_BUFFER = 1.05 if ULTRA_FAST else 1.5

WITHDRAW_MILESTONE_ENABLED = True
PROTECTED_BASE_CAPITAL     = _float_env("PROTECTED_BASE_CAPITAL", 10)
WITHDRAW_MILESTONE_STEP    = _float_env("WITHDRAW_MILESTONE_STEP", 100)

# ── Trailing: lock profit, then let the AI extend runners ───
PROGRESSIVE_TRAIL      = True
BREAK_EVEN_PROGRESS    = _float_env("BREAK_EVEN_PROGRESS",
                           0.30 if ULTRA_FAST else 0.45)
TRAIL_STAGE_1_PROGRESS = 0.30 if ULTRA_FAST else 0.45
TRAIL_STAGE_2_PROGRESS = 0.50 if ULTRA_FAST else 0.60
TRAIL_STAGE_3_PROGRESS = 0.70 if ULTRA_FAST else 0.80
TRAIL_STAGE_1_ATR_MULT = 2.0
TRAIL_STAGE_2_ATR_MULT = 1.2
TRAIL_STAGE_3_ATR_MULT = 0.6

SL_BUFFER_ENABLED = False      # widening a stop is the one thing AI must not

# ── Loop Speed ──────────────────────────────────────────────
ANALYSIS_INTERVAL_SECONDS  = 0.3 if SCALPER else (0.5 if ULTRA_FAST else 1.0)
MIN_ORDER_INTERVAL_SECONDS = 0.3

DATA_ROOT_PATH = "SMC_Bot_Data"

# ── ML Settings ─────────────────────────────────────────────
ML_ENABLED              = True
FORCE_INITIAL_TRAINING  = False
INITIAL_TRAIN_DAYS      = 30 if ULTRA_FAST else 60
ML_MIN_TRADES           = 20 if ULTRA_FAST else 60
ML_MIN_CONFIDENCE       = 0.35 if ULTRA_FAST else 0.45
ML_RETRAIN_EVERY        = 15 if ULTRA_FAST else 40
MAX_TRAINING_TRADES     = 500
RETRAIN_INTERVAL_HOURS  = 2
RETRAIN_MIN_NEW_TRADES  = 10

MODEL_PATH     = f"{DATA_ROOT_PATH}/models/smc_model.pkl"
SCALER_PATH    = f"{DATA_ROOT_PATH}/models/smc_scaler.pkl"
TRADE_LOG_PATH = f"{DATA_ROOT_PATH}/data/trade_log.csv"

# ── Pattern Learning ────────────────────────────────────────
PATTERN_CLUSTERING_ENABLED    = True
BLACKLIST_ENABLED             = not ULTRA_FAST   # stop-hunt clusters block
PATTERN_UPDATE_INTERVAL_HOURS = 12
WHITELIST_CONFIDENCE_BOOST    = 0.25 if ULTRA_FAST else 0.15
BLACKLIST_BLOCK_CONFIDENCE    = 0.30
PATTERN_DATA_PATH             = f"{DATA_ROOT_PATH}/data/patterns.json"

TELEGRAM_ENABLED = _bool_env("TELEGRAM_ENABLED", True)
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Session ─────────────────────────────────────────────────
SESSION_FILTER = ULTRA_FAST is False     # trade the liquid hours only
SESSION_START  = 7                        # UTC
SESSION_END    = 20                       # UTC

# ── Engine filters ──────────────────────────────────────────
H1_TREND_FILTER  = True
H1_EMA_LEN       = 50
MTF_PRIORITY_M15 = ULTRA_FAST is False    # prefer the higher-TF signal

RSI_FILTER_ENABLED        = not ULTRA_FAST
RSI_BULL_MIN              = 40 if ULTRA_FAST else 52
RSI_BEAR_MAX              = 60 if ULTRA_FAST else 48
RSI_CHOP_PATTERN_OVERRIDE = 1 if ULTRA_FAST else 2

# ── Entry Engine ────────────────────────────────────────────
# SR_H1  = duel winner (H1 S/R breakout, PF 1.15 on 2024-25 real data)
# SMC_MTF= fast M5/M15 scalp (PF 0.87 — demo only)
# FLASH  = MTF + profit lock + hedge flip (PF 0.67-0.85 — demo only)
ENTRY_ENGINE = _str_env("ENTRY_ENGINE",
                        "SMC_MTF" if ULTRA_FAST else "SR_H1")

# ── FLASH engine ────────────────────────────────────────────
FLASH_SL_MULT       = 0.0
FLASH_PROFIT_LOCK   = True
FLASH_LOCK_TRIGGER  = 0.4
FLASH_LOCK_PCT      = 0.6
FLASH_HEDGE         = True
FLASH_HEDGE_TRIGGER = 0.5
FLASH_TIME_STOP     = 0
FLASH_DAILY_STOP_R  = 5.0 if ULTRA_FAST else 3.0
FLASH_ML_GATE       = False

# ── SR_H1 engine ────────────────────────────────────────────
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

# ════════════════════════════════════════════════════════════
#   AI LAYER  (3 independent roles + 1 shared plumbing)
# ════════════════════════════════════════════════════════════
# plumbing — the only thing all three roles need
AI_PROVIDER        = _str_env("AI_PROVIDER", "disabled")   # openai_compat | disabled
AI_BASE_URL        = _str_env("AI_BASE_URL", "https://api.openai.com/v1")
_ai_keys_raw = os.getenv("AI_API_KEYS", "") or os.getenv("AI_API_KEY", "")
AI_API_KEYS        = [k.strip() for k in _ai_keys_raw.split(",") if k.strip()]
AI_API_KEY         = AI_API_KEYS[0] if AI_API_KEYS else ""
AI_MODEL           = _str_env("AI_MODEL", "gpt-4o-mini")
AI_RESPONSE_FORMAT = _str_env("AI_RESPONSE_FORMAT", "json_object")

# role 1 — entry gate (veto). Fail-CLOSED: no verdict = no trade.
AI_ENABLED          = _bool_env("AI_ENABLED", False)
AI_MIN_CONFIDENCE   = _float_env("AI_MIN_CONFIDENCE", 0.60)
# role 2 — position manager (HOLD / CLOSE / EXTEND). Fail-SAFE: no verdict
# = keep the deterministic protections. Independent of role 1 since v2.1.
AI_POSITION_ENABLED        = _bool_env("AI_POSITION_ENABLED", True)
AI_POSITION_INTERVAL       = _float_env("AI_POSITION_INTERVAL",
                                        30.0 if SCALPER else
                                        (300.0 if ULTRA_FAST else 900.0))
AI_POSITION_MIN_CONFIDENCE = _float_env("AI_POSITION_MIN_CONFIDENCE", 0.60)
AI_EXTEND_MIN_PROGRESS     = _float_env("AI_EXTEND_MIN_PROGRESS",
                                       0.85 if ULTRA_FAST else 0.90)
AI_EXTEND_TP_MULT          = _float_env("AI_EXTEND_TP_MULT", 1.15)
AI_EXTEND_LOCK_PCT         = _float_env("AI_EXTEND_LOCK_PCT", 0.80)
AI_POSITION_SCHEMA_VERSION = "position_review_v2"
# EXTEND needs agreement — but each extra vote is one more provider round-trip
# on a trade that lives for minutes, so the scalper asks once.
AI_POSITION_VOTES          = _int_env("AI_POSITION_VOTES", 1 if SCALPER else 2)
AI_POSITION_MODEL_2        = _str_env("AI_POSITION_MODEL_2", "")
AI_POSITION_MAX_TOKENS     = _int_env("AI_POSITION_MAX_TOKENS", 500)
# role 3 — market adaptation / bot directives (bounded, clamped, expiring).
AI_REGIME_ENABLED        = _bool_env("AI_REGIME_ENABLED", True)
AI_REGIME_INTERVAL       = 1800.0 if SCALPER else (3600.0 if ULTRA_FAST else 7200.0)
AI_REGIME_MODEL          = _str_env("AI_REGIME_MODEL", "")
AI_REGIME_SCHEMA_VERSION = "market_adaptation_v2"

# shared transport / protection
AI_TIMEOUT_SECONDS          = _float_env("AI_TIMEOUT_SECONDS", 4.0 if SCALPER else 8.0)
AI_MAX_RETRIES              = _int_env("AI_MAX_RETRIES", 0 if SCALPER else 1)
AI_CIRCUIT_FAILURES         = _int_env("AI_CIRCUIT_FAILURES", 5)
AI_CIRCUIT_COOLDOWN_SECONDS = _float_env("AI_CIRCUIT_COOLDOWN_SECONDS", 120.0)
AI_MAX_OUTPUT_TOKENS        = _int_env("AI_MAX_OUTPUT_TOKENS", 400)
AI_JOURNAL_PATH             = f"{DATA_ROOT_PATH}/data/ai_decisions.csv"
AI_JOURNAL_MAX_ROWS         = _int_env("AI_JOURNAL_MAX_ROWS", 20000)
AI_SCHEMA_VERSION           = "entry_review_v2"
AI_FEATURE_VERSION          = _str_env("AI_FEATURE_VERSION",
                                       "sr_h1_v2" if not ULTRA_FAST else "smc_mtf_v2")

# ── AI ⇄ loop decoupling (v2.1) ─────────────────────────────
# The AI runs on its own worker thread so a slow/rate-limited provider can
# never freeze break-even, trailing or trade reconciliation in the hot loop.
AI_ASYNC                = _bool_env("AI_ASYNC", True)   # False = call AI inline (v2.0 behaviour, demo only)
AI_WORKER_THREADS       = _int_env("AI_WORKER_THREADS", 2 if SCALPER else 1)
AI_QUEUE_MAX            = _int_env("AI_QUEUE_MAX", 12)   # extra work is refused
AI_RESULT_TTL_SECONDS   = _float_env("AI_RESULT_TTL_SECONDS", 120.0)
# Backpressure: when the queue is full the candidate is dropped AND the scan
# backs off, so a rate-limited provider is not hammered every single bar.
AI_QUEUE_BACKOFF_SECONDS = _float_env("AI_QUEUE_BACKOFF_SECONDS", 10.0)
# After an AI REJECT, wait before asking again about the same symbol — the
# market did not change in 3 seconds, and each ask costs a provider call.
AI_REJECT_COOLDOWN_SECONDS = _float_env("AI_REJECT_COOLDOWN_SECONDS",
                                        20.0 if SCALPER else 45.0)
# entry: a candidate waits at most this long for a verdict; older results are
# dropped as STALE and a fresh scan re-arms.
AI_ENTRY_MAX_AGE_SECONDS = _float_env("AI_ENTRY_MAX_AGE_SECONDS",
                                      10.0 if SCALPER else 45.0)
AI_ENTRY_MAX_DRIFT_ATR   = _float_env("AI_ENTRY_MAX_DRIFT_ATR",
                                      0.30 if SCALPER else 0.5)

# ── Entry latency budget (v2.2 scalper) ──────────────────────
# How long a candidate waits for the AI before the TIMEOUT POLICY applies.
#   AI_ENTRY_TIMEOUT_MODE = "block"   : no verdict = no trade (the v2.1 contract,
#                                       default for balanced / ultra_fast)
#   AI_ENTRY_TIMEOUT_MODE = "proceed" : no verdict inside the budget = the bot
#                                       trades its OWN signal (all deterministic
#                                       risk gates still run). A real REJECT from
#                                       the AI is always final — only silence,
#                                       errors, an open circuit or a full queue
#                                       are treated as "AI could not answer".
# Budget 0 = no early deadline in "block" mode (the candidate lives until
# AI_ENTRY_MAX_AGE); in "proceed" mode it falls back to 3s.
AI_ENTRY_TIMEOUT_MODE    = _str_env("AI_ENTRY_TIMEOUT_MODE",
                                    "proceed" if SCALPER else "block").lower()
if AI_ENTRY_TIMEOUT_MODE not in ("block", "proceed"):
    AI_ENTRY_TIMEOUT_MODE = "block"
AI_ENTRY_BUDGET_SECONDS  = _float_env("AI_ENTRY_BUDGET_SECONDS",
                                      3.0 if SCALPER else 0.0)
# position: CLOSE/EXTEND is applied only if the trade still looks like the one
# the AI reviewed (progress moved less than this fraction).
AI_POSITION_MAX_DRIFT_PROGRESS = _float_env("AI_POSITION_MAX_DRIFT_PROGRESS", 0.15)
AI_ENTRY_APPEAL               = _bool_env("AI_ENTRY_APPEAL", not ULTRA_FAST)
AI_ENTRY_APPEAL_MIN_CONFIDENCE = _float_env("AI_ENTRY_APPEAL_MIN_CONFIDENCE", 0.75)

# ── News (context for the AI + Telegram alerts) ──────────────
NEWS_ENABLED       = _bool_env("NEWS_ENABLED", True)
NEWS_RSS_URL       = _str_env("NEWS_RSS_URL", "https://www.fxstreet.com/rss/news")
NEWS_POLL_SECONDS  = 600.0
NEWS_MAX_AGE_HOURS = 2
NEWS_SEEN_PATH     = f"{DATA_ROOT_PATH}/data/news_seen.json"
NEWS_ALERT_KEYWORDS = ["fed", "fomc", "powell", "cpi", "nfp", "ecb", "boe"]
NEWS_MAX_ALERTS_PER_HOUR = 10

# ════════════════════════════════════════════════════════════
#   AI DIRECTIVES — the ONLY legal channel "AI → bot"
# ════════════════════════════════════════════════════════════
# Every tunable must be listed here or the AI cannot touch it. Ranges are
# chosen so the AI can only make the bot SAFER or leave it alone: loss limits,
# drawdown halts, margin buffer, stops, kill switch and sessions are NOT here.
AI_REGIME_ALLOWED = {
    "RISK_PERCENT_PER_TRADE":       (0.20, 1.00),
    "MAX_POSITIONS":                (1, 3),
    "MAX_SPREAD":                   (0.02, 0.08),
    "TP_SCALE":                     (0.85, 1.15),
    "BREAK_EVEN_PROGRESS":          (0.20, 0.55),
    "MIN_ATR_MULT":                 (0.60, 1.40),
    "ATR_EXPANSION_BLOCK":          (1.30, 2.50),
    "COOLDOWN_BARS":                (3, 12),
    "CONSEC_LOSS_LIMIT":            (2, 6),
    "AI_MIN_CONFIDENCE":            (0.60, 0.85),
    "AI_POSITION_MIN_CONFIDENCE":   (0.60, 0.85),
    "AI_EXTEND_MIN_PROGRESS":       (0.75, 0.95),
    "ML_MIN_CONFIDENCE":            (0.35, 0.60),
}
# A directive older than this expires and the baseline is restored (so a dead
# provider can never leave the bot stuck in a state chosen hours ago).
AI_REGIME_TTL_SECONDS = _float_env("AI_REGIME_TTL_SECONDS", 4 * 3600.0)
AI_REGIME_STATE_PATH  = f"{DATA_ROOT_PATH}/data/ai_directives.json"

LOG_PATH        = "logs/bot.log"
CRASH_LOG_PATH  = "logs/crash.log"

# Baseline for every AI-adjustable param, captured BEFORE any AI runs. Used to
# clamp "never loosen beyond the owner's baseline" and to restore on expiry.
AI_REGIME_BASELINE = {p: globals()[p] for p in AI_REGIME_ALLOWED if p in globals()}


# ════════════════════════════════════════════════════════════
#   FLAG HELPERS — one source of truth for "is this AI role live?"
#   (v2.0 bug: AI_POSITION_ENABLED=True silently did nothing because the
#    position manager was gated behind AI_ENABLED. Never again: every role
#    is gated on the plumbing only, and the status is printed at startup.)
# ════════════════════════════════════════════════════════════
def ai_plumbing_ready():
    """True when a provider is configured AND at least one API key exists."""
    return (str(globals().get("AI_PROVIDER", "disabled")).lower() != "disabled"
            and bool(globals().get("AI_API_KEYS"))
            and bool(str(globals().get("AI_BASE_URL", "")).strip()))


def ai_entry_on():
    """AI entry gate (veto before ordering)."""
    return bool(globals().get("AI_ENABLED", False)) and ai_plumbing_ready()


def ai_position_on():
    """AI position manager (HOLD / CLOSE / EXTEND on open trades)."""
    return bool(globals().get("AI_POSITION_ENABLED", False)) and ai_plumbing_ready()


def ai_regime_on():
    """AI market adaptation (bounded bot directives)."""
    return bool(globals().get("AI_REGIME_ENABLED", False)) and ai_plumbing_ready()


def ai_any_on():
    return ai_entry_on() or ai_position_on() or ai_regime_on()


def ai_entry_proceeds_on_timeout():
    """True when 'the AI could not answer in time' means 'trade the bot's own
    signal' instead of 'no trade'. A real REJECT is never overridden."""
    return str(globals().get("AI_ENTRY_TIMEOUT_MODE", "block")) == "proceed"


def ai_entry_budget_seconds():
    """The per-entry AI latency budget, clamped to stay inside the freshness
    window (a budget >= AI_ENTRY_MAX_AGE would drop every late candidate as
    STALE before the timeout policy could act). 0 = no early deadline."""
    budget = float(globals().get("AI_ENTRY_BUDGET_SECONDS", 0.0) or 0.0)
    max_age = float(globals().get("AI_ENTRY_MAX_AGE_SECONDS", 45.0) or 45.0)
    if budget <= 0:
        # In "proceed" mode a deadline is what makes the policy mean anything,
        # so a 0 budget falls back to 3s. In "block" mode 0 = no early deadline.
        if not ai_entry_proceeds_on_timeout():
            return 0.0
        budget = 3.0
    return min(budget, max_age * 0.8)


def ai_status_lines():
    """Human-readable 'what is actually running' report for startup logs.

    Any flag that is ON but CANNOT run (no key / provider disabled) is
    reported loudly — that was the v2.0 silent-dead-flow bug.
    """
    ready = ai_plumbing_ready()
    out = [f"profile={BOT_PROFILE} engine={ENTRY_ENGINE} ai_plumbing="
           + ("READY" if ready else "MISSING (AI_PROVIDER/AI_API_KEYS)")]
    for label, want, on in (
            ("AI entry gate", globals().get("AI_ENABLED", False), ai_entry_on()),
            ("AI position manager", globals().get("AI_POSITION_ENABLED", False),
             ai_position_on()),
            ("AI directives (regime)", globals().get("AI_REGIME_ENABLED", False),
             ai_regime_on())):
        if on:
            out.append(f"{label}: ON")
        elif want and not ready:
            out.append(f"{label}: requested ON but OFFLINE — no API key/provider"
                       " (add AI_API_KEYS to .env or set the flag false)")
        else:
            out.append(f"{label}: off")
    if ai_entry_on():
        budget = ai_entry_budget_seconds()
        if ai_entry_proceeds_on_timeout() and budget > 0:
            out.append(f"AI entry latency: {budget:.1f}s budget -> if the AI has "
                       f"not answered, the bot trades its own signal "
                       f"(a real REJECT is still final)")
        else:
            out.append("AI entry latency: no verdict = no trade (fail-closed)")
    if ENV_FILE_USED:
        out.append("secrets file: " + (
            "INSIDE the repo (git-ignored) - move it to ~/.boss-bot/.env"
            if os.path.dirname(os.path.abspath(ENV_FILE_USED)) == _REPO_DIR
            else "outside the repo (good)"))
    return out
