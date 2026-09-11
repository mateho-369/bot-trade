# ============================================================
#   rarc/engine.py
#   RarcEngine — the single façade main.py talks to.
#
#   WIRING CONTRACT (see RARC_ARCHITECTURE.md for the full map):
#     engine.pre_trade_gate(...)    → call in execute_trade() BEFORE
#                                     any order. Returns allow/deny,
#                                     the effective Risk_Per_Trade and
#                                     the ATR-regulated SL/TP factors.
#     engine.on_trade_closed(...)   → call when a position resolves.
#     engine.on_fill(...)           → call after each fill with the
#                                     requested vs actual price.
#     engine.run_due_maintenance()  → call once per main-loop tick;
#                                     internally runs the hourly
#                                     friction/ATR jobs and the
#                                     00:00 UTC walk-forward retrain.
#
#   GATE ORDER (cheap → expensive, hard → statistical):
#     1. Cooldown hard gate          (fail-CLOSED)
#     2. Friction gate               (fail-CLOSED)
#     3. Blacklist matrix blindspots (fail-OPEN, structural)
#     4. Walk-forward smart gate     (fail-OPEN, statistical, block-only)
#   Each layer is independent; a failure in one never disables the
#   ones above it.
# ============================================================

from __future__ import annotations

import gc
import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import settings as S
from .boundaries import (BlacklistMatrix, build_matrix, load_matrix,
                         save_matrix, load_trades_csv)
from .friction import FrictionTracker, ATRRegulator
from .walkforward import WalkForwardFilter
from .cooldown import CooldownMatrix
from .store import FileLock

logger = logging.getLogger("RARC")


@dataclass
class GateResult:
    allowed: bool
    reason: str = ""
    risk_pct: float | None = None        # effective Risk_Per_Trade
    sl_mult: float = 1.0                 # ATR-regulated distance factors
    tp_mult: float = 1.0
    p_loss: float = -1.0                 # smart-gate failure probability
    tags: list = field(default_factory=list)   # telemetry labels


class RarcEngine:
    def __init__(self, symbols=None, logger_=None):
        if logger_ is not None:
            global logger
            logger = logger_
        S.ensure_dirs()
        self.symbols = list(symbols or [])
        self.matrix: BlacklistMatrix | None = load_matrix()
        self.friction = FrictionTracker()
        self.atr = ATRRegulator()
        self.wf = WalkForwardFilter()
        self.cooldown = CooldownMatrix()
        if self.matrix:
            self.friction.set_tolerance(self.matrix.max_spread_tolerance)
        self._last_hour_job = None      # datetime of last hourly run
        self._last_daily_job = None     # UTC date str of last daily run
        self._latest_atr = {}           # symbol → latest ATR (cheap feed)
        logger.info("RARC engine ready | matrix=%s | wf_model=%s | "
                    "cooldown_tier=%d | friction_gate=%s",
                    "loaded" if self.matrix else "MISSING (run Module 1)",
                    "ready" if self.wf.model else "none yet",
                    self.cooldown.tier,
                    "OPEN" if self.friction.trading_allowed else "CLOSED")

    # ══════════════════════════════════════════════════════════
    #  ENTRY GATE — the only API the trading path needs
    # ══════════════════════════════════════════════════════════
    def pre_trade_gate(self, symbol: str, spread_pct: float,
                       hour_utc: int, day_of_week: int,
                       atr_pct: float = 0.0, trend_strength: float = 0.0,
                       session_active: int = 1, now: datetime | None = None
                       ) -> GateResult:
        now = now or datetime.now(timezone.utc)

        # ── 1. Cooldown hard gate (fail-closed) ──────────────
        ok, why = self.cooldown.trading_allowed(now)
        if not ok:
            return GateResult(False, f"cooldown: {why}", tags=["cooldown"])

        # ── 2. Friction gate (fail-closed) ───────────────────
        if not self.friction.trading_allowed:
            return GateResult(False, f"friction: {self.friction.block_reason}",
                              tags=["friction"])

        # ── 3. Blacklist matrix blindspots (fail-open) ───────
        m = self.matrix
        if m:
            if m.hour_toxic(int(hour_utc)):
                return GateResult(False, f"toxic hour {hour_utc} UTC "
                                         f"(historical PF < 1.0)",
                                  tags=["blindspot:hour"])
            if m.day_toxic(int(day_of_week)):
                return GateResult(False, f"toxic weekday (historical PF < 1.0)",
                                  tags=["blindspot:day"])
            if not m.spread_ok(float(spread_pct)):
                return GateResult(False,
                                  f"spread {spread_pct:.4f} > tolerance "
                                  f"{m.max_spread_tolerance:.4f}",
                                  tags=["blindspot:spread"])

        # ── 4. Walk-forward smart gate (fail-open, block-only) ─
        dec = self.wf.gate_signal(hour_utc, day_of_week, spread_pct,
                                  atr_pct, trend_strength, session_active,
                                  self.cooldown.streak(now), now)
        if dec.block:
            return GateResult(False, dec.reason, p_loss=dec.p_loss,
                              tags=["smart_gate", f"p_loss={dec.p_loss:.2f}"])

        # ── ALL CLEAR: compose the bounded sizing + distances ─
        cap = m.capped_risk_pct if m else None
        risk_pct = self.cooldown.effective_risk_pct(S.BASELINE_RISK_PCT, cap, now)
        return GateResult(
            True, "pass", risk_pct=risk_pct,
            sl_mult=self.atr.current_mult, tp_mult=self.atr.current_mult,
            p_loss=dec.p_loss,
            tags=[f"tier{self.cooldown.tier}", f"risk={risk_pct:.2f}%"])

    def on_blocked_signal(self, symbol, direction, entry, sl, tp,
                          p_loss=-1, now=None):
        """Shadow-log a Smart-Gate-Bypassed signal for live self-audit."""
        self.wf.record_blocked(symbol, direction, entry, sl, tp, now, p_loss)

    def note_atr(self, symbol: str, atr: float):
        """Cheap feed from the main loop: latest observed ATR per symbol.
        The hourly job uses the primary symbol's value for the regulator."""
        self._latest_atr[symbol] = float(atr)

    # ══════════════════════════════════════════════════════════
    #  FEEDBACK HOOKS
    # ══════════════════════════════════════════════════════════
    def on_fill(self, symbol: str, requested_price: float, filled_price: float,
                spread_pct: float, atr: float = 0.0):
        """Record actual friction: slippage = |fill − requested|."""
        slip = abs(filled_price - requested_price) if requested_price else 0.0
        self.friction.record(spread_pct, slippage=slip)

    def on_trade_closed(self, symbol: str, ticket, profit: float,
                        now: datetime | None = None) -> dict:
        """Update the cooldown matrix. Returns sizing context + any
        state transitions worth alerting on."""
        now = now or datetime.now(timezone.utc)
        was_cooldown = self.cooldown.in_cooldown(now)
        was_tier = self.cooldown.tier
        self.cooldown.on_trade_closed(win=profit > 0, now=now)
        return {
            "symbol": symbol,
            "profit": profit,
            "tier": self.cooldown.tier,
            "risk_mult": self.cooldown.risk_mult(),
            "in_cooldown": self.cooldown.in_cooldown(now),
            "streak_frame": self.cooldown.streak(now),
            "cooldown_started": (not was_cooldown
                                 and self.cooldown.in_cooldown(now)),
            "tier_changed": self.cooldown.tier != was_tier,
        }

    # ══════════════════════════════════════════════════════════
    #  SCHEDULER — hourly + daily maintenance, inline-safe
    # ══════════════════════════════════════════════════════════
    def run_due_maintenance(self, now: datetime | None = None,
                            live_spread: float | None = None,
                            live_atr: float | None = None) -> dict:
        """
        Call once per main-loop tick (1s). Internally no-ops unless a
        job is due: hourly friction/ATR refresh, 00:00 UTC retrain,
        matrix re-extract when stale. Never raises; hard time-budgeted
        so the 1s loop never stalls.
        """
        now = now or datetime.now(timezone.utc)
        out = {}
        try:
            # ── hourly job ───────────────────────────────────
            if self._hour_due(now):
                self._last_hour_job = now.replace(minute=0, second=0, microsecond=0)
                if live_spread is None and self.symbols:
                    live_spread = None   # caller may wire a provider
                ok, why = self.friction.hourly_check(None, live_spread)
                atr_val = live_atr or self._latest_atr.get(
                    self.symbols[0] if self.symbols else "")
                if atr_val:
                    self.atr.hourly_update(atr_val, now)
                out["hourly"] = {"friction_open": ok, "reason": why,
                                 "stats": self.friction.stats(),
                                 "atr_mult": self.atr.current_mult}

            # ── daily 00:00 UTC job ──────────────────────────
            today = now.strftime("%Y-%m-%d")
            if now.hour == S.WF_RETRAIN_HOUR_UTC and self._last_daily_job != today:
                with FileLock(S.LOCK_PATH, timeout=S.LOCK_TIMEOUT_SECONDS):
                    if self._last_daily_job != today:   # re-check under lock
                        out["daily"] = self._daily_job(now)

            # ── matrix staleness ─────────────────────────────
            if self.matrix and self._matrix_stale(now):
                out["matrix"] = "STALE — rebuild from backtest recommended"

            # ── shadow-trade resolution (cheap, internal) ────
            if self.wf.shadow:
                pending = sum(1 for r in self.wf.shadow if r["outcome"] is None)
                if pending and now.minute % 15 == 0 and now.second < S.MAINTENANCE_EVERY_SECONDS:
                    n = self.wf.resolve_shadow_trades(self._shadow_lookup)
                    if n:
                        out["shadow_resolved"] = n
        except Exception as e:                     # never kill the loop
            logger.exception("maintenance error: %s", e)
        return out

    def _daily_job(self, now: datetime) -> dict:
        """Daily retrain under the file lock. Loads the rolling window
        from the bot's trade log, trains, validates, maybe deploys.
        Memory-safe: window is bounded, DataFrames released after."""
        log_path = None
        try:
            import config as _cfg
            log_path = getattr(_cfg, "TRADE_LOG_PATH", None)
        except Exception:
            pass
        diag = {"date": now.isoformat()}
        try:
            df = None
            import os
            if log_path and os.path.exists(log_path):
                df = pd.read_csv(log_path)
            if df is not None and len(df):
                result = self.wf.daily_retrain(df, now)
                diag.update(result)
            else:
                diag["skipped"] = "no trade log available"
        except Exception as e:
            diag["error"] = str(e)
            logger.exception("daily retrain failed: %s", e)
        finally:
            df = None
            gc.collect()          # explicit release after the heavy path
        return diag

    def _hour_due(self, now: datetime) -> bool:
        if self._last_hour_job is None:
            return True
        return (now - self._last_hour_job).total_seconds() >= 3600

    def _matrix_stale(self, now: datetime) -> bool:
        try:
            ts = pd.Timestamp(self.matrix.computed_at)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            return (now - ts) > pd.Timedelta(hours=S.MATRIX_MAX_AGE_HOURS)
        except Exception:
            return True

    def _shadow_lookup(self, symbol, ts_iso, direction, sl, tp):
        """
        Default shadow-trade resolver using MT5-free candle replay is
        not possible inside the engine (needs broker data). The bot
        should inject its own via set_shadow_lookup() with
        get_candles(). Returns None → stays pending.
        """
        return None

    def set_shadow_lookup(self, fn):
        """Inject the bot's candle-replay resolver for shadow trades."""
        self._shadow_lookup = fn

    # ══════════════════════════════════════════════════════════
    #  OFFLINE TOOLS (run from a script / REPL, not in-loop)
    # ══════════════════════════════════════════════════════════
    def rebuild_matrix_from_backtest(self, trades_csv: str,
                                     symbol: str = "ALL") -> dict:
        """Full Module 1 pass over multi-year backtest trades."""
        trades = load_trades_csv(trades_csv)
        if len(trades) == 0:
            raise ValueError(f"no valid trades parsed from {trades_csv}")
        matrix = build_matrix(trades, symbol=symbol)
        with FileLock(S.LOCK_PATH, timeout=S.LOCK_TIMEOUT_SECONDS):
            save_matrix(matrix)
        self.matrix = load_matrix()
        if self.matrix:
            self.friction.set_tolerance(self.matrix.max_spread_tolerance)
        logger.info("risk_blacklist_matrix rebuilt: risk capped at %.2f%%, "
                    "%d toxic hours, %d toxic days, spread tolerance %.4f",
                    self.matrix.capped_risk_pct or -1,
                    len(self.matrix.toxic_hours),
                    len(self.matrix.toxic_days),
                    self.matrix.max_spread_tolerance or -1)
        return matrix
