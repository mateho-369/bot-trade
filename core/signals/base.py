"""Candidate-signal contract shared by every L1 engine and the backtester."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class CandidateSignal:
    engine: str                 # e.g. "SR_H1"
    timeframe: str              # execution timeframe, e.g. "H1"
    direction: int              # +1 BUY, -1 SELL
    bar_index: int              # index of the CLOSED signal bar in its frame
    signal_time: pd.Timestamp   # close time of the signal bar (UTC)
    stop_price: float           # absolute stop price (structure/ATR based)
    target_R: float = 2.0       # take-profit as multiple of |entry-stop|
    time_stop_bars: Optional[int] = None  # vertical barrier, in bars
    atr: float = 0.0
    level: Optional[float] = None
    features: dict = field(default_factory=dict)
