# ============================================================
#   core/mtf_engine.py
#   Multi-Timeframe Engine
#   Scans M1, M5, M15 and decides:
#   - Which timeframe to trade
#   - How many positions (1-3)
#   - Signal strength score
# ============================================================

import logging
import pandas as pd
import numpy as np
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import config
from core.smc_engine import compute_features, get_latest_signal
from core.state_logger import signal_log as _sig_log

logger = logging.getLogger("MTFEngine")

# Timeframes to scan in order (fastest to slowest)
# (actual list set below after the SCALPING QUALITY FLOOR note)

# Weight of each timeframe signal (higher TF = more reliable)
TF_WEIGHT = {
    "M1":  1,
    "M5":  2,
    "M15": 3,
}

# SCALPING QUALITY FLOOR (2026-10-07): M1 signals dropped — live data showed
# M1 scalps lose to spread and noise (avg win +$0.06 vs losses), while the
# winners came from M5/M15. The engine now scans M5 + M15 only.
TIMEFRAMES = ["M5", "M15"]


def active_timeframes():
    """Timeframes the scan looks at. The scalper profile adds M1 (v2.3.2): with
    only M5/M15 a scalper waits for a closed-bar M5/M15 setup, which on one gold
    chart is a handful of signals a day - the AI Analyst could watch a clean M1
    sell-off for 6 minutes and the bot still had nothing to act on. The cost
    gates (spread vs stop, spread vs $ target) are what keep M1 honest."""
    if getattr(config, "SCALP_SCAN_M1", False):
        return ["M1"] + list(TIMEFRAMES)
    return list(TIMEFRAMES)
TF_ATR_MULT = {
    # SCALPING MODE (owner request 2026-10-07): tight targets for many
    # small wins. Was {"M1": 3.0, "M5": 3.5, "M15": 4.0} take-profit.
    # RR kept >= ~1.3 so the AI risk reviewer does not reject 1:1 setups.
    "M1":  {"sl": 1.5, "tp": 2.0},
    "M5":  {"sl": 1.8, "tp": 2.5},
    "M15": {"sl": 2.0, "tp": 3.0},
}


# ── v1.9: H1 TREND GATE ───────────────────────────────────────
# The engine duel (same market, same costs) showed entries that go AGAINST
# the H1 trend bleed ~10x more per trade. These functions are the single
# source of truth used by main.py (live) and the backtest harness.
def h1_trend_direction(df_h1, ema_len=None):
    """Given an H1 OHLCV df (last confirmed bar last), return 'BUY' (uptrend),
    'SELL' (downtrend) or None (not enough data)."""
    span = ema_len or config.H1_EMA_LEN
    if df_h1 is None or len(df_h1) < span + 5:
        return None
    ema = df_h1['close'].ewm(span=span, min_periods=span, adjust=False).mean()
    last_ema = ema.iloc[-1]
    if pd.isna(last_ema):
        return None
    return 'BUY' if df_h1['close'].iloc[-1] > last_ema else 'SELL'


def h1_gate_ok(direction, h1_dir):
    """True if the entry is allowed under the H1 trend filter."""
    if not getattr(config, 'H1_TREND_FILTER', False) or h1_dir is None:
        return True
    return direction == h1_dir


def scan_all_timeframes(symbol, connector_func):
    """
    Scans the active timeframes (M5+M15, plus M1 for the scalper profile).
    Returns MTF decision dict.

    connector_func = get_candles function from mt5_connector
    """
    results = {}

    for tf in active_timeframes():
        try:
            # Need enough bars for EMA200 warmup
            count = 500 if tf == "M1" else 300
            df = connector_func(symbol, tf, count=count)

            if df is None or len(df) < 220:
                _sig_log.log(f"{symbol}:{tf}", f"[{symbol}][{tf}] Not enough data")
                results[tf] = None
                continue

            df_feat = compute_features(df)
            signal  = get_latest_signal(df_feat)

            # Change-only: log a TF's signal state only when it actually changes,
            # instead of repeating "No signal" every bar.
            if signal:
                _sig_log.log(
                    f"{symbol}:{tf}",
                    f"[{symbol}][{tf}] Signal: {signal['signal']} | "
                    f"RSI:{signal.get('rsi',0):.1f} | ATR:{signal.get('atr',0):.2f}"
                )
            else:
                _sig_log.log(f"{symbol}:{tf}", f"[{symbol}][{tf}] No signal")

            results[tf] = {
                'signal':   signal,
                'df_feat':  df_feat,
                'weight':   TF_WEIGHT[tf],
            }

        except Exception as e:
            logger.error(f"[{symbol}][{tf}] Scan error: {e}")
            results[tf] = None

    return _make_decision(symbol, results)


def _make_decision(symbol, results):
    """
    Combines all TF signals into a trading decision.
    Returns decision dict or None.
    """
    # ── v1.9: M15 PRIORITY — if an M15 signal exists, trade ONLY that ──
    # M1/M5 become pure noise; the M15 ATR is ~2x wider so the SL distance
    # grows and the broker spread becomes a smaller % of risk. In the engine
    # duel this cut cost/SL from 11% → 8% and cut max drawdown nearly in half.
    if getattr(config, 'MTF_PRIORITY_M15', False):
        d15 = results.get('M15')
        if d15 is not None and d15['signal'] is not None:
            results = {'M15': d15}

    buy_score  = 0
    sell_score = 0
    signals_found = []
    best_tf    = None
    best_weight = 0

    for tf, data in results.items():
        if data is None or data['signal'] is None:
            continue

        sig    = data['signal']['signal']
        weight = data['weight']
        rsi    = float(data['signal'].get('rsi', 50))
        pat    = int(data['signal'].get('pattern_score', 0))

        # ── RSI chop-zone filter ──────────────────────────────
        # Block signals where RSI is in the 48-52 no-man's-land (no
        # directional momentum). A strong candle pattern (engulf/pin)
        # overrides because price action trumps oscillator noise.
        if getattr(config, 'RSI_FILTER_ENABLED', True):
            bull_min = float(getattr(config, 'RSI_BULL_MIN', 52))
            bear_max = float(getattr(config, 'RSI_BEAR_MAX', 48))
            override = int(getattr(config, 'RSI_CHOP_PATTERN_OVERRIDE', 2))
            if sig == 'BUY'  and rsi < bull_min and pat < override:
                _sig_log.log(f"{symbol}:{tf}:rsi",
                             f"[{symbol}][{tf}] RSI-CHOP BUY blocked "
                             f"(RSI {rsi:.1f} < {bull_min}, pattern {pat})")
                continue
            if sig == 'SELL' and rsi > bear_max and pat < override:
                _sig_log.log(f"{symbol}:{tf}:rsi",
                             f"[{symbol}][{tf}] RSI-CHOP SELL blocked "
                             f"(RSI {rsi:.1f} > {bear_max}, pattern {pat})")
                continue

        if sig == 'BUY':
            buy_score  += weight
            signals_found.append({'tf': tf, 'dir': 'BUY', 'weight': weight, 'data': data})
        elif sig == 'SELL':
            sell_score += weight
            signals_found.append({'tf': tf, 'dir': 'SELL', 'weight': weight, 'data': data})

        # Track highest TF with signal
        if weight > best_weight:
            best_weight = weight
            best_tf     = tf

    # No signals at all
    if buy_score == 0 and sell_score == 0:
        return None

    # Conflicting signals (both buy and sell) — skip
    if buy_score > 0 and sell_score > 0:
        logger.info(f"[{symbol}] MTF conflict — BUY score:{buy_score} SELL score:{sell_score} — skipping")
        return None

    # Determine direction
    direction  = 'BUY' if buy_score > sell_score else 'SELL'
    total_score = buy_score if direction == 'BUY' else sell_score

    # Number of positions based on agreement strength
    # Score 1 (only M1) → 1 position
    # Score 2-3 (M1+M5 or M5 alone) → 2 positions
    # Score 4+ (M15 or multiple agree) → 3 positions
    if total_score >= 4:
        n_positions = 3
    elif total_score >= 2:
        n_positions = 2
    else:
        n_positions = 1

    # Choose best timeframe for trade settings
    # Prefer highest TF that agrees with direction
    chosen_tf   = None
    chosen_data = None
    for tf in reversed(active_timeframes()):  # M15 → M5 → M1
        data = results.get(tf)
        if data and data['signal'] and data['signal']['signal'] == direction:
            chosen_tf   = tf
            chosen_data = data
            break

    if chosen_tf is None or chosen_data is None:
        return None

    signal = chosen_data['signal']

    # change-only: logs when the decision changes, not every bar it persists
    _sig_log.log(
        f"{symbol}:decision",
        f"[{symbol}] MTF Decision: {direction} | "
        f"Score:{total_score} | "
        f"Positions:{n_positions} | "
        f"Best TF:{chosen_tf} | "
        f"TFs agreeing:{[s['tf'] for s in signals_found if s['dir']==direction]}"
    )

    return {
        'direction':   direction,
        'n_positions': n_positions,
        'chosen_tf':   chosen_tf,
        'signal':      signal,
        'df_feat':     chosen_data['df_feat'],
        'score':       total_score,
        'sl_mult':     TF_ATR_MULT[chosen_tf]['sl'],
        'tp_mult':     TF_ATR_MULT[chosen_tf]['tp'],
        'tfs_agreeing': [s['tf'] for s in signals_found if s['dir'] == direction],
        'summary': (
            f"{direction} | TF:{chosen_tf} | "
            f"Score:{total_score} | {n_positions} pos | "
            f"TFs:{[s['tf'] for s in signals_found if s['dir']==direction]}"
        )
    }
