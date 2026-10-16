"""P2 pre-registered combo grid (mirror of research/P2_plan.md v1).

Every (engine, timeframe, combo) backtest = 1 trial. The trial counter
is reported on the leaderboard and used for the Deflated Sharpe Ratio.
Changing this grid after seeing real results requires a new plan
version in research/P2_plan.md; the counter never resets.
"""
from __future__ import annotations

from dataclasses import dataclass

SESSIONS = {"LIQUID_07_20": (7, 20), "OVERLAP_13_17": (13, 17),
            "MORNING_00_12": (0, 13)}   # all exclude 21:00-23:59 rollover

HOLD_MINUTES = {"ORB": 180, "PULLBACK": 360, "SMC": 360, "RANGE": 360,
                "SQUEEZE": 480}
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}

# engine -> (primary variant values, has_h1_axis, has_target_axis)
ENGINE_SPECS = {
    "ORB": ([30, 60], True, True),
    "PULLBACK": ([False, True], False, True),   # variant = RSI gate
    "SMC": ([False, True], True, True),          # variant = bos_only
    "RANGE": ([0.15, 0.25], True, False),        # variant = regime slope
    "SQUEEZE": ([10, 20], True, True),           # variant = ATR pctile
}


@dataclass(frozen=True)
class Combo:
    engine: str
    combo_id: str
    variant: object
    session_name: str
    session_range: tuple
    h1_trend: bool
    target_R: float | None
    time_stop_bars: int | None = None

    def with_time_stop(self, timeframe: str) -> "Combo":
        mins = HOLD_MINUTES[self.engine]
        bars = max(3, mins // TF_MINUTES[timeframe])
        return Combo(self.engine, self.combo_id, self.variant,
                     self.session_name, self.session_range, self.h1_trend,
                     self.target_R, bars)


def combos_for(engine: str, timeframe: str) -> list:
    variants, has_h1, has_target = ENGINE_SPECS[engine]
    out = []
    for v in variants:
        for sname, srange in SESSIONS.items():
            for h1 in ((False, True) if has_h1 else (False,)):
                for tgt in ((1.5, 2.0) if has_target else (None,)):
                    cid = (f"{engine}|v={v}|s={sname}|h1={int(h1)}|"
                           f"tgt={tgt}")
                    out.append(Combo(engine, cid, v, sname, srange, h1,
                                     tgt).with_time_stop(timeframe))
    return out


def trial_count(engines, timeframes) -> int:
    """Total (engine, TF, combo) trials the grid would run."""
    return sum(len(combos_for(e, tf)) for e in engines for tf in timeframes)
