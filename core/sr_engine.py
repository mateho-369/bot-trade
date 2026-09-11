# ============================================================
#   core/sr_engine.py  —  ENGINE C (duel winner, v1.9.1)
#   S/R Breakout Engine — the architecture of the best-documented
#   gold bot builds, re-implemented in this bot's code:
#
#     • Daily S/R levels (previous day high/low) + recent confirmed
#       H1 swing pivots, clustered
#     • H1 close crosses a level  → breakout candidate
#     • Momentum confirmation (body >= 0.5*ATR, close in the upper
#       40% of the bar) → false-breakout filter
#     • H4 trend filter (close vs EMA) → only trade with trend
#     • Session 7-20 UTC + news window 12:30-14:30 UTC skip
#     • Each level consumed once per day, per direction
#     • H1 execution: SL at level ∓ 0.25*ATR (capped), TP = RR * R
#
#   No grid, no martingale — hard SL/TP on every trade.
#   Verified in the engine-vs-engine duel (REAL_WORLD_TEST.md):
#   +0.015 R/trade, PF 1.15, max DD 4.0R on 2024-2025 real data.
# ============================================================

import numpy as np
import pandas as pd
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import config
from core.smc_engine import calc_atr, calc_rsi, calc_ema

H4_EMA = getattr(config, 'SR_H4_EMA_LEN', 50)


def _pivot_flags(values, side, length):
    """Classic swing pivots (Pine pivothigh/pivotlow), marked at the CENTER
    bar. A pivot is only usable `length` bars later — callers must respect
    the confirmation lag (we additionally require 6h staleness)."""
    v = values
    n = len(v)
    out = np.zeros(n, dtype=bool)
    for i in range(length, n - length):
        if side == 'high':
            if v[i] > v[i-length:i].max() and v[i] > v[i+1:i+length+1].max():
                out[i] = True
        else:
            if v[i] < v[i-length:i].min() and v[i] < v[i+1:i+length+1].min():
                out[i] = True
    return out


def _levels(h1, atr_now, t_now):
    """Candidate S/R levels available at time t_now (confirmed bars only).
    Previous-day high/low + confirmed swing pivots from the lookback window,
    filtered to SR_LEVEL_ATR_RANGE*ATR and clustered at SR_CLUSTER_ATR*ATR.
    Returns a sorted list of level prices."""
    n = len(h1)
    idx = h1.index
    day = idx.date
    # previous complete day (bar i must not be the first bar of its day)
    i = n - 1
    prev_days = [k for k in range(n) if day[k] < day[i]]
    lv = []
    if prev_days:
        j = prev_days[-1]
        if i - j > 1:
            d = day[j]
            dmask = (day == d)
            lv += [float(h1['high'].values[dmask].max()),
                   float(h1['low'].values[dmask].min())]
    # swing pivots: confirmed (length bars later) AND >= 6h stale (matches
    # the tested build), within the lookback window
    L = getattr(config, 'SR_SWING_LEN', 5)
    hi = h1['high'].values
    lo = h1['low'].values
    t_now = idx[i]
    lookback_start = t_now - pd.Timedelta(days=getattr(config, 'SR_LOOKBACK_DAYS', 10))
    stale_cutoff = t_now - pd.Timedelta(hours=6)
    for side, vals in (('high', hi), ('low', lo)):
        flags = _pivot_flags(vals, side, L)
        for k in np.where(flags)[0]:
            tk = idx[k]
            # pivot must be fully confirmed (L bars later) AND >= 6h stale —
            # this matches the tested build exactly
            if tk > stale_cutoff:
                continue
            if tk < lookback_start:
                continue
            lv.append(float(vals[k]))
    # filter + cluster
    lv = [x for x in lv if np.isfinite(x) and abs(x - float(h1['close'].values[i - 1])) <=
          getattr(config, 'SR_LEVEL_ATR_RANGE', 1.0) * atr_now]
    lv.sort()
    clusters = []
    for x in lv:
        if clusters and x - clusters[-1][-1] <= getattr(config, 'SR_CLUSTER_ATR', 0.25) * atr_now:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return [float(np.median(c)) for c in clusters]


def scan_sr(h1, h4, consumed, symbol="XAUUSD"):
    """
    ENGINE C scan. h1/h4 = confirmed-bar OHLCV frames (last row = last
    confirmed bar). `consumed` = {day_ordinal: set of (level_rounded, dir)}.
    Returns a decision dict (execute_trade compatible) or None.
    """
    if h1 is None or len(h1) < 60 or h4 is None or len(h4) < H4_EMA + 5:
        return None

    atr = calc_atr(h1, getattr(config, 'SR_ATR_LEN', 14)).iloc[-1]
    if not np.isfinite(atr) or atr <= 0:
        return None

    t = h1.index[-1]
    hour = t.hour + t.minute / 60.0
    # session + news window
    if not (config.SESSION_START <= hour < config.SESSION_END):
        return None
    if getattr(config, 'SR_NEWS_START', 12.5) <= hour < getattr(config, 'SR_NEWS_END', 14.5):
        return None

    # H4 trend (last confirmed H4 bar in the passed slice)
    ema4 = calc_ema(h4['close'], H4_EMA)
    if pd.isna(ema4.iloc[-1]):
        return None
    h4_up   = h4['close'].iloc[-1] > ema4.iloc[-1]
    h4_down = h4['close'].iloc[-1] < ema4.iloc[-1]

    o, hi, lo, c = (float(h1['open'].iloc[-1]),  float(h1['high'].iloc[-1]),
                    float(h1['low'].iloc[-1]),   float(h1['close'].iloc[-1]))
    c_prev = float(h1['close'].iloc[-2])
    body = c - o
    rng = hi - lo
    mom_buy  = body >= getattr(config, 'SR_MOMENTUM_BODY', 0.5) * atr and \
               (rng <= 0 or c >= lo + getattr(config, 'SR_CLOSE_POS', 0.6) * rng)
    mom_sell = -body >= getattr(config, 'SR_MOMENTUM_BODY', 0.5) * atr and \
               (rng <= 0 or c <= hi - getattr(config, 'SR_CLOSE_POS', 0.6) * rng)

    levels = _levels(h1, float(atr), t)
    day_ord = t.toordinal()
    consumed.setdefault(day_ord, set())
    sl_atr = getattr(config, 'SR_SL_ATR', 0.25)
    rr     = getattr(config, 'SR_RR', 2.0)

    signal_ctx = {
        'atr':      float(atr),
        'atr_pct':  float(atr) / c * 100.0,
        'rsi':      float(calc_rsi(h1['close'], 14).iloc[-1]),
        'body_pct': float(body) / c * 100.0,
        'close_vs_ema': float((c - h1['close'].iloc[-1]) / c * 100.0),
        'hour_utc': int(t.hour),
        'bull_sweep': False, 'bear_sweep': False,
        'bull_bos':   False, 'bear_bos':   False,
    }
    signal_ctx['close_vs_ema'] = float(
        (c - calc_ema(h1['close'], config.EMA_LEN).iloc[-1]) / c * 100.0)

    for L_ in levels:
        if c_prev <= L_ < c and mom_buy and h4_up:
            key = (round(L_, 2), 1)
            if key in consumed[day_ord]:
                continue
            consumed[day_ord].add(key)
            return {
                'engine':      'SR_H1',
                'direction':   'BUY',
                'n_positions': 1,
                'chosen_tf':   'H1',
                'signal':      dict(signal_ctx, signal='BUY', bar_time=t),
                'sl_price':    L_ - sl_atr * float(atr),
                'sl_atr':      float(atr),
                'rr':          rr,
                'score':       1,
                'summary':     f"BUY H1 breakout {L_:.2f}",
            }
        if c_prev >= L_ > c and mom_sell and h4_down:
            key = (round(L_, 2), -1)
            if key in consumed[day_ord]:
                continue
            consumed[day_ord].add(key)
            return {
                'engine':      'SR_H1',
                'direction':   'SELL',
                'n_positions': 1,
                'chosen_tf':   'H1',
                'signal':      dict(signal_ctx, signal='SELL', bar_time=t),
                'sl_price':    L_ + sl_atr * float(atr),
                'sl_atr':      float(atr),
                'rr':          rr,
                'score':       1,
                'summary':     f"SELL H1 breakout {L_:.2f}",
            }
    return None
