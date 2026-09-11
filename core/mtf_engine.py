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
TIMEFRAMES = ["M1", "M5", "M15"]

# Weight of each timeframe signal (higher TF = more reliable)
TF_WEIGHT = {
    "M1":  1,
    "M5":  2,
    "M15": 3,
}

# ATR multiplier per timeframe (higher TF = wider SL/TP)
TF_ATR_MULT = {
    "M1":  {"sl": 1.5, "tp": 3.0},
    "M5":  {"sl": 1.8, "tp": 3.5},
    "M15": {"sl": 2.0, "tp": 4.0},
}


def scan_all_timeframes(symbol, connector_func):
    """
    Scans M1, M5, M15 for signals.
    Returns MTF decision dict.

    connector_func = get_candles function from mt5_connector
    """
    results = {}

    for tf in TIMEFRAMES:
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
    for tf in reversed(TIMEFRAMES):  # M15 → M5 → M1
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
