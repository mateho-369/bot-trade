"""SR_H1 signal generation for backtesting (v3 L1 candidate, ENGINE C port).

This is a faithful, bar-by-bar port of the logic in core/sr_engine.py
(the duel winner, documented PF 1.15 on real 2024-2025 data in
REAL_WORLD_TEST.md), restructured as a pure function
(H1 bars, H4 bars) -> list[CandidateSignal] so ONE shared backtester can
evaluate it next to every other candidate.

Rules (identical to core/sr_engine.py unless noted):
  * Levels: previous-day high/low + confirmed swing pivots (len 5),
    pivot must be >= 6h stale, within 10 days, within 1.0xATR of the
    previous close, clustered at 0.25xATR (median of cluster).
  * H1 close crosses a level + momentum (body >= 0.5xATR, close in the
    favourable 40% of the bar) + H4 trend (close vs EMA50 of the last
    CLOSED H4 bar -- no look-ahead).
  * Session 07:00-20:00 UTC, skip 12:30-14:30 UTC (news window).
  * Each level consumed once per day per direction.
  * Stop = level -/+ 0.25xATR; target = 2R (applied by the backtester
    from the actual entry, exactly like main.py's SR execution path,
    which also rejects |entry-stop| outside [0.15, 2.5] x ATR -- the
    backtester applies the same rejection at entry).

All thresholds default to config.py's SR_* values; pass `params` to
override in research (every override counts as a trial, Hard Rule #4).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

import config
from core.signals.base import CandidateSignal
from core.smc_engine import calc_atr, calc_ema, calc_rsi

DEFAULTS = {
    "swing_len": int(getattr(config, "SR_SWING_LEN", 5)),
    "atr_len": int(getattr(config, "SR_ATR_LEN", 14)),
    "h4_ema_len": int(getattr(config, "SR_H4_EMA_LEN", 50)),
    "level_atr_range": float(getattr(config, "SR_LEVEL_ATR_RANGE", 1.0)),
    "cluster_atr": float(getattr(config, "SR_CLUSTER_ATR", 0.25)),
    "momentum_body": float(getattr(config, "SR_MOMENTUM_BODY", 0.5)),
    "close_pos": float(getattr(config, "SR_CLOSE_POS", 0.6)),
    "sl_atr": float(getattr(config, "SR_SL_ATR", 0.25)),
    "rr": float(getattr(config, "SR_RR", 2.0)),
    "lookback_days": float(getattr(config, "SR_LOOKBACK_DAYS", 10)),
    "news_start": float(getattr(config, "SR_NEWS_START", 12.5)),
    "news_end": float(getattr(config, "SR_NEWS_END", 14.5)),
    "session_start": float(getattr(config, "SESSION_START", 7)),
    "session_end": float(getattr(config, "SESSION_END", 20)),
    "min_sl_atr": float(getattr(config, "SR_MIN_SL_ATR", 0.15)),
    "max_sl_atr": float(getattr(config, "SR_MAX_SL_ATR", 2.5)),
}


def _pivot_flags(values: np.ndarray, side: str, length: int) -> np.ndarray:
    """Same pivot definition as core/sr_engine._pivot_flags."""
    n = len(values)
    out = np.zeros(n, dtype=bool)
    for i in range(length, n - length):
        if side == "high":
            if values[i] > values[i-length:i].max() and values[i] > values[i+1:i+length+1].max():
                out[i] = True
        else:
            if values[i] < values[i-length:i].min() and values[i] < values[i+1:i+length+1].min():
                out[i] = True
    return out


def _as_indexed(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    if "time" in df.columns:
        df = df.set_index(pd.to_datetime(df["time"], utc=True))
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def generate_sr_signals(h1: pd.DataFrame, h4: pd.DataFrame,
                        params: Optional[dict] = None,
                        time_stop_bars: Optional[int] = None) -> list:
    """Return CandidateSignal list. `h1`/`h4`: canonical bars (time col)."""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    h1 = _as_indexed(h1)
    h4 = _as_indexed(h4)
    if len(h1) < 60 or len(h4) < p["h4_ema_len"] + 5:
        return []

    atr_s = calc_atr(h1, p["atr_len"])
    rsi_s = calc_rsi(h1["close"], 14)
    ema200 = calc_ema(h1["close"], int(getattr(config, "EMA_LEN", 200)))
    ema4 = calc_ema(h4["close"], p["h4_ema_len"])
    # last CLOSED H4 bar for each H1 bar: H4 bar start + 4h <= H1 bar close
    h4_close_times = h4.index + pd.Timedelta(hours=4)
    h1_close_times = h1.index + pd.Timedelta(hours=1)
    h4_pos = np.searchsorted(h4_close_times.values, h1_close_times.values,
                             side="right") - 1

    highs = h1["high"].to_numpy(float)
    lows = h1["low"].to_numpy(float)
    opens = h1["open"].to_numpy(float)
    closes = h1["close"].to_numpy(float)
    times = h1.index
    dates = np.array([t.date() for t in times])
    day_ord = np.array([t.toordinal() for t in times])

    pivots = []  # (bar_pos, price, day_ord_of_pivot_time, timestamp)
    for side, vals in (("high", highs), ("low", lows)):
        flags = _pivot_flags(vals, side, p["swing_len"])
        for k in np.where(flags)[0]:
            pivots.append((int(k), float(vals[k]), times[k]))

    # previous-day high/low, precomputed per distinct day (in bar order)
    prev_day_hl = {}
    uniq_days = []
    for d in dates:
        if d not in prev_day_hl:
            uniq_days.append(d)
            prev_day_hl[d] = None
    for idx_d, d in enumerate(uniq_days):
        if idx_d == 0:
            continue
        prev = uniq_days[idx_d - 1]
        mask = dates == prev
        prev_day_hl[d] = (float(highs[mask].max()), float(lows[mask].min()))

    consumed: dict = {}
    signals: list = []
    for i in range(1, len(h1)):
        atr = float(atr_s.iloc[i]) if i < len(atr_s) else float("nan")
        if not np.isfinite(atr) or atr <= 0:
            continue
        t = times[i]
        hour = t.hour + t.minute / 60.0
        if not (p["session_start"] <= hour < p["session_end"]):
            continue
        if p["news_start"] <= hour < p["news_end"]:
            continue
        j = int(h4_pos[i])
        if j < 0 or not np.isfinite(ema4.iloc[j]):
            continue
        h4_up = float(h4["close"].iloc[j]) > float(ema4.iloc[j])
        h4_down = float(h4["close"].iloc[j]) < float(ema4.iloc[j])
        if not (h4_up or h4_down):
            continue

        o, hi, lo, c = opens[i], highs[i], lows[i], closes[i]
        c_prev = closes[i - 1]
        body = c - o
        rng = hi - lo
        mom_buy = body >= p["momentum_body"] * atr and \
            (rng <= 0 or c >= lo + p["close_pos"] * rng)
        mom_sell = -body >= p["momentum_body"] * atr and \
            (rng <= 0 or c <= hi - p["close_pos"] * rng)
        if not (mom_buy or mom_sell):
            continue

        # candidate levels (same construction as core/sr_engine._levels)
        lv = []
        hl = prev_day_hl.get(dates[i])
        if hl is not None and dates[i] == dates[i - 1]:
            # original: prev-day levels only once today has a prior bar
            lv += [hl[0], hl[1]]
        stale_cutoff = t - pd.Timedelta(hours=6)
        lookback_start = t - pd.Timedelta(days=p["lookback_days"])
        for k, price, tk in pivots:
            if k + p["swing_len"] > i:      # pivot not yet confirmed
                continue
            if tk > stale_cutoff or tk < lookback_start:
                continue
            lv.append(price)
        lv = [x for x in lv if np.isfinite(x)
              and abs(x - c_prev) <= p["level_atr_range"] * atr]
        if not lv:
            continue
        lv.sort()
        clusters = []
        for x in lv:
            if clusters and x - clusters[-1][-1] <= p["cluster_atr"] * atr:
                clusters[-1].append(x)
            else:
                clusters.append([x])
        levels = [float(np.median(cl)) for cl in clusters]

        used = consumed.setdefault(int(day_ord[i]), set())
        feats = {
            "atr": atr,
            "atr_pct": atr / c * 100.0 if c else 0.0,
            "rsi": float(rsi_s.iloc[i]) if np.isfinite(rsi_s.iloc[i]) else 50.0,
            "hour_utc": int(t.hour),
            "close_vs_ema200": float((c - ema200.iloc[i]) / c * 100.0)
            if np.isfinite(ema200.iloc[i]) and c else 0.0,
            "min_sl_atr": p["min_sl_atr"], "max_sl_atr": p["max_sl_atr"],
        }
        for level in levels:
            if c_prev <= level < c and mom_buy and h4_up:
                key = (round(level, 2), 1)
                if key in used:
                    continue
                used.add(key)
                signals.append(CandidateSignal(
                    engine="SR_H1", timeframe="H1", direction=1,
                    bar_index=i, signal_time=t + pd.Timedelta(hours=1),
                    stop_price=level - p["sl_atr"] * atr,
                    target_R=p["rr"], atr=atr, level=level,
                    time_stop_bars=time_stop_bars, features=dict(feats)))
                break
            if c_prev >= level > c and mom_sell and h4_down:
                key = (round(level, 2), -1)
                if key in used:
                    continue
                used.add(key)
                signals.append(CandidateSignal(
                    engine="SR_H1", timeframe="H1", direction=-1,
                    bar_index=i, signal_time=t + pd.Timedelta(hours=1),
                    stop_price=level + p["sl_atr"] * atr,
                    target_R=p["rr"], atr=atr, level=level,
                    time_stop_bars=time_stop_bars, features=dict(feats)))
                break
    return signals
