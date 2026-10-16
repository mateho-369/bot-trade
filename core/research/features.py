"""Vectorised causal features for P2 engines.

Every column at bar i uses bars <= i only (rolling/shifted windows), so
signals built on this frame cannot look ahead. Input/output: canonical
bars (time column, UTC). Higher-timeframe trend is aligned by CLOSE
time: an H1 trend value is used only after that H1 bar has closed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.smc_engine import calc_atr, calc_ema, calc_rsi

TF_MINUTES = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30,
              "H1": 60, "H4": 240, "D1": 1440}


def _indexed(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    if "time" in df.columns:
        df = df.set_index(pd.to_datetime(df["time"], utc=True))
        df = df.drop(columns=["time"])
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def compute_features(bars: pd.DataFrame, timeframe: str,
                     trend_bars: pd.DataFrame | None = None) -> pd.DataFrame:
    df = _indexed(bars)
    out = df.copy()
    close = df["close"]
    out["atr"] = calc_atr(df, 14)
    out["ema20"] = calc_ema(close, 20)
    out["ema50"] = calc_ema(close, 50)
    out["ema200"] = calc_ema(close, 200)
    out["rsi"] = calc_rsi(close, 14)
    out["ema50_slope_atr"] = (out["ema50"] - out["ema50"].shift(10)) / out["atr"]
    # ATR percentile within trailing 200 bars (causal rank)
    out["atr_pctile"] = out["atr"].rolling(200, min_periods=50).rank(pct=True)
    # Bollinger 20/2
    mid = close.rolling(20).mean()
    sd = close.rolling(20).std(ddof=0)
    out["bb_mid"] = mid
    out["bb_up"] = mid + 2 * sd
    out["bb_low"] = mid - 2 * sd
    # daily-anchored VWAP (typical price, tick volume)
    typical = (df["high"] + df["low"] + close) / 3.0
    day = df.index.date
    pv = (typical * df["tick_volume"]).groupby(day).cumsum()
    vv = df["tick_volume"].groupby(day).cumsum()
    out["vwap"] = pv / vv.replace(0, np.nan)
    # prior extremes (previous bar's rolling values: breakout reference)
    out["hh20_prev"] = df["high"].rolling(20).max().shift(1)
    out["ll20_prev"] = df["low"].rolling(20).min().shift(1)
    out["ll5_prev"] = df["low"].rolling(5).min().shift(1)
    out["hh5_prev"] = df["high"].rolling(5).max().shift(1)
    out["hour"] = df.index.hour + df.index.minute / 60.0
    out["hour_utc"] = df.index.hour.astype(int)
    out["date"] = day
    # H1 trend aligned by close time (no look-ahead)
    out["h1_trend"] = 0
    if trend_bars is not None and len(trend_bars):
        tb = _indexed(trend_bars)
        ema50 = calc_ema(tb["close"], 50)
        trend = np.where(tb["close"] > ema50, 1,
                         np.where(tb["close"] < ema50, -1, 0))
        t_close = tb.index + pd.Timedelta(hours=1)
        tf_min = TF_MINUTES.get(timeframe, 1)
        sig_close = df.index + pd.Timedelta(minutes=tf_min)
        pos = np.searchsorted(t_close.values, sig_close.values, side="right") - 1
        vals = np.zeros(len(df), dtype=int)
        ok = pos >= 0
        vals[ok] = trend[pos[ok]]
        out["h1_trend"] = vals
    return out.reset_index().rename(columns={"index": "time"})
