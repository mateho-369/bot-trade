"""Deterministic synthetic XAUUSD-like bars for OFFLINE tests only.

Seeded random walk with intraday volatility shape and occasional level
breakouts. This data has NO market edge and must never be used to claim
performance; it exists so the P1 pipeline (data -> signals -> backtest
-> report) can be tested end-to-end without an MT5 terminal.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.data.history import ensure_utc


def generate_synthetic_m1(days: int = 10, seed: int = 7,
                          start_price: float = 2400.0,
                          start: str = "2026-09-28 00:00") -> pd.DataFrame:
    """`days` of synthetic M1 bars (24h x 60m), canonical columns."""
    rng = np.random.default_rng(seed)
    n = int(days) * 24 * 60
    times = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    hour = times.hour.to_numpy()
    # gold sleeps in Asia, moves at London/NY opens
    vol = 0.35 + 0.9 * np.exp(-0.5 * ((hour - 13.5) / 3.0) ** 2) \
        + 0.5 * np.exp(-0.5 * ((hour - 8.0) / 1.5) ** 2)
    vol = vol * 0.55  # ~0.3-0.8 price units per M1 bar
    drift = 0.00002 * np.sin(np.arange(n) / 900.0)  # slow wave, no free edge
    rets = rng.normal(0.0, 1.0, n) * vol + drift
    close = start_price + np.cumsum(rets)
    spread_move = np.abs(rets) * rng.uniform(0.4, 1.1, n)
    open_ = np.empty(n); open_[0] = start_price; open_[1:] = close[:-1]
    high = np.maximum(open_, close) + spread_move * 0.6
    low = np.minimum(open_, close) - spread_move * 0.6
    spread_pts = np.where((hour >= 7) & (hour < 21), 24.0, 38.0)  # $0.24/$0.38
    df = pd.DataFrame({
        "time": times, "open": open_, "high": high, "low": low, "close": close,
        "tick_volume": rng.integers(20, 400, n),
        "spread_points": spread_pts,
        "real_volume": 0,
    })
    return ensure_utc(df)
