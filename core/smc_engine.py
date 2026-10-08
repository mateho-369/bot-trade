# ============================================================
#   core/smc_engine.py  v1.2
#   Exact Pine SMC logic — fixed dropna + hour_utc bug
# ============================================================

import numpy as np
import pandas as pd
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import config


# ── ATR ──────────────────────────────────────────────────────
def calc_atr(df, length=14):
    high       = df['high']
    low        = df['low']
    close      = df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/length, min_periods=length, adjust=False).mean()


# ── EMA ──────────────────────────────────────────────────────
def calc_ema(series, length):
    return series.ewm(span=length, min_periods=length, adjust=False).mean()


# ── RSI ──────────────────────────────────────────────────────
def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ── PIVOT HIGH ───────────────────────────────────────────────
# Pine: ta.pivothigh(high, swingLen, swingLen)
# high[i] > all highs in [i-len..i-1] AND [i+1..i+len]
def calc_pivot_high(high_series, length):
    high = high_series.values
    n    = len(high)
    out  = np.full(n, np.nan)
    for i in range(length, n - length):
        center = high[i]
        if center > high[i-length:i].max() and center > high[i+1:i+length+1].max():
            out[i] = center
    return pd.Series(out, index=high_series.index)


# ── PIVOT LOW ────────────────────────────────────────────────
def calc_pivot_low(low_series, length):
    low = low_series.values
    n   = len(low)
    out = np.full(n, np.nan)
    for i in range(length, n - length):
        center = low[i]
        if center < low[i-length:i].min() and center < low[i+1:i+length+1].min():
            out[i] = center
    return pd.Series(out, index=low_series.index)


# ── COMPUTE FEATURES ─────────────────────────────────────────
def compute_features(df):
    """
    Compute all SMC features. Exact Pine logic.
    Input:  OHLCV DataFrame with DatetimeIndex
    Output: DataFrame with all signal columns
    """
    df = df.copy()

    # ── Indicators ───────────────────────────────────────────
    df['atr']      = calc_atr(df, config.ATR_LEN)
    df['ema']      = calc_ema(df['close'], config.EMA_LEN)
    df['atr_sma50']= df['atr'].rolling(50).mean()
    df['vol_filter']= df['atr'] > df['atr_sma50'] * config.MIN_ATR_MULT

    # ── Pivots ───────────────────────────────────────────────
    df['ph']       = calc_pivot_high(df['high'], config.SWING_LEN)
    df['pl']       = calc_pivot_low (df['low'],  config.SWING_LEN)
    # FIX: a pivot is only CONFIRMED SWING_LEN bars after it forms (the whole
    # right side of the pivot must already exist). Without this shift the
    # backtester was using pivots ~5 bars before they could be known —
    # look-ahead bias that made results look better than live trading.
    # With the shift, backtest timing == live timing (MT5/Pine style).
    df['lastHigh'] = df['ph'].shift(config.SWING_LEN).ffill()
    df['lastLow']  = df['pl'].shift(config.SWING_LEN).ffill()

    # ── Displacement ─────────────────────────────────────────
    df['body_size']    = (df['close'] - df['open']).abs()
    df['displacement'] = df['body_size'] > df['atr'] * config.DISP_MULT

    # ── Sweep & BOS ──────────────────────────────────────────
    df['bull_sweep'] = (
        df['lastLow'].notna() &
        (df['low']   < df['lastLow']) &
        (df['close'] > df['lastLow']) &
        df['displacement']
    )
    df['bear_sweep'] = (
        df['lastHigh'].notna() &
        (df['high']  > df['lastHigh']) &
        (df['close'] < df['lastHigh']) &
        df['displacement']
    )
    df['bull_bos'] = (
        df['lastHigh'].notna() &
        (df['close'].shift(1) <= df['lastHigh']) &
        (df['close']           > df['lastHigh']) &
        df['displacement']
    )
    df['bear_bos'] = (
        df['lastLow'].notna() &
        (df['close'].shift(1) >= df['lastLow']) &
        (df['close']           < df['lastLow']) &
        df['displacement']
    )

    # ── Trend ────────────────────────────────────────────────
    df['bull_trend'] = df['close'] > df['ema']
    df['bear_trend'] = df['close'] < df['ema']

    # ── Final signals ────────────────────────────────────────
    df['buy_signal']  = (
        (df['bull_sweep'] | df['bull_bos']) &
        df['bull_trend'] &
        df['vol_filter']
    )
    df['sell_signal'] = (
        (df['bear_sweep'] | df['bear_bos']) &
        df['bear_trend'] &
        df['vol_filter']
    )

    # ── ML extra features ────────────────────────────────────
    df['rsi']          = calc_rsi(df['close'], 14)
    df['atr_pct']      = df['atr'] / df['close'] * 100
    df['body_pct']     = df['body_size'] / df['close'] * 100
    df['close_vs_ema'] = (df['close'] - df['ema']) / df['ema'] * 100

    # FIX: hour_utc must be a full Series, not a scalar
    if hasattr(df.index, 'hour'):
        df['hour_utc'] = df.index.hour.astype(int)
    else:
        df['hour_utc'] = pd.Series(np.zeros(len(df), dtype=int), index=df.index)

    df['candle_dir'] = np.where(df['close'] > df['open'], 1, -1)

    # ── Candle pattern detection ──────────────────────────────
    # Engulfing: current body fully covers previous body in the SAME direction
    # as the signal (bullish engulfing = bull body wraps bear body).
    prev_open  = df['open'].shift(1)
    prev_close = df['close'].shift(1)
    prev_body  = (prev_close - prev_open)
    cur_body   = (df['close'] - df['open'])
    df['bull_engulf'] = (
        (cur_body > 0) & (prev_body < 0) &
        (df['open']  <= prev_close) &
        (df['close'] >= prev_open)
    )
    df['bear_engulf'] = (
        (cur_body < 0) & (prev_body > 0) &
        (df['open']  >= prev_close) &
        (df['close'] <= prev_open)
    )

    # Pin bar (hammer / shooting star): wick >= 2× body on the rejection side,
    # small body (body < 40% of ATR).  Fires on the bar itself.
    bar_range  = df['high'] - df['low']
    body_abs   = cur_body.abs()
    upper_wick = df['high'] - df[['open','close']].max(axis=1)
    lower_wick = df[['open','close']].min(axis=1) - df['low']
    small_body = body_abs < df['atr'] * 0.40
    df['bull_pin'] = (
        small_body &
        (lower_wick >= 2 * body_abs.clip(lower=1e-10)) &
        (lower_wick >= 0.5 * bar_range)
    )
    df['bear_pin'] = (
        small_body &
        (upper_wick >= 2 * body_abs.clip(lower=1e-10)) &
        (upper_wick >= 0.5 * bar_range)
    )

    # Inside bar: current high/low fully inside the previous bar's range.
    df['inside_bar'] = (
        (df['high'] < df['high'].shift(1)) &
        (df['low']  > df['low'].shift(1))
    )

    # Pattern strength score: how many confirming candle patterns align with
    # the current bar's signal direction (0-3).  Filled into the signal dict.
    df['bull_pattern_score'] = (
        df['bull_engulf'].astype(int) +
        df['bull_pin'].astype(int) +
        (~df['inside_bar']).astype(int)   # inside bar = AGAINST bullish breakout
    )
    df['bear_pattern_score'] = (
        df['bear_engulf'].astype(int) +
        df['bear_pin'].astype(int) +
        (~df['inside_bar']).astype(int)
    )

    return df


# ── LIVE SIGNAL ──────────────────────────────────────────────
def get_latest_signal(df):
    """
    Get signal from last confirmed closed bar.
    Only fires on rows where indicator warmup is complete.
    """
    if df is None or len(df) < config.EMA_LEN + 10:
        return None

    # Only consider rows where all core indicators are ready
    # (not NaN) — same as Pine only drawing after warmup
    valid = df.dropna(subset=['atr', 'ema', 'atr_sma50', 'lastHigh', 'lastLow'])
    if len(valid) == 0:
        return None

    last = valid.iloc[-1]

    buy_sig  = bool(last['buy_signal'])
    sell_sig = bool(last['sell_signal'])

    if not buy_sig and not sell_sig:
        return None

    signal = 'BUY' if buy_sig else 'SELL'
    atr    = float(last['atr'])

    # ── Last-5-candle context for the AI snapshot ─────────────
    # Gives the AI a mini "chart read": direction, body size relative to ATR,
    # and upper/lower wick size for each of the last 5 confirmed bars.
    last5_rows = valid.tail(5)
    last5 = []
    for _, row in last5_rows.iterrows():
        _bar_range  = row['high'] - row['low']
        _body       = abs(row['close'] - row['open'])
        _upper_wick = row['high'] - max(row['open'], row['close'])
        _lower_wick = min(row['open'], row['close']) - row['low']
        last5.append({
            'dir':        1 if row['close'] > row['open'] else -1,
            'body_atr':   round(_body  / atr, 3) if atr > 0 else 0,
            'upper_wick': round(_upper_wick / atr, 3) if atr > 0 else 0,
            'lower_wick': round(_lower_wick / atr, 3) if atr > 0 else 0,
        })

    # Distance from price to the last swing high / low (in ATR units)
    dist_to_high = round((float(last['lastHigh']) - float(last['close'])) / atr, 2) \
        if atr > 0 and not pd.isna(last['lastHigh']) else None
    dist_to_low  = round((float(last['close']) - float(last['lastLow']))  / atr, 2) \
        if atr > 0 and not pd.isna(last['lastLow'])  else None

    # atr_sma: 50-bar average ATR — used by the ATR expansion block in main.py
    atr_sma = float(last['atr_sma50']) if not pd.isna(last.get('atr_sma50', float('nan'))) else None

    # Pattern score for the signal direction (0–3)
    pat_key = 'bull_pattern_score' if buy_sig else 'bear_pattern_score'
    pattern_score = int(last.get(pat_key, 0))

    return {
        'signal':            signal,
        'bar_time':          valid.index[-1],
        'close':             float(last['close']),
        'atr':               atr,
        'atr_sma':           atr_sma,          # ← Task 5: ATR expansion block
        'ema':               float(last['ema']),
        'rsi':               float(last['rsi']),
        'atr_pct':           float(last['atr_pct']),
        'body_pct':          float(last['body_pct']),
        'close_vs_ema':      float(last['close_vs_ema']),
        'bull_sweep':        bool(last['bull_sweep']),
        'bear_sweep':        bool(last['bear_sweep']),
        'bull_bos':          bool(last['bull_bos']),
        'bear_bos':          bool(last['bear_bos']),
        'bull_trend':        bool(last['bull_trend']),
        'bear_trend':        bool(last['bear_trend']),
        'vol_filter':        bool(last['vol_filter']),
        'lastHigh':          float(last['lastHigh']),
        'lastLow':           float(last['lastLow']),
        'hour_utc':          int(last['hour_utc']),
        # ── new ──────────────────────────────────────────────
        'bull_engulf':       bool(last['bull_engulf']),
        'bear_engulf':       bool(last['bear_engulf']),
        'bull_pin':          bool(last['bull_pin']),
        'bear_pin':          bool(last['bear_pin']),
        'inside_bar':        bool(last['inside_bar']),
        'pattern_score':     pattern_score,    # 0=weak 1=ok 2=good 3=great
        'dist_to_high_atr':  dist_to_high,     # +ve = price below swing high
        'dist_to_low_atr':   dist_to_low,      # +ve = price above swing low
        'last5_candles':     last5,            # mini chart for AI
    }
