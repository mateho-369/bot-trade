# ============================================================
#   rarc — Regime Adaptation & Risk Correction Engine
#
#   A mathematically bounded safety layer for the BOSS SMC bot.
#   It does NOT predict prices and NEVER opens trades. It can only:
#     · cap Risk_Per_Trade (Module 1 + Module 4)
#     · block entries (friction, blindspots, smart gate)
#     · re-scale SL/TP distance parameters (Module 2)
# ============================================================

from .engine import RarcEngine, GateResult
from .boundaries import (BlacklistMatrix, build_matrix, load_matrix,
                         save_matrix, load_trades_csv)
from .friction import FrictionTracker, ATRRegulator
from .walkforward import WalkForwardFilter, build_dataset
from .cooldown import CooldownMatrix
from . import settings

__all__ = [
    "RarcEngine", "GateResult",
    "BlacklistMatrix", "build_matrix", "load_matrix", "save_matrix",
    "load_trades_csv",
    "FrictionTracker", "ATRRegulator",
    "WalkForwardFilter", "build_dataset",
    "CooldownMatrix",
    "settings",
]
