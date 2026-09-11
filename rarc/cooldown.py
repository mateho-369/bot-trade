# ============================================================
#   rarc/cooldown.py  —  MODULE 4
#   THE AUTOMATED SELF-CORRECTION / COOLDOWN MATRIX
#
#   JOB:
#     Recognize sequential LIVE failures and adapt position sizing
#     so equity cannot be destroyed during catastrophic regime
#     changes.
#
#   MECHANICS:
#     - Streak counting is LOCALIZED to a 12-hour rolling frame:
#       losses older than 12h fall out of the window, so yesterday's
#       bad day cannot suppress today's fresh regime forever — but an
#       active COOLDOWN keeps its full timer regardless of frame.
#     - Tier ladder (bounded cage — see settings.COOLDOWN_TIERS):
#         0-1 losses → 1.00 × baseline
#         2 losses   → 0.50 × baseline
#         3 losses   → 0.25 × baseline (defensive tier)
#         4 losses   → 0.25 × baseline + 12h operational cooldown
#     - RECOVERY HYSTERESIS: after a win or cooldown expiry the bot
#       climbs back ONE tier at a time and must land
#       RECOVERY_TRADES_PER_TIER resolved trades per tier. This kills
#       the flapping oscillation (win → full risk → loss → panic)
#       that naive streak logic produces.
#     - Everything persists with timestamps: a 12h cooldown survives
#       a bot restart with its remaining time intact.
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from datetime import datetime, timezone

from . import settings as S
from .store import atomic_write_json, read_json

logger = logging.getLogger("RARC.cooldown")


class CooldownMatrix:
    """Live consecutive-loss tracker + tiered risk scaler."""

    def __init__(self, state_path=None, now: datetime | None = None):
        self.path = state_path or S.COOLDOWN_STATE_PATH
        now = now or datetime.now(timezone.utc)
        st = read_json(self.path, {}) or {}
        # bounded deque of ISO timestamps of losses inside the frame
        self.loss_times = [t for t in st.get("loss_times", [])
                           if self._in_frame(t, now)]
        self.wins_since_rebuild = int(st.get("wins_since_rebuild", 0))
        self.recovery_progress = int(st.get("recovery_progress", 0))
        self.tier = int(st.get("tier", 0))
        self.cooldown_until = st.get("cooldown_until", "")
        self.last_event = st.get("last_event", "")

    # ── core state transitions ───────────────────────────────
    def on_trade_closed(self, win: bool, now: datetime | None = None):
        """Feed every RESOLVED live trade through here."""
        now = now or datetime.now(timezone.utc)
        self._prune_frame(now)

        if self._in_cooldown(now):
            # Cooldown overrides streak logic; trade was blocked anyway,
            # but if one slipped through we still record it.
            if not win:
                self.loss_times.append(now.isoformat())
            self._save()
            return

        if win:
            self.loss_times = []          # a win breaks the loss run…
            self.wins_since_rebuild += 1  # …but NOT the recovery hysteresis
            if self.tier > 0:
                self.recovery_progress += 1
                if self.recovery_progress >= S.RECOVERY_TRADES_PER_TIER:
                    self.tier -= 1                      # ONE tier at a time
                    self.recovery_progress = 0
                    logger.info("COOLDOWN MATRIX: recovered to tier %d "
                                "(risk ×%.2f)", self.tier, self.risk_mult())
            self.last_event = f"win@{now.isoformat()}"
        else:
            self.loss_times.append(now.isoformat())
            self.wins_since_rebuild = 0
            self.recovery_progress = 0
            streak = self.streak(now)
            new_tier = self._tier_for_streak(streak)
            if new_tier != self.tier:
                self.tier = new_tier
                logger.warning("COOLDOWN MATRIX: %d consecutive losses in "
                               "%dh frame → tier %d (risk ×%.2f, action %s)",
                               streak, S.COOLDOWN_FRAME_HOURS, self.tier,
                               self.risk_mult(), self.action())
            # Cooldown trigger: tier whose action == "cooldown"
            if self.action() == "cooldown":
                self.cooldown_until = (now + pd.Timedelta(
                    hours=S.COOLDOWN_DURATION_HOURS)).isoformat()
                self.last_event = f"cooldown@{now.isoformat()}"
                logger.warning("OPERATIONAL COOLDOWN until %s "
                               "(%d losses in %dh frame)",
                               self.cooldown_until, streak,
                               S.COOLDOWN_FRAME_HOURS)
        self._save()

    # ── queries the engine uses ──────────────────────────────
    def streak(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        self._prune_frame(now)
        # consecutive = trailing run of losses with no win gap. We track
        # only loss timestamps in the frame; a win resets the run, which
        # on_trade_closed encodes by clearing recovery counters and (via
        # tier drop) effectively restarting the run count. For exactness
        # we define frame streak = count of losses since the last win.
        return len(self.loss_times)

    def in_cooldown(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self._in_cooldown(now)

    def risk_mult(self) -> float:
        """Bounded multiplier from the tier ladder. Always in (0, 1]."""
        return float(S.COOLDOWN_TIERS.get(self.tier, {"risk_mult": 1.0})["risk_mult"])

    def action(self) -> str:
        return str(S.COOLDOWN_TIERS.get(self.tier, {"action": "normal"})["action"])

    def effective_risk_pct(self, baseline_pct: float,
                           hard_cap_pct: float | None = None,
                           now: datetime | None = None) -> float:
        """
        Final Risk_Per_Trade: baseline × tier multiplier, floored by
        RISK_PCT_FLOOR and never above the Module 1 hard cap when one
        is supplied. This is the ONLY sizing authority in Module 4.
        """
        base = min(baseline_pct, hard_cap_pct) if hard_cap_pct else baseline_pct
        mult = self.risk_mult()
        v = base * mult
        # floor: never below floor % of BASELINE scale — if the floor
        # exceeds the capped base, keep the capped base (never size UP).
        floor_pct = S.RISK_PCT_FLOOR
        v = max(min(v, base), min(floor_pct, base))
        return round(v, 4)

    def trading_allowed(self, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        if self._in_cooldown(now):
            remain = (datetime.fromisoformat(self.cooldown_until) - now)
            return False, (f"operational cooldown active "
                           f"({remain.total_seconds()/3600:.1f}h remaining)")
        return True, ""

    # ── internals ────────────────────────────────────────────
    def _in_cooldown(self, now: datetime) -> bool:
        if not self.cooldown_until:
            return False
        if datetime.fromisoformat(self.cooldown_until) > now:
            return True
        # Cooldown expired → start recovery exactly one tier below the
        # breach tier (hysteresis: no full-risk instant re-arm).
        self.cooldown_until = ""
        if self.tier > 0:
            self.tier = max(0, self.tier - 1)
            self.recovery_progress = 0
        self.loss_times = []           # fresh frame after a full stop
        logger.info("Cooldown expired → resuming at tier %d (×%.2f)",
                    self.tier, self.risk_mult())
        return False

    def _tier_for_streak(self, streak: int) -> int:
        # Highest configured tier whose threshold is reached.
        tiers = sorted(S.COOLDOWN_TIERS.keys())
        tier = 0
        cumulative = 0
        # tiers are keyed by absolute streak thresholds in settings;
        # find the largest key <= streak
        for k in tiers:
            if streak >= k:
                tier = k
        return tier

    def _prune_frame(self, now: datetime):
        cutoff = pd.Timestamp(now) - pd.Timedelta(hours=S.COOLDOWN_FRAME_HOURS)
        self.loss_times = [t for t in self.loss_times if self._in_frame(t, now)]

    @staticmethod
    def _in_frame(t_iso: str, now: datetime) -> bool:
        try:
            t = pd.Timestamp(t_iso)
            if t.tzinfo is None:
                t = t.tz_localize("UTC")
            return t >= pd.Timestamp(now) - pd.Timedelta(hours=S.COOLDOWN_FRAME_HOURS)
        except (ValueError, TypeError):
            return False

    def _save(self):
        atomic_write_json(self.path, {
            "loss_times": self.loss_times,
            "tier": self.tier,
            "recovery_progress": self.recovery_progress,
            "wins_since_rebuild": self.wins_since_rebuild,
            "cooldown_until": self.cooldown_until,
            "last_event": self.last_event,
        })
