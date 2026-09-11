# ============================================================
#   rarc/settings.py
#   Regime Adaptation & Risk Correction Engine — all knobs.
#
#   DESIGN RULES (read before changing anything):
#   1. Every adaptive quantity is BOUNDED. No multiplier, probability,
#      or risk level in this file is allowed to push the bot beyond
#      its hard bounds — the bounds here ARE the contract.
#   2. The engine may only ever SHRINK risk or BLOCK entries.
#      It can never enlarge risk beyond BASELINE_RISK_PCT and can
#      never force a trade open.
#   3. Safety gates are fail-CLOSED (spread gate, cooldown, ROR cap).
#      Statistical gates are fail-OPEN (ML smart gate, blindspot
#      matrix) — missing data must not silence the bot, but missing
#      safety must not arm it.
#   4. All timestamps are UTC.
# ============================================================

import os
import sys

# Optional bridge to the bot's global config (only when running inside
# the bot repo). RARC works standalone too.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bot_config():
    try:
        import config as _cfg  # noqa
        return _cfg
    except Exception:
        return None


_CFG = _bot_config()

# ── Paths (persisted state lives under the bot's data root) ──
_DATA_ROOT = getattr(_CFG, "DATA_ROOT_PATH", "SMC_Bot_Data") if _CFG else "SMC_Bot_Data"
RARC_DIR = os.path.join(_DATA_ROOT, "rarc")

BLACKLIST_MATRIX_PATH = os.path.join(RARC_DIR, "risk_blacklist_matrix.json")
FRICTION_LOG_PATH     = os.path.join(RARC_DIR, "friction_log.jsonl")
FRICTION_STATE_PATH   = os.path.join(RARC_DIR, "friction_state.json")
ATR_STATE_PATH        = os.path.join(RARC_DIR, "atr_regulator.json")
COOLDOWN_STATE_PATH   = os.path.join(RARC_DIR, "cooldown_state.json")
WF_STATE_PATH         = os.path.join(RARC_DIR, "walkforward_state.json")
WF_MODEL_PATH         = os.path.join(RARC_DIR, "wf_model.pkl")
WF_MODEL_BAK_PATH     = WF_MODEL_PATH + ".bak"
LOCK_PATH             = os.path.join(RARC_DIR, "rarc.lock")

# ── MODULE 1 — Historical boundary extraction ────────────────
# Risk of ruin: analytic target. 0.0001 == 0.01% probability of
# ever touching the ruin drawdown barrier (fixed-fractional sizing).
TARGET_RISK_OF_RUIN    = 0.0001
# "Ruin" means equity touches this drawdown from peak. Aligned with the
# bot's existing MAX_DRAWDOWN_PERCENT kill-switch.
RUIN_DRAWDOWN_PCT      = getattr(_CFG, "MAX_DRAWDOWN_PERCENT", 30.0) / 100.0
# Historical max streak is a SAMPLE, not a law of nature: plan for a
# worse streak than ever observed.
STREAK_SAFETY_FACTOR   = 1.5    # scale observed worst streak by this
STREAK_PAD             = 2      # ...and add this many losses on top
# A pure survival constraint: S_plan consecutive losses at risk f must
# lose no more than this fraction of equity.
STREAK_MAX_DD_PCT      = 0.12
# Monte Carlo verification of the analytic cap (bootstrap of empirical
# R-multiple distribution). Deterministic seed = reproducible caps.
MC_PATHS               = 4000
MC_HORIZON_TRADES      = 3000
MC_SEED                = 42

# Blindspot extraction
TOXIC_MIN_SAMPLES      = 30     # a bucket needs this many trades to be enforceable
TOXIC_PF_THRESHOLD     = 1.0    # expectancy decayed below PF 1.0 → toxic
SHRINKAGE_STRENGTH     = 40     # Beta-prior strength (pseudo-trades) pulling
                                # bucket win rates toward the global win rate
SPREAD_BIN_PERCENTILES = (50, 60, 70, 75, 80, 85, 90, 92.5, 95, 97.5, 99)
# Final hard cap applied by Module 1 regardless of everything else
RISK_PCT_FLOOR         = 0.10   # never size below 0.10% (else broker min lot
                                # makes sizing meaningless)
BASELINE_RISK_PCT      = getattr(_CFG, "RISK_PERCENT_PER_TRADE", 1.0)

# Re-extract boundaries this often (hourly job 24 → daily is fine;
# the matrix is a long-horizon object, it should move slowly).
MATRIX_MAX_AGE_HOURS   = 24 * 14

# ── MODULE 2 — Friction & volatility ─────────────────────────
FRICTION_WINDOW_HOURS  = 3      # trailing window per spec
FRICTION_SAMPLE_MAX    = 400    # bounded deque → no memory growth
FRICTION_GATE_METRIC   = "p95"  # p50 | p90 | p95 | max — robust friction read
# Hysteresis: re-open only after this many consecutive clean hourly
# checks, so a single calm hour doesn't re-arm the bot in a storm.
FRICTION_REOPEN_STREAK = 2
ATR_REGIME_LEN         = 14     # ATR period for the hourly regulator
ATR_REF_DAYS           = 30     # reference = trailing 30d median hourly ATR
# Hourly SL/TP expansion/contraction BOUNDS (the "mathematical cage"):
ATR_MULT_MIN           = 0.75   # never tighten stops below 0.75 × nominal
ATR_MULT_MAX           = 1.50   # never widen stops beyond 1.50 × nominal
ATR_RATIO_EXPONENT     = 1.0    # vol_ratio^exp → multiplier scaling

# ── MODULE 3 — Daily walk-forward filter ─────────────────────
WF_RETRAIN_HOUR_UTC    = 0      # 00:00 UTC daily job
WF_WINDOW_DAYS_MIN     = 30
WF_WINDOW_DAYS_MAX     = 60
WF_MIN_SAMPLES         = 60     # refuse to train on less (overfit guard)
WF_EMBARGO_HOURS       = 24     # purge/embargo gap around each CV fold
WF_N_FOLDS             = 4      # purged walk-forward folds for validation
# Deploy the model only if its purged-CV AUC beats the coin flip by a
# real margin. If not, keep yesterday's model. This is the primary
# anti-overfitting tripwire.
WF_MIN_AUC             = 0.55
# Block only when the model is quite sure the trade is a failure.
WF_BLOCK_THRESHOLD     = 0.65
# Circuit breaker: if the gate blocks more than this fraction of the
# trailing signals, the model has gone degenerate — suspend it.
WF_MAX_BLOCK_FRACTION  = 0.60
WF_SUSPEND_HOURS       = 24
# Self-audit: blocked signals are replayed as shadow trades; if the
# blocked subset would have won more than the strategy's base rate,
# the gate is deleting edge and gets relaxed automatically.
WF_SHADOW_MAX          = 300    # bounded shadow-trade buffer
WF_GATE_RELAX_ABOVE_WR = 0.55   # blocked-set win rate above this → relax
# Model hyperparameters (small forest = regularization, fast retrain)
WF_N_ESTIMATORS        = 150
WF_MAX_DEPTH           = 4
WF_MIN_LEAF            = 10

# ── MODULE 4 — Cooldown matrix ───────────────────────────────
COOLDOWN_FRAME_HOURS   = 12     # localized rolling frame for streak counting
# Tier ladder: consecutive live losses → (risk multiplier, action)
# Multipliers multiply BASELINE_RISK_PCT and are clamped to
# [RISK_PCT_FLOOR/BASELINE, 1.0] — the mathematical cage.
COOLDOWN_TIERS = {
    0: {"risk_mult": 1.00, "action": "normal"},
    1: {"risk_mult": 1.00, "action": "normal"},
    2: {"risk_mult": 0.50, "action": "downscale"},          # 1.0% → 0.50%
    3: {"risk_mult": 0.25, "action": "defensive"},          # 1.0% → 0.25%
    4: {"risk_mult": 0.25, "action": "cooldown"},           # + hard gate
}
COOLDOWN_DURATION_HOURS = 12   # operational cooldown window
# Recovery hysteresis: after a cooldown expires or a win lands, the bot
# climbs back ONE tier at a time and must survive this many resolved
# trades per tier before the next promotion.
RECOVERY_TRADES_PER_TIER = 2

# ── Scheduler / ops ──────────────────────────────────────────
MAINTENANCE_EVERY_SECONDS = 30  # engine polls due jobs this often
TRAIN_IN_SUBPROCESS       = False  # optional memory isolation for retrain
LOCK_TIMEOUT_SECONDS      = 10  # stale file-lock takeover
LOG = None  # engine wires the bot's logger here; falls back to "RARC"


def ensure_dirs():
    os.makedirs(RARC_DIR, exist_ok=True)
