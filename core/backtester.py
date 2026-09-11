# ============================================================
#   core/backtester.py  v1.2
#   Fixed: use row['col'] not row.get() for pandas Series
# ============================================================

import pandas as pd
import numpy as np
import logging
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import config
from core.smc_engine import compute_features

logger = logging.getLogger("Backtester")


def run_backtest(df, symbol="SYMBOL", verbose=True):
    """
    Runs Pine-equivalent backtest on historical OHLCV data.
    Returns: results dict + trade_log DataFrame
    """
    # Step 1: compute features
    df_feat = compute_features(df.copy())

    # Step 2: drop warmup rows (NaN from EMA/ATR/pivot calc)
    df_feat = df_feat.dropna(subset=['buy_signal','sell_signal','atr','ema'])
    df_feat = df_feat.reset_index(drop=False)  # keep time as column

    if len(df_feat) == 0:
        logger.info(f"[{symbol}] No valid rows after warmup drop")
        return _empty_results(symbol), pd.DataFrame()

    # Debug: count raw signals before cooldown
    raw_buys  = int(df_feat['buy_signal'].sum())
    raw_sells = int(df_feat['sell_signal'].sum())
    logger.info(f"[{symbol}] Raw signals — BUY:{raw_buys} SELL:{raw_sells} "
                f"over {len(df_feat)} bars")

    trades    = []
    in_trade  = False
    entry     = 0.0
    tp        = 0.0
    sl        = 0.0
    direction = 0
    last_bar  = -999

    for i in range(len(df_feat)):
        row = df_feat.iloc[i]

        # ── Check TP/SL on open trade ─────────────────────────
        if in_trade:
            trade_closed = False
            outcome      = None
            pnl          = 0.0
            high_i       = float(row['high'])
            low_i        = float(row['low'])

            if direction == 1:    # BUY
                if high_i >= tp:
                    outcome = 1; pnl = tp - entry
                    trade_closed = True; in_trade = False
                elif low_i <= sl:
                    outcome = 0; pnl = sl - entry
                    trade_closed = True; in_trade = False

            elif direction == -1:  # SELL
                if low_i <= tp:
                    outcome = 1; pnl = entry - tp
                    trade_closed = True; in_trade = False
                elif high_i >= sl:
                    outcome = 0; pnl = entry - sl
                    trade_closed = True; in_trade = False

            if trade_closed and trades:
                trades[-1]['outcome']   = outcome
                trades[-1]['pnl']       = pnl
                trades[-1]['close_bar'] = i

        # ── Entry logic ───────────────────────────────────────
        can_trade  = (i - last_bar) > config.COOLDOWN_BARS
        buy_sig    = bool(row['buy_signal'])
        sell_sig   = bool(row['sell_signal'])

        if not in_trade and can_trade:
            atr = float(row['atr'])

            if buy_sig:
                entry     = float(row['open'])
                tp        = entry + atr * config.TP_MULT
                sl        = entry - atr * config.SL_MULT
                direction = 1
                in_trade  = True
                last_bar  = i
                trades.append(_make_row(i, row, symbol, 'BUY', entry, tp, sl, atr))

            elif sell_sig:
                entry     = float(row['open'])
                tp        = entry - atr * config.TP_MULT
                sl        = entry + atr * config.SL_MULT
                direction = -1
                in_trade  = True
                last_bar  = i
                trades.append(_make_row(i, row, symbol, 'SELL', entry, tp, sl, atr))

    # ── Build results ─────────────────────────────────────────
    if len(trades) == 0:
        logger.info(f"[{symbol}] Backtest: 0 trades entered (all filtered by cooldown or no signals)")
        return _empty_results(symbol), pd.DataFrame()

    trade_log = pd.DataFrame(trades)
    closed    = trade_log.dropna(subset=['outcome'])
    win_count = int(closed['outcome'].sum()) if len(closed) > 0 else 0
    total_cl  = len(closed)
    wr        = win_count / total_cl * 100 if total_cl > 0 else 0.0

    results = {
        'symbol':      symbol,
        'total':       total_cl,
        'wins':        win_count,
        'losses':      total_cl - win_count,
        'winrate':     wr,
        'open_trades': len(trade_log) - total_cl,
    }

    if verbose:
        logger.info(
            f"[{symbol}] Backtest done | "
            f"Trades:{total_cl} W:{win_count} L:{total_cl-win_count} "
            f"WR:{wr:.1f}%"
        )

    return results, trade_log


# ── HELPERS ──────────────────────────────────────────────────

def _safe(row, col, default=0):
    """Safely get value from pandas Series row"""
    try:
        v = row[col]
        return default if pd.isna(v) else v
    except (KeyError, TypeError):
        return default

def _make_row(i, row, symbol, signal, entry, tp, sl, atr):
    is_buy = (signal == 'BUY')
    return {
        'bar':          i,
        'time':         _safe(row, 'time', i),
        'symbol':       symbol,
        'signal':       signal,
        'entry':        entry,
        'tp':           tp,
        'sl':           sl,
        'atr':          atr,
        'atr_pct':      float(_safe(row, 'atr_pct',      0)),
        'rsi':          float(_safe(row, 'rsi',           50)),
        'close_vs_ema': float(_safe(row, 'close_vs_ema',  0)),
        'body_pct':     float(_safe(row, 'body_pct',      0)),
        'hour_utc':     int  (_safe(row, 'hour_utc',      0)),
        'candle_dir':   1 if is_buy else -1,
        'bull_sweep':   int(bool(_safe(row, 'bull_sweep', False))) if is_buy  else 0,
        'bear_sweep':   0 if is_buy else int(bool(_safe(row, 'bear_sweep', False))),
        'bull_bos':     int(bool(_safe(row, 'bull_bos',   False))) if is_buy  else 0,
        'bear_bos':     0 if is_buy else int(bool(_safe(row, 'bear_bos',   False))),
        'signal_type':  1 if is_buy else -1,
        'spread_pct':   0.0,
        'outcome':      np.nan,
        'pnl':          np.nan,
        'close_bar':    np.nan,
    }

def _empty_results(symbol):
    return {'symbol': symbol, 'total': 0, 'wins': 0,
            'losses': 0, 'winrate': 0.0, 'open_trades': 0}
