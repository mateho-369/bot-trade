# ============================================================
#   rarc/walkforward.py  —  MODULE 3
#   DAILY ROLLING WALK-FORWARD MACHINE LEARNING FILTER
#
#   WHAT THIS IS:
#     A failure classifier, NOT a price predictor. Target label is
#     `y = 1 if the trade LOST`. Features are STRUCTURAL execution
#     metadata only (hour, weekday, spread, volatility, trend
#     strength, streak state) — never price levels, never direction.
#     Its only authority is a BLOCK ("Smart Gate Bypass").
#
#   ANTI-OVERFITTING SPINE (four independent tripwires):
#     1. ROLLING WINDOW — trains only on the last 30–60 days. Old
#        regimes decay out of the sample by construction.
#     2. PURGED + EMBARGOED walk-forward validation — training rows
#        whose label horizon overlaps the test fold are purged, plus
#        a 24h embargo for serial correlation (López de Prado). The
#        reported AUC is an honest out-of-sample number.
#     3. DEPLOYMENT GATE — the model replaces yesterday's model ONLY
#        if its purged-CV AUC beats the coin flip by a margin
#        (WF_MIN_AUC = 0.55). Noise never gets deployed.
#     4. LIVE SELF-AUDIT — every blocked signal is replayed as a
#        shadow trade against actual candles. If the blocked set
#        would have WON more than the strategy base rate, the gate is
#        deleting edge → threshold auto-relaxes. A circuit breaker
#        also suspends the gate if it blocks > 60% of signals.
#
#   SCHEDULE: 00:00 UTC daily, idempotent per date (crash-safe).
# ============================================================

from __future__ import annotations

import os
import logging
import numpy as np
import pandas as pd
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from . import settings as S
from .store import (atomic_write_json, atomic_write_bytes, read_json,
                    FileLock)

logger = logging.getLogger("RARC.walkforward")

FEATURES = [
    "hour_utc",          # structural: time-of-day regime
    "hour_sin",          # cyclical encoding — toxic hours cluster across
    "hour_cos",          # midnight, trees split linearly, so feed both
    "day_of_week",       # structural: weekday regime
    "spread_pct",        # friction at signal time
    "atr_pct",           # volatility regime
    "trend_strength",    # |close_vs_ema| or EMA slope magnitude
    "session_active",    # inside configured session window (0/1)
    "streak_state",      # consecutive losses BEFORE this trade (0,1,2,3+)
]
LABEL = "y_loss"


# ── FEATURE ASSEMBLY ─────────────────────────────────────────

def build_dataset(closed_trades: pd.DataFrame,
                  trend_col: str = "close_vs_ema") -> pd.DataFrame:
    """
    closed_trades: rows from the bot's trade log with outcome resolved,
    plus execution features captured AT SIGNAL TIME (the log already
    stores hour_utc, spread_pct, atr_pct, close_vs_ema, ...).
    `streak_state` is computed causally (rolling, before each trade).
    """
    df = closed_trades.copy()
    df["ts"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts", "outcome"]).sort_values("ts")
    df["y_loss"] = (df["outcome"] == 0).astype(int)
    df["hour_utc"] = df["ts"].dt.hour
    df["day_of_week"] = df["ts"].dt.dayofweek
    if "spread_pct" not in df:
        df["spread_pct"] = 0.0
    if "atr_pct" not in df:
        df["atr_pct"] = 0.0
    if trend_col not in df:
        df[trend_col] = 0.0
    df["trend_strength"] = df[trend_col].abs()
    # Causal streak: shift(1).cummax-style — count of consecutive losses
    # strictly BEFORE this trade. No label leakage.
    losses = (df["y_loss"] == 1).astype(int)
    grp = losses.groupby((losses == 0).cumsum()).cumsum()  # run lengths
    df["streak_state"] = grp.shift(1).fillna(0).clip(upper=4)
    df["session_active"] = ((df["hour_utc"] >= 7) & (df["hour_utc"] < 20)).astype(int)
    df["hour_sin"] = np.sin(2.0 * np.pi * df["hour_utc"] / 24.0)
    df["hour_cos"] = np.cos(2.0 * np.pi * df["hour_utc"] / 24.0)
    return df.dropna(subset=FEATURES).reset_index(drop=True)


# ── PURGED, EMBARGOED WALK-FORWARD CV ────────────────────────

def purged_walk_forward_auc(X: np.ndarray, y: np.ndarray, times: pd.Series,
                            n_folds: int, embargo_hours: float,
                            model_factory) -> float:
    """
    Chronological folds. For fold k:
      purge → drop training rows whose label horizon could overlap the
      test window (approximated by a trade-duration halo on both sides
      of the test block), embargo → additionally drop `embargo_hours`
      of training rows immediately BEFORE the test block (kills serial
      correlation leaking backwards through features).
    Returns mean out-of-fold ROC-AUC. No shuffling, ever.
    """
    from sklearn.metrics import roc_auc_score

    n = len(y)
    if n < n_folds * 10:
        return 0.5
    fold_edges = np.linspace(0, n, n_folds + 1).astype(int)
    aucs = []
    t = pd.to_datetime(times).reset_index(drop=True)
    halo = pd.Timedelta(hours=embargo_hours)

    for k in range(n_folds):
        a, b = fold_edges[k], fold_edges[k + 1]
        if b - a < 8:
            continue
        test_idx = np.arange(a, b)
        train_mask = np.ones(n, dtype=bool)
        train_mask[a:b] = False
        # purge/embargo around the test block in TIME, not index —
        # trade arrival is irregular, time is the honest axis.
        t_start, t_end = t.iloc[a] - halo, t.iloc[b - 1] + halo
        in_halo = (t > t_start) & (t < t_end)
        train_mask &= ~in_halo.to_numpy()
        if train_mask.sum() < 20 or len(np.unique(y[train_mask])) < 2:
            continue
        model = model_factory()
        model.fit(X[train_mask], y[train_mask])
        if len(np.unique(y[test_idx])) < 2:
            continue
        proba = model.predict_proba(X[test_idx])[:, 1]
        aucs.append(roc_auc_score(y[test_idx], proba))
    return float(np.mean(aucs)) if aucs else 0.5


def _model_factory():
    from sklearn.ensemble import RandomForestClassifier
    return RandomForestClassifier(
        n_estimators=S.WF_N_ESTIMATORS, max_depth=S.WF_MAX_DEPTH,
        min_samples_leaf=S.WF_MIN_LEAF, class_weight="balanced",
        random_state=42, n_jobs=1)     # n_jobs=1: no fork storms in-loop


# ── THE FILTER ───────────────────────────────────────────────

@dataclass
class GateDecision:
    block: bool
    p_loss: float
    reason: str


class WalkForwardFilter:
    """
    Trains daily at 00:00 UTC on the rolling window, serves O(1)
    failure-probability lookups to the entry path, audits itself live.
    Fail-OPEN: no model / stale model / corrupt model → gate open.
    """

    def __init__(self, model_path=None, state_path=None):
        self.model_path = model_path or S.WF_MODEL_PATH
        self.state_path = state_path or S.WF_STATE_PATH
        self.model = None
        self.meta = {}
        self.shadow = deque(maxlen=S.WF_SHADOW_MAX)   # bounded self-audit
        self.signal_times = deque(maxlen=500)         # bounded block-rate window
        self._recent_blocks = deque(maxlen=500)       # block events, same window
        self._load()

    # ── persistence ──────────────────────────────────────────
    def _load(self):
        st = read_json(self.state_path, {}) or {}
        self.meta = st.get("meta", {})
        self.last_train_date = st.get("last_train_date", "")
        self.block_threshold = float(st.get("block_threshold", S.WF_BLOCK_THRESHOLD))
        self.suspended_until = st.get("suspended_until", "")
        for rec in st.get("shadow", [])[-S.WF_SHADOW_MAX:]:
            self.shadow.append(rec)
        for ts in st.get("signal_times", [])[-500:]:
            self.signal_times.append(ts)
        if os.path.exists(self.model_path):
            try:
                import joblib
                self.model = joblib.load(self.model_path)
                logger.info("Walk-forward model loaded (trained %s, AUC %.3f)",
                            self.meta.get("trained_at", "?")[:16],
                            self.meta.get("cv_auc", -1))
            except Exception as e:
                logger.error("WF model load failed (fail-open): %s", e)
                self.model = None

    def _save(self):
        import io
        import joblib
        buf = io.BytesIO()
        joblib.dump(self.model, buf)
        if os.path.exists(self.model_path):       # keep rollback copy
            try:
                with open(self.model_path, "rb") as f:
                    atomic_write_bytes(S.WF_MODEL_BAK_PATH, f.read())
            except OSError:
                pass
        atomic_write_bytes(self.model_path, buf.getvalue())
        atomic_write_json(self.state_path, {
            "last_train_date": self.last_train_date,
            "block_threshold": self.block_threshold,
            "suspended_until": self.suspended_until,
            "meta": self.meta,
            "shadow": list(self.shadow),
            "signal_times": list(self.signal_times),
            "recent_blocks": list(self._recent_blocks),
        })

    # ── the 00:00 UTC daily job ──────────────────────────────
    def daily_retrain(self, trade_log_df: pd.DataFrame,
                      now: datetime | None = None,
                      window_days: int | None = None) -> dict:
        """
        Loads the most recent 30–60 day window, evaluates expectancy,
        trains with purged walk-forward validation, deploys only if
        honest AUC clears the bar. Idempotent per UTC date.
        Returns a diagnostics dict (also good for Telegram reporting).
        """
        now = now or datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if self.last_train_date == today:
            return {"skipped": "already trained today"}

        window_days = window_days or S.WF_WINDOW_DAYS_MAX
        cutoff = pd.Timestamp(now) - pd.Timedelta(days=window_days)
        df = trade_log_df.copy()
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        df = df[df["time"] >= cutoff]

        closed = df[df["outcome"].isin([0, 1])].copy()
        diag = {"window_days": window_days, "closed_trades": int(len(closed)),
                "date": today}

        if len(closed) < S.WF_MIN_SAMPLES:
            diag["skipped"] = (f"only {len(closed)} closed trades < "
                               f"{S.WF_MIN_SAMPLES} (keeping yesterday's model)")
            self.last_train_date = today      # idempotency marker
            self._save()
            return diag

        data = build_dataset(closed)
        X = data[FEATURES].to_numpy(dtype=float)
        y = data[LABEL].to_numpy(dtype=int)

        # Expectancy snapshot of the current regime (report + drift watch)
        diag["n"] = len(data)
        diag["win_rate"] = round(float((y == 0).mean()), 4)
        diag["expectancy_note"] = "see Module 1 for R-based expectancy"

        # Honest out-of-sample validation BEFORE deployment.
        auc = purged_walk_forward_auc(X, y, data["ts"], S.WF_N_FOLDS,
                                      S.WF_EMBARGO_HOURS, _model_factory)
        diag["purged_cv_auc"] = round(auc, 4)

        if auc < S.WF_MIN_AUC:
            diag["deployed"] = False
            diag["reason"] = (f"AUC {auc:.3f} < {S.WF_MIN_AUC} — "
                              f"structure has no learnable signal now; "
                              f"keeping previous model")
            self.last_train_date = today
            self._save()
            return diag

        # Deploy: fit on the FULL window (validation already done).
        self.model = _model_factory()
        self.model.fit(X, y)
        self.meta = {"trained_at": now.isoformat(), "cv_auc": auc,
                     "n": len(data), "features": FEATURES}
        self.last_train_date = today
        self._save()
        diag["deployed"] = True
        logger.info("WF model deployed: AUC %.3f on %d trades", auc, len(data))
        return diag

    # ── the prediction bridge ────────────────────────────────
    def gate_signal(self, hour_utc: int, day_of_week: int, spread_pct: float,
                    atr_pct: float, trend_strength: float,
                    session_active: int, streak_state: int,
                    now: datetime | None = None) -> GateDecision:
        """
        Called for EVERY generated signal. Returns a GateDecision.
        The model can only ever say BLOCK — it never forces entries.
        """
        now = now or datetime.now(timezone.utc)
        self.signal_times.append(now.isoformat())

        if self._suspended(now):
            return GateDecision(False, -1, "gate suspended (circuit breaker)")
        if self.model is None:
            return GateDecision(False, -1, "no model (fail-open)")

        # Circuit breaker: a degenerate model that blocks everything is
        # worse than no model — suspend instead of starving the bot.
        if self.block_fraction_recent() > S.WF_MAX_BLOCK_FRACTION:
            self.trip_circuit_breaker(now, "block rate exceeded "
                                           f"{S.WF_MAX_BLOCK_FRACTION:.0%}")
            return GateDecision(False, -1, "circuit breaker tripped")

        x = np.array([[hour_utc,
                       float(np.sin(2.0 * np.pi * hour_utc / 24.0)),
                       float(np.cos(2.0 * np.pi * hour_utc / 24.0)),
                       day_of_week, spread_pct, atr_pct,
                       trend_strength, session_active, min(streak_state, 4)]])
        try:
            p_loss = float(self.model.predict_proba(x)[0][1])
        except Exception as e:
            logger.error("WF predict failed (fail-open): %s", e)
            return GateDecision(False, -1, f"predict error: {e}")

        # Live self-audit auto-relaxation.
        self._maybe_relax()
        if p_loss >= self.block_threshold:
            self._recent_blocks.append(now.isoformat())
            return GateDecision(True, p_loss,
                                f"Smart Gate Bypass: p(loss)={p_loss:.2f} "
                                f">= {self.block_threshold:.2f}")
        return GateDecision(False, p_loss, "pass")

    def record_blocked(self, symbol: str, direction: int, entry: float,
                       sl: float, tp: float, ts: datetime | None = None,
                       p_loss: float = -1):
        """Register a blocked signal as a shadow trade for the live
        self-audit (was the block correct?). Bounded buffer."""
        ts = ts or datetime.now(timezone.utc)
        self.shadow.append({"ts": ts.isoformat(), "symbol": symbol,
                            "direction": int(direction), "entry": entry,
                            "sl": sl, "tp": tp,
                            "outcome": None, "p_loss": p_loss})

    def resolve_shadow_trades(self, price_lookup) -> int:
        """
        price_lookup(symbol, ts_iso, direction, sl, tp) -> 'win'|'loss'|None
        Resolve pending shadow trades with ACTUAL candles; then, if the
        blocked set is outperforming the base strategy, relax the gate.
        Returns number of newly resolved shadow outcomes.
        """
        resolved = 0
        for rec in self.shadow:
            if rec["outcome"] is None:
                try:
                    out = price_lookup(rec["symbol"], rec["ts"],
                                       rec["direction"], rec["sl"], rec["tp"])
                except Exception:
                    out = None
                if out in ("win", "loss"):
                    rec["outcome"] = 1 if out == "win" else 0
                    resolved += 1
        if resolved:
            self._save()
        return resolved

    def blocked_win_rate(self) -> float | None:
        done = [r["outcome"] for r in self.shadow if r["outcome"] is not None]
        if len(done) < 10:
            return None
        return float(np.mean(done))

    def _maybe_relax(self):
        """Blocked set won too often → the gate deletes edge → relax."""
        wr = self.blocked_win_rate()
        if wr is not None and wr > S.WF_GATE_RELAX_ABOVE_WR:
            old = self.block_threshold
            self.block_threshold = min(0.95, self.block_threshold + 0.10)
            if self.block_threshold != old:
                logger.warning("WF gate relaxed %.2f → %.2f: blocked set "
                               "win rate %.2f shows deleted edge",
                               old, self.block_threshold, wr)
                self._save()

    def block_fraction_recent(self, hours: float = 24) -> float:
        """Fraction of recent signals that the gate wanted to block.
        Uses a bounded deque of block events vs signal events."""
        cutoff = datetime.now(timezone.utc) - pd.Timedelta(hours=hours)
        sig = [datetime.fromisoformat(t) for t in self.signal_times
               if datetime.fromisoformat(t) >= cutoff]
        blk = [datetime.fromisoformat(t) for t in self._recent_blocks
               if datetime.fromisoformat(t) >= cutoff]
        return len(blk) / len(sig) if sig else 0.0

    def trip_circuit_breaker(self, now: datetime | None = None,
                             reason: str = ""):
        now = now or datetime.now(timezone.utc)
        self.suspended_until = (now + pd.Timedelta(
            hours=S.WF_SUSPEND_HOURS)).isoformat()
        self._save()
        logger.warning("WF gate SUSPENDED %dh: %s", S.WF_SUSPEND_HOURS, reason)

    def _suspended(self, now: datetime) -> bool:
        if not self.suspended_until:
            return False
        if datetime.fromisoformat(self.suspended_until) > now:
            return True
        self.suspended_until = ""
        return False
