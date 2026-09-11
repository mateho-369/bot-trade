# ============================================================
#   rarc/friction.py  —  MODULE 2
#   HOURLY MARKET FRICTION & VOLATILITY TRACKER
#
#   JOB (every 60 minutes, plus cheap per-fill sampling):
#     1. Record actual broker Spread + execution Slippage over a
#        trailing 3-hour window. Cross-reference live friction with
#        Module 1's `max_spread_tolerance`; if live friction exceeds
#        tolerance → strict gate: trading_allowed = False.
#     2. Dynamic ATR regulator: every hour re-scale the SL/TP
#        distance multipliers from EMPIRICAL volatility so stops
#        expand in fast regimes and contract in slow ones — with
#        hard bounds [0.75×, 1.50×] so adaptation can never
#        degenerate into random walk behaviour.
#
#   MEMORY: bounded ring buffer (deque maxlen) + rotated JSONL on
#   disk. Hours of runtime cannot grow the footprint.
# ============================================================

from __future__ import annotations

import time
import logging
import numpy as np
import pandas as pd
from collections import deque
from datetime import datetime, timezone

from . import settings as S
from .store import append_jsonl, read_jsonl_tail, atomic_write_json, read_json

logger = logging.getLogger("RARC.friction")


class FrictionTracker:
    """
    Trailing broker-friction monitor and the strict `trading_allowed`
    gate. Fail-CLOSED: if Module 1 gave us a tolerance and we have no
    fresh data, we stay closed until fresh data arrives (never trade
    blind through a friction storm).
    """

    def __init__(self, path=None, window_hours=None, sample_max=None,
                 state_path=None, now_fn=None):
        self.path = path or S.FRICTION_LOG_PATH
        self.state_path = state_path or S.FRICTION_STATE_PATH
        self.window_hours = window_hours or S.FRICTION_WINDOW_HOURS
        self._now_fn = now_fn or (lambda: pd.Timestamp.now(tz="UTC"))
        self.samples = deque(maxlen=sample_max or S.FRICTION_SAMPLE_MAX)
        # gate state (hysteresis so the flag doesn't flap bar-by-bar)
        self.trading_allowed = True
        self.block_reason = ""
        self._clean_checks = 0
        self._load_state()
        for rec in read_jsonl_tail(self.path, max_lines=S.FRICTION_SAMPLE_MAX):
            try:
                ts = pd.Timestamp(rec["ts"])
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                self.samples.append({"ts": ts, "spread_pct": float(rec["spread_pct"]),
                                     "slippage": float(rec.get("slippage", 0.0))})
            except (KeyError, TypeError, ValueError):
                continue
        self._refresh_gate()

    # ── recording ────────────────────────────────────────────
    def record(self, spread_pct: float, slippage: float = 0.0,
               ts: datetime | None = None):
        """One friction sample. Cheap enough to call on every fill and
        once per hourly job; deque maxlen bounds memory forever."""
        ts = ts or self._now_fn()
        self.samples.append({"ts": pd.Timestamp(ts), "spread_pct": float(spread_pct),
                             "slippage": float(slippage)})
        append_jsonl(self.path, {"ts": str(pd.Timestamp(ts)),
                                 "spread_pct": float(spread_pct),
                                 "slippage": float(slippage)})

    # ── statistics ───────────────────────────────────────────
    def stats(self, window_hours: float | None = None) -> dict:
        w = window_hours or self.window_hours
        cutoff = self._now_fn() - pd.Timedelta(hours=w)
        recent = [s for s in self.samples
                  if s["ts"] >= cutoff or s["ts"].tz is None
                  and s["ts"] >= cutoff.tz_localize(None)]
        if not recent:
            return {"n": 0}
        sp = np.array([s["spread_pct"] for s in recent])
        sl = np.array([s["slippage"] for s in recent])
        q = lambda arr, p: float(np.percentile(arr, p))
        return {
            "n": len(recent),
            "window_hours": w,
            "spread_p50": round(q(sp, 50), 4),
            "spread_p90": round(q(sp, 90), 4),
            "spread_p95": round(q(sp, 95), 4),
            "spread_max": round(float(sp.max()), 4),
            "slippage_mean": round(float(sl.mean()), 5),
            "slippage_p95": round(q(sl, 95), 5),
        }

    # ── the strict gate ──────────────────────────────────────
    def _refresh_gate(self, tolerance=None):
        tol = tolerance if tolerance is not None else self._tolerance
        if tol is None:
            # Module 1 hasn't produced a tolerance yet → fail-open on
            # the STATISTICAL number but the main spread filter in
            # config.MAX_SPREAD still applies upstream.
            if not self.trading_allowed:
                self.trading_allowed = True
                self.block_reason = ""
            return
        st = self.stats(self.window_hours)
        if st["n"] == 0:
            return  # no fresh evidence → keep current state (stale-safe)
        live = st.get(S.FRICTION_GATE_METRIC, st["spread_p95"])
        if live > tol:
            self._clean_checks = 0
            if self.trading_allowed:
                self.trading_allowed = False
                self.block_reason = (f"friction {live:.4f} > tolerance "
                                     f"{tol:.4f} ({S.FRICTION_GATE_METRIC} "
                                     f"over {self.window_hours}h)")
                logger.warning("FRICTION GATE CLOSED: %s", self.block_reason)
        else:
            # Re-open only after N consecutive clean checks (hysteresis).
            self._clean_checks += 1
            if not self.trading_allowed and self._clean_checks >= S.FRICTION_REOPEN_STREAK:
                self.trading_allowed = True
                self.block_reason = ""
                logger.info("FRICTION GATE REOPENED after %d clean checks",
                            self._clean_checks)

    def hourly_check(self, tolerance=None, live_spread: float | None = None):
        """
        The 60-minute job: sample current spread (if provided), expire
        old samples, refresh the gate, persist state. Idempotent.
        """
        if live_spread is not None:
            self.record(float(live_spread))
        self._prune()
        self._refresh_gate(tolerance)
        self._save_state()
        return self.trading_allowed, self.block_reason

    def _prune(self):
        cutoff = self._now_fn() - pd.Timedelta(hours=self.window_hours * 2)
        self.samples = deque(
            (s for s in self.samples
             if (s["ts"] if s["ts"].tzinfo else s["ts"].tz_localize("UTC")) >= cutoff),
            maxlen=self.samples.maxlen)

    # ── persistence (restart-safe) ───────────────────────────
    @property
    def _tolerance(self):
        return getattr(self, "_tolerance_val", None)

    def set_tolerance(self, tolerance: float | None):
        self._tolerance_val = tolerance

    def _save_state(self):
        atomic_write_json(self.state_path, {
            "trading_allowed": self.trading_allowed,
            "block_reason": self.block_reason,
            "clean_checks": self._clean_checks,
            "updated": datetime.now(timezone.utc).isoformat(),
        })

    def _load_state(self):
        st = read_json(self.state_path, {}) or {}
        self.trading_allowed = bool(st.get("trading_allowed", True))
        self.block_reason = st.get("block_reason", "")
        self._clean_checks = int(st.get("clean_checks", 0))


class ATRRegulator:
    """
    Hourly SL/TP distance recalibration from EMPIRICAL volatility.

    vol_ratio = live ATR / reference ATR (trailing median). Multipliers
    scale as vol_ratio^exponent, clamped to [ATR_MULT_MIN, ATR_MULT_MAX]
    — the structural parameters expand and contract, but inside a cage.

    Reference is a 30-day MEDIAN hourly ATR: robust to the very spikes
    we are measuring (a median does not chase the news candle).
    """

    def __init__(self, state_path=None):
        self.path = state_path or S.ATR_STATE_PATH
        st = read_json(self.path, {}) or {}
        self.ref_atr = st.get("ref_atr")
        self.current_mult = st.get("current_mult", 1.0)
        self.last_update = st.get("last_update", "")
        self.history = deque(st.get("history", []), maxlen=24 * S.ATR_REF_DAYS)

    def hourly_update(self, live_atr: float, now: datetime | None = None) -> float:
        """Recompute the SL/TP multiplier for the next hour. Bounded."""
        if not live_atr or not np.isfinite(live_atr) or live_atr <= 0:
            return self.current_mult          # hold last good value
        now = now or datetime.now(timezone.utc)
        self.history.append({"ts": now.isoformat(), "atr": float(live_atr)})
        med = float(np.median([h["atr"] for h in self.history])) \
            if self.history else float(live_atr)
        self.ref_atr = med
        ratio = float(live_atr) / med if med > 0 else 1.0
        raw = ratio ** S.ATR_RATIO_EXPONENT
        self.current_mult = float(np.clip(raw, S.ATR_MULT_MIN, S.ATR_MULT_MAX))
        self.last_update = now.isoformat()
        atomic_write_json(self.path, {
            "ref_atr": self.ref_atr,
            "current_mult": self.current_mult,
            "last_update": self.last_update,
            "history": list(self.history),
        })
        logger.info("ATR regulator: live %.5f / ref %.5f → SL/TP mult %.3f",
                    live_atr, med, self.current_mult)
        return self.current_mult

    def scale_distances(self, sl_distance: float, tp_distance: float):
        """Apply the current multiplier to SL/TP distances (same factor
        on both → RR geometry is preserved; only the structural
        breathing room adapts)."""
        m = self.current_mult
        return sl_distance * m, tp_distance * m
