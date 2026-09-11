# ============================================================
#   rarc/boundaries.py  —  MODULE 1
#   HISTORICAL RISK BOUNDARY & BLINDSPOT EXTRACTION
#   (The Multi-Year Backtest Learner)
#
#   JOB:
#     Parse multi-year historical backtest trade logs and distill
#     them into a small, versioned set of MATHEMATICAL SAFETY
#     CONSTRAINTS (not memorized price paths):
#
#     1. Max Consecutive Losses → dynamic Risk_Per_Trade cap holding
#        the mathematical Risk of Ruin below TARGET_RISK_OF_RUIN
#        (default 0.01%).
#     2. Regime blindspots — structural features where expectancy
#        decays below PF 1.0:
#          · Toxic Hours of Day (UTC)
#          · Toxic Days of Week
#          · Maximum Bid-Ask Spread Tolerance (friction decay point)
#        exported as the continuous `risk_blacklist_matrix`.
#
#   CONTRACT:
#     - Input: one trades DataFrame/CSV with columns
#         time (entry ts), outcome (1/0), profit (R or currency),
#         spread_pct, [symbol]
#       The bot's existing backtester trade_log already matches this
#       after `load_trades_csv()` normalization.
#     - Output: risk_blacklist_matrix.json + a loaded BlacklistMatrix
#       object consumed by rarc.engine at every entry decision.
#     - The matrix moves SLOWLY (re-extract ≤ 1×/day). It is a
#       long-horizon structural object, not a daily noise toy.
# ============================================================

from __future__ import annotations

import os
import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import settings as S
from . import mathlib as M
from .store import atomic_write_json, read_json

logger = logging.getLogger("RARC.boundaries")

MATRIX_VERSION = 1

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ── INPUT LOADING ────────────────────────────────────────────

def load_trades_csv(path) -> pd.DataFrame:
    """
    Normalize a backtest/live trade log into the canonical schema:
      ts: datetime(UTC) | outcome: {0,1} | profit: float | spread_pct: float
    Tolerates the repo's existing trade_log column names.
    """
    df = pd.read_csv(path)
    ren = {}
    if "time" in df.columns and "entry_time" not in df.columns:
        ren["time"] = "ts"
    if "entry_time" in df.columns:
        ren["entry_time"] = "ts"
    df = df.rename(columns=ren)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("UTC")
    df["outcome"] = pd.to_numeric(df["outcome"], errors="coerce")
    df["profit"] = pd.to_numeric(df["profit"], errors="coerce")
    df["spread_pct"] = pd.to_numeric(df.get("spread_pct", np.nan),
                                     errors="coerce")
    df = df.dropna(subset=["ts", "outcome", "profit"])
    df["hour_utc"] = df["ts"].dt.hour
    df["dow_utc"] = df["ts"].dt.dayofweek
    return df.sort_values("ts").reset_index(drop=True)


# ── 1. STREAK & RISK CAP ─────────────────────────────────────

def extract_risk_cap(trades: pd.DataFrame,
                     baseline_risk_pct: float | None = None) -> dict:
    """
    Scan the multi-year outcome sequence and derive the hard cap on
    Risk_Per_Trade such that mathematical Risk of Ruin stays below the
    target (0.01% default), verified by Monte Carlo on the empirical
    R-multiple distribution.
    """
    base = baseline_risk_pct if baseline_risk_pct is not None else S.BASELINE_RISK_PCT

    # Prefer true R-multiples when available; fall back to normalized
    # profit sign-statistics (win rate & payoff ratio still fully
    # determine the diffusion bound up to scale).
    profits = trades["profit"].to_numpy(dtype=float)
    if "r_multiple" in trades.columns and trades["r_multiple"].notna().sum() > 20:
        r = trades["r_multiple"].to_numpy(dtype=float)
        r = r[np.isfinite(r)]
    else:
        # Re-center currency profits around the median losing trade so
        # the R-distribution has unit typical loss. Robust to currency.
        med_loss = -np.median(np.abs(profits[profits < 0])) if (profits < 0).any() else 1.0
        scale = abs(med_loss) if med_loss and np.isfinite(med_loss) and med_loss > 0 else 1.0
        r = profits / scale

    worst_streak = M.max_consecutive_losses(trades["outcome"].astype(bool).tolist())
    n = len(r)
    exp_streak = M.expected_max_streak(n, 1.0 - (r > 0).mean()) if n else 0.0

    analysis = M.max_risk_fraction(
        r,
        target_ror=S.TARGET_RISK_OF_RUIN,
        ruin_dd=S.RUIN_DRAWDOWN_PCT,
        observed_streak=worst_streak,
        streak_safety=S.STREAK_SAFETY_FACTOR,
        streak_pad=S.STREAK_PAD,
        streak_max_dd=S.STREAK_MAX_DD_PCT,
        f_min=S.RISK_PCT_FLOOR / 100.0,
        f_max=max(0.05, base / 100.0 * 2.0),
        mc_verify=True, seed=S.MC_SEED,
        mc_horizon=S.MC_HORIZON_TRADES, mc_paths=S.MC_PATHS,
    )

    cap_pct = float(np.clip(analysis.r_cap * 100.0, S.RISK_PCT_FLOOR, base))
    streak_prob = M.streak_probability(n, 1.0 - analysis.win_rate, worst_streak)

    return {
        "capped_risk_pct": round(cap_pct, 4),
        "baseline_risk_pct": base,
        "observed_max_streak": worst_streak,
        "planned_max_streak": analysis.planned_streak,
        "expected_max_streak_iid": round(exp_streak, 1),
        "streak_likelihood_pct": round(streak_prob * 100, 1),
        "win_rate": round(analysis.win_rate, 4),
        "expectancy_r": round(analysis.expectancy_r, 5),
        "avg_win_r": round(analysis.avg_win_r, 3),
        "avg_loss_r": round(analysis.avg_loss_r, 3),
        "n_trades": n,
        "ror_at_cap_analytic": float(f"{analysis.ror_at_cap:.3e}"),
        "ror_at_cap_montecarlo": (float(f"{analysis.ror_mc_at_cap:.3e}")
                                  if np.isfinite(analysis.ror_mc_at_cap) else None),
        "notes": analysis.notes,
    }


# ── 2. BLINDSPOTS (toxic hours / days / spread ceiling) ──────

def _bucket_stats(sub: pd.DataFrame):
    wins = int((sub["outcome"] == 1).sum())
    n = len(sub)
    avg_w = float(sub.loc[sub["profit"] > 0, "profit"].mean()) if wins else 0.0
    losses = sub.loc[sub["outcome"] == 0, "profit"]
    avg_l = float(-losses.mean()) if len(losses) else 0.0
    raw_pf = M.profit_factor(sub["profit"].to_numpy())
    return wins, n, avg_w, avg_l, raw_pf


def extract_blindspots(trades: pd.DataFrame,
                       min_samples: int | None = None,
                       pf_threshold: float | None = None,
                       per_symbol: bool = True) -> dict:
    """
    Automated failure-correlation parser over STRUCTURAL features only
    (hour, weekday, spread). Never looks at price levels or direction —
    that is the anti-overfitting line this module does not cross.

    A bucket is toxic when its SHRUNKEN profit factor drops below
    `pf_threshold` (1.0) AND it carries >= min_samples trades.
    Shrinkage (Beta prior toward the global win rate) keeps 3-trade
    buckets from being promoted into law.
    """
    min_samples = min_samples or S.TOXIC_MIN_SAMPLES
    pf_threshold = pf_threshold if pf_threshold is not None else S.TOXIC_PF_THRESHOLD
    g_rate = float((trades["outcome"] == 1).mean()) if len(trades) else 0.5

    def scan(df: pd.DataFrame, key: str, labels) -> dict:
        out = {}
        for val, label in labels:
            sub = df[df[key] == val]
            wins, n, avg_w, avg_l, raw_pf = _bucket_stats(sub)
            if n == 0:
                continue
            shrunk_pf = M.bucket_toxicity(wins, n, avg_w, avg_l,
                                          g_rate, S.SHRINKAGE_STRENGTH)
            out[label] = {
                "n": n,
                "wins": wins,
                "raw_pf": round(raw_pf, 3),
                "shrunk_pf": round(shrunk_pf, 3),
                "toxic": bool(shrunk_pf < pf_threshold and n >= min_samples),
            }
        return out

    hours = scan(trades, "hour_utc", [(h, h) for h in range(24)])
    days = scan(trades, "dow_utc", [(d, WEEKDAYS[d]) for d in range(7)])

    # ── Friction decay: Maximum Bid-Ask Spread Tolerance ────────
    # Sort trades by recorded spread and walk candidate thresholds
    # upward. Tolerance = the HIGHEST threshold whose PF (trades taken
    # at or below it) still clears 1.0 with enough samples. Beyond it,
    # friction has mathematically eaten the edge.
    spread = trades["spread_pct"].dropna()
    tolerance = None
    decay_spread = None
    spread_curve = []
    if len(spread) >= min_samples:
        qs = np.percentile(spread, S.SPREAD_BIN_PERCENTILES)
        for q in qs:
            q = float(q)
            sub = trades[trades["spread_pct"] <= q]
            if len(sub) < min_samples:
                continue
            _, _, _, _, pf = _bucket_stats(sub)
            spread_curve.append({"spread_le_pct": round(q, 4),
                                 "n": len(sub), "pf": round(pf, 3)})
            if pf >= 1.0:
                tolerance = q          # keep the highest good threshold
            elif decay_spread is None:
                decay_spread = q       # first threshold where PF < 1.0
    if tolerance is None and len(spread) > 0:
        # Whole sample is friction-starved: fall back to the 75th pct
        # so the gate still has SOME number to enforce.
        tolerance = float(np.percentile(spread, 75))
        decay_spread = decay_spread or tolerance

    return {
        "global": {"n": len(trades),
                   "win_rate": round(g_rate, 4),
                   "profit_factor": round(M.profit_factor(trades["profit"]), 3)},
        "toxic_hours_utc": hours,
        "toxic_weekdays": days,
        "max_spread_tolerance_pct": (round(tolerance, 4)
                                     if tolerance is not None else None),
        "friction_decay_spread_pct": (round(decay_spread, 4)
                                      if decay_spread is not None else None),
        "spread_pf_curve": spread_curve,
    }


# ── 3. MATRIX EXPORT / LOAD ──────────────────────────────────

def build_matrix(trades: pd.DataFrame,
                 baseline_risk_pct: float | None = None,
                 symbol: str = "ALL") -> dict:
    """Full Module 1 pass → risk_blacklist_matrix dict (not yet saved)."""
    trades = trades.copy()
    if "ts" not in trades.columns:
        trades = trades.rename(columns={"time": "ts", "entry_time": "ts"})
    trades["ts"] = pd.to_datetime(trades["ts"], utc=True, errors="coerce")
    trades = trades.dropna(subset=["ts", "outcome", "profit"]).sort_values("ts")
    trades["hour_utc"] = trades["ts"].dt.hour
    trades["dow_utc"] = trades["ts"].dt.dayofweek
    risk = extract_risk_cap(trades, baseline_risk_pct)
    blind = extract_blindspots(trades)
    return {
        "schema_version": MATRIX_VERSION,
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_window": {
            "from": str(trades["ts"].min()),
            "to": str(trades["ts"].max()),
            "n_trades": int(len(trades)),
        },
        "risk_cap": risk,
        "blindspots": blind,
        "symbols": [symbol],
    }


def save_matrix(matrix: dict, path: str | None = None):
    atomic_write_json(path or S.BLACKLIST_MATRIX_PATH, matrix)


def load_matrix(path: str | None = None) -> "BlacklistMatrix | None":
    raw = read_json(path or S.BLACKLIST_MATRIX_PATH)
    return BlacklistMatrix.from_dict(raw) if raw else None


# ── CONSUMER VIEW ────────────────────────────────────────────

@dataclass
class BlacklistMatrix:
    """
    In-memory view the engine gates against. All lookups O(1).
    Missing matrix → all gates open (fail-open: statistics may be
    absent; hard safety never is).
    """
    toxic_hours: set = field(default_factory=set)
    toxic_days: set = field(default_factory=set)
    max_spread_tolerance: float | None = None
    friction_decay_spread: float | None = None
    capped_risk_pct: float | None = None
    computed_at: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "BlacklistMatrix":
        b = d.get("blindspots", {})
        rc = d.get("risk_cap", {})
        label_to_dow = {label: i for i, label in enumerate(WEEKDAYS)}
        return cls(
            toxic_hours={int(h) for h, v in b.get("toxic_hours_utc", {}).items()
                         if v.get("toxic")},
            toxic_days={label_to_dow[d] for d, v in b.get("toxic_weekdays", {}).items()
                        if v.get("toxic") and d in label_to_dow},
            max_spread_tolerance=b.get("max_spread_tolerance_pct"),
            friction_decay_spread=b.get("friction_decay_spread_pct"),
            capped_risk_pct=rc.get("capped_risk_pct"),
            computed_at=d.get("computed_at_utc", ""),
            raw=d,
        )

    def hour_toxic(self, hour_utc: int) -> bool:
        return hour_utc in self.toxic_hours

    def day_toxic(self, dow_utc: int) -> bool:
        return dow_utc in self.toxic_days

    def spread_ok(self, spread_pct: float) -> bool:
        if self.max_spread_tolerance is None:
            return True
        return spread_pct <= self.max_spread_tolerance
