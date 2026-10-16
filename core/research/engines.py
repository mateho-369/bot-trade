"""P2 L1 candidate engines (pre-registered in research/P2_plan.md).

Pure functions: feature frame (core.research.features.compute_features)
-> list[CandidateSignal]. All conditions use the closed signal bar and
earlier data only. Common exit axes (session, H1-trend filter,
target_R, time-stop) are applied by core.research.plan, not here.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from core.signals.base import CandidateSignal

TF_MIN = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "H1": 60}


def _arr(feat, col):
    return feat[col].to_numpy(float) if col in feat.columns else None


def _mk(feat, tf, i, engine, direction, stop, atr, target_R=1.5, level=None):
    t = pd.to_datetime(feat["time"].iloc[i])
    return CandidateSignal(
        engine=engine, timeframe=tf, direction=int(direction), bar_index=int(i),
        signal_time=t + pd.Timedelta(minutes=TF_MIN.get(tf, 1)),
        stop_price=float(stop), target_R=float(target_R), atr=float(atr),
        level=None if level is None else float(level),
        features={"hour_utc": int(feat["hour_utc"].iloc[i]),
                  "h1_trend": int(feat["h1_trend"].iloc[i])
                  if "h1_trend" in feat.columns else 0,
                  "atr_pctile": float(feat["atr_pctile"].iloc[i])
                  if "atr_pctile" in feat.columns else 0.0,
                  "rsi": float(feat["rsi"].iloc[i])
                  if "rsi" in feat.columns else 50.0})


def _base_ok(feat, i):
    atr = float(feat["atr"].iloc[i]) if "atr" in feat.columns else 0.0
    return np.isfinite(atr) and atr > 0


# ── (a) Session opening-range breakout ────────────────────────────────
def orb_events(feat, tf, or_minutes=60):
    df = feat.copy()
    times = pd.to_datetime(df["time"])
    mins = (times.dt.hour * 60 + times.dt.minute).to_numpy()
    close = _arr(df, "close"); high = _arr(df, "high"); low = _arr(df, "low")
    atr = _arr(df, "atr")
    sigs = []
    for start in (8 * 60, 13 * 60):  # London 08:00, NY 13:00 UTC
        or_end, trade_end = start + or_minutes, start + 120
        in_or = (mins >= start) & (mins < or_end)
        after = (mins >= or_end) & (mins < trade_end)
        tmp = pd.DataFrame({"date": df["date"].to_numpy(),
                            "sess": start,
                            "h": np.where(in_or, high, -np.inf),
                            "l": np.where(in_or, low, np.inf)})
        g = tmp.groupby(["date", "sess"])
        or_high = g["h"].transform("max").to_numpy()
        or_low = g["l"].transform("min").to_numpy()
        valid_or = np.isfinite(or_high) & np.isfinite(or_low) & (or_high > or_low)
        cross_up = after & valid_or & (close > or_high) & \
            (np.roll(close, 1) <= np.roll(or_high, 1))
        cross_dn = after & valid_or & (close < or_low) & \
            (np.roll(close, 1) >= np.roll(or_low, 1))
        if len(close):
            cross_up[0] = cross_dn[0] = False
        seen_up = pd.Series(cross_up).groupby(
            [df["date"].to_numpy(), np.full(len(df), start)]).cumsum().to_numpy()
        seen_dn = pd.Series(cross_dn).groupby(
            [df["date"].to_numpy(), np.full(len(df), start)]).cumsum().to_numpy()
        for i in np.where((cross_up & (seen_up <= 1)) | (cross_dn & (seen_dn <= 1)))[0]:
            if not _base_ok(df, int(i)):
                continue
            if cross_up[i]:
                stop = max(or_low[i], close[i] - 1.5 * atr[i])
                if stop < close[i]:
                    sigs.append(_mk(df, tf, int(i), "ORB", 1, stop, atr[i],
                                    level=or_high[i]))
            elif cross_dn[i]:
                stop = min(or_high[i], close[i] + 1.5 * atr[i])
                if stop > close[i]:
                    sigs.append(_mk(df, tf, int(i), "ORB", -1, stop, atr[i],
                                    level=or_low[i]))
    return sorted(sigs, key=lambda s: s.bar_index)


# ── (b) Trend pullback ────────────────────────────────────────────────
def pullback_events(feat, tf, rsi_gate=False, tol_atr=0.25):
    close = _arr(feat, "close"); low = _arr(feat, "low"); high = _arr(feat, "high")
    ema20 = _arr(feat, "ema20"); ema50 = _arr(feat, "ema50")
    slope = _arr(feat, "ema50_slope_atr"); atr = _arr(feat, "atr")
    rsi = _arr(feat, "rsi")
    n = len(feat)
    ll5 = pd.Series(low).rolling(5).min().to_numpy()
    hh5 = pd.Series(high).rolling(5).max().to_numpy()
    touch_buy = (low <= ema20 + tol_atr * atr)
    touch_sell = (high >= ema20 - tol_atr * atr)
    recent_buy = pd.Series(touch_buy).rolling(5).max().fillna(0).to_numpy() > 0
    recent_sell = pd.Series(touch_sell).rolling(5).max().fillna(0).to_numpy() > 0
    sigs = []
    for i in range(1, n):
        if not _base_ok(feat, i) or not np.isfinite(ema50[i]):
            continue
        cross_up = close[i] > ema20[i] and close[i-1] <= ema20[i-1]
        cross_dn = close[i] < ema20[i] and close[i-1] >= ema20[i-1]
        up = close[i] > ema50[i] and slope[i] > 0
        dn = close[i] < ema50[i] and slope[i] < 0
        if cross_up and up and recent_buy[i] and (not rsi_gate or rsi[i] > 50):
            stop = ll5[i] - 0.25 * atr[i]
            if stop < close[i]:
                sigs.append(_mk(feat, tf, i, "PULLBACK", 1, stop, atr[i]))
        elif cross_dn and dn and recent_sell[i] and (not rsi_gate or rsi[i] < 50):
            stop = hh5[i] + 0.25 * atr[i]
            if stop > close[i]:
                sigs.append(_mk(feat, tf, i, "PULLBACK", -1, stop, atr[i]))
    return sigs


# ── (c) Liquidity sweep + BOS (port of core/smc_engine) ──────────────
def smc_events(feat, tf, bos_only=False):
    from core.smc_engine import compute_features
    df = feat.copy()
    df = df.set_index(pd.to_datetime(df["time"]))
    f = compute_features(df)
    sigs = []
    if bos_only:
        buy = (f["bull_bos"] & f["bull_trend"] & f["vol_filter"]).to_numpy()
        sell = (f["bear_bos"] & f["bear_trend"] & f["vol_filter"]).to_numpy()
    else:
        buy = f["buy_signal"].to_numpy(bool)
        sell = f["sell_signal"].to_numpy(bool)
    atr = f["atr"].to_numpy(float)
    last_low = f["lastLow"].to_numpy(float)
    last_high = f["lastHigh"].to_numpy(float)
    work = feat.reset_index(drop=True)
    for i in range(len(f)):
        if not (buy[i] or sell[i]) or not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        if buy[i] and np.isfinite(last_low[i]):
            sigs.append(_mk(work, tf, i, "SMC", 1, last_low[i] - 0.25 * atr[i],
                            atr[i]))
        elif sell[i] and np.isfinite(last_high[i]):
            sigs.append(_mk(work, tf, i, "SMC", -1, last_high[i] + 0.25 * atr[i],
                            atr[i]))
    return sigs


# ── (d) Range mean reversion (deterministic regime gate) ─────────────
def range_events(feat, tf, slope_thr=0.15):
    close = _arr(feat, "close"); low = _arr(feat, "low"); high = _arr(feat, "high")
    atr = _arr(feat, "atr"); slope = _arr(feat, "ema50_slope_atr")
    pct = _arr(feat, "atr_pctile")
    bb_low = _arr(feat, "bb_low"); bb_up = _arr(feat, "bb_up")
    bb_mid = _arr(feat, "bb_mid")
    ll5 = pd.Series(low).rolling(5).min().to_numpy()
    hh5 = pd.Series(high).rolling(5).max().to_numpy()
    regime = (np.abs(slope) < slope_thr) & (pct >= 0.1) & (pct <= 0.9)
    sigs = []
    for i in range(1, len(feat)):
        if not _base_ok(feat, i) or not regime[i] or not np.isfinite(bb_mid[i]):
            continue
        if close[i-1] <= bb_low[i-1] and close[i] > bb_low[i] and close[i] < bb_mid[i]:
            stop = ll5[i] - 0.5 * atr[i]
            risk = close[i] - stop
            if risk > 0:
                tgt = float(np.clip((bb_mid[i] - close[i]) / risk, 0.8, 2.0))
                s = _mk(feat, tf, i, "RANGE", 1, stop, atr[i], target_R=tgt)
                sigs.append(s)
        elif close[i-1] >= bb_up[i-1] and close[i] < bb_up[i] and close[i] > bb_mid[i]:
            stop = hh5[i] + 0.5 * atr[i]
            risk = stop - close[i]
            if risk > 0:
                tgt = float(np.clip((close[i] - bb_mid[i]) / risk, 0.8, 2.0))
                s = _mk(feat, tf, i, "RANGE", -1, stop, atr[i], target_R=tgt)
                sigs.append(s)
    return sigs


# ── (e) Volatility squeeze breakout ───────────────────────────────────
def squeeze_events(feat, tf, pctile=20):
    close = _arr(feat, "close"); open_ = _arr(feat, "open")
    atr = _arr(feat, "atr"); pct = _arr(feat, "atr_pctile")
    hh = _arr(feat, "hh20_prev"); ll = _arr(feat, "ll20_prev")
    comp = pd.Series(pct <= pctile / 100.0).rolling(5).max().fillna(0).to_numpy() > 0
    sigs = []
    for i in range(1, len(feat)):
        if not _base_ok(feat, i) or not comp[i] or not np.isfinite(hh[i]):
            continue
        body = close[i] - open_[i]
        if close[i] > hh[i] and body >= 0.5 * atr[i]:
            sigs.append(_mk(feat, tf, i, "SQUEEZE", 1, close[i] - 1.5 * atr[i],
                            atr[i], level=hh[i]))
        elif close[i] < ll[i] and -body >= 0.5 * atr[i]:
            sigs.append(_mk(feat, tf, i, "SQUEEZE", -1, close[i] + 1.5 * atr[i],
                            atr[i], level=ll[i]))
    return sigs


EVENT_GENERATORS = {
    "ORB": lambda feat, tf, v: orb_events(feat, tf, or_minutes=int(v)),
    "PULLBACK": lambda feat, tf, v: pullback_events(feat, tf, rsi_gate=bool(v)),
    "SMC": lambda feat, tf, v: smc_events(feat, tf, bos_only=bool(v)),
    "RANGE": lambda feat, tf, v: range_events(feat, tf, slope_thr=float(v)),
    "SQUEEZE": lambda feat, tf, v: squeeze_events(feat, tf, pctile=int(v)),
}


def apply_combo(signals, combo, timeframe):
    """Apply session / H1-trend / target / time-stop axes to events."""
    out = []
    for s in signals:
        hour = int(s.features.get("hour_utc", 0))
        lo, hi = combo.session_range
        if not (lo <= hour < hi):
            continue
        if combo.h1_trend and int(s.features.get("h1_trend", 0)) != 0 \
                and int(s.features.get("h1_trend", 0)) != s.direction:
            continue
        target = s.target_R if combo.target_R is None else combo.target_R
        out.append(replace(s, target_R=float(target),
                           time_stop_bars=combo.time_stop_bars))
    return out
