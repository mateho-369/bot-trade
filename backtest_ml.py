# ============================================================
#   backtest_ml.py
#   HONEST walk-forward validation of the ML filter.
#
#   The old validate_ml.py tests the model on the SAME trades it
#   trained on (in-sample) — that always looks good and proves
#   nothing. This script does it the right way:
#
#     1. Gather labelled trades (fresh MT5 backtest if a terminal is
#        available, otherwise the logged trade_log.csv).
#     2. Walk forward in time: train on the past, predict the next
#        unseen chunk, roll forward. All predictions are OUT-OF-SAMPLE.
#     3. Answer the real question: does filtering by the ML actually
#        improve win rate / profit factor vs taking every signal?
#
#   Run:  python backtest_ml.py
# ============================================================

import os
import sys
import numpy as np
import pandas as pd

sys.path.append(os.path.dirname(__file__))
import config

FEATURE_COLS = [
    'atr_pct', 'rsi', 'close_vs_ema', 'body_pct', 'hour_utc',
    'bull_sweep', 'bear_sweep', 'bull_bos', 'bear_bos'
]


# ── DATA GATHERING ───────────────────────────────────────────

def gather_from_backtest(days=45):
    """Run the strategy backtest over recent history to get many labelled
    trades. Needs a running MT5 terminal. Returns a DataFrame or None."""
    try:
        from core.mt5_connector import connect, get_candles_history
        from core.backtester import run_backtest
    except Exception as e:
        print(f"(MT5 modules unavailable: {e})")
        return None

    if not connect():
        print("(Could not connect to MT5 — falling back to trade_log.csv)")
        return None

    frames = []
    for symbol in config.SYMBOLS:
        df = get_candles_history(symbol, config.TIMEFRAME, days=days)
        if df is None or len(df) < 500:
            continue
        _, trade_log = run_backtest(df, symbol=symbol, verbose=False)
        closed = trade_log.dropna(subset=['outcome']) if len(trade_log) else trade_log
        if len(closed):
            frames.append(closed)

    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    print(f"Gathered {len(out)} backtest trades over ~{days} days.")
    return out


def gather_from_log():
    """Fall back to the real logged trades."""
    if not os.path.exists(config.TRADE_LOG_PATH):
        return None
    df = pd.read_csv(config.TRADE_LOG_PATH)
    closed = df.dropna(subset=['outcome'])
    print(f"Using {len(closed)} logged trades from trade_log.csv.")
    return closed if len(closed) else None


# ── WALK-FORWARD ─────────────────────────────────────────────

def walk_forward(df, min_train=40, step=15):
    """Expanding-window walk-forward. Returns arrays of out-of-sample
    truth / probability / pnl for every test trade."""
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler

    df = df.reset_index(drop=True)
    if 'time' in df.columns:
        try:
            df = df.sort_values('time').reset_index(drop=True)
        except Exception:
            pass

    # pnl column: backtest uses 'pnl', live log uses 'profit'
    pnl_col = 'pnl' if 'pnl' in df.columns else ('profit' if 'profit' in df.columns else None)

    y_true, y_prob, pnl = [], [], []
    n = len(df)
    if n < min_train + step:
        return None

    start = min_train
    while start < n:
        train = df.iloc[:start]
        test  = df.iloc[start:start + step]
        if len(test) == 0:
            break
        # need both classes present to train a classifier
        if train['outcome'].nunique() < 2:
            start += step
            continue

        scaler = StandardScaler()
        Xtr = scaler.fit_transform(train[FEATURE_COLS].values)
        model = RandomForestClassifier(n_estimators=100, random_state=42,
                                       class_weight='balanced')
        model.fit(Xtr, train['outcome'].values)

        Xte = scaler.transform(test[FEATURE_COLS].values)
        p = model.predict_proba(Xte)[:, 1]

        y_true.extend(test['outcome'].values.tolist())
        y_prob.extend(p.tolist())
        pnl.extend((test[pnl_col].values.tolist() if pnl_col else [np.nan] * len(test)))
        start += step

    return (np.array(y_true), np.array(y_prob), np.array(pnl, dtype=float))


# ── REPORT ───────────────────────────────────────────────────

def _pf(pnls):
    wins = pnls[pnls > 0].sum()
    loss = abs(pnls[pnls < 0].sum())
    return (wins / loss) if loss > 0 else float('inf')


def report(y_true, y_prob, pnl):
    from sklearn.metrics import roc_auc_score, accuracy_score

    thr = config.ML_MIN_CONFIDENCE
    approved = y_prob >= thr
    n = len(y_true)

    print("\n" + "=" * 56)
    print("   WALK-FORWARD (OUT-OF-SAMPLE) ML VALIDATION")
    print("=" * 56)
    print(f"Out-of-sample trades evaluated : {n}")

    try:
        auc = roc_auc_score(y_true, y_prob)
    except Exception:
        auc = float('nan')
    acc = accuracy_score(y_true, (y_prob >= 0.5).astype(int))
    print(f"OOS ROC-AUC (0.50 = coin flip)  : {auc:.3f}")
    print(f"OOS accuracy                    : {acc:.3f}")

    base_wr = y_true.mean() * 100
    print(f"\nBaseline (take EVERY signal)    : {n} trades | win rate {base_wr:.1f}%")
    if not np.isnan(pnl).all():
        print(f"  net pnl {np.nansum(pnl):+.2f} | profit factor {_pf(pnl[~np.isnan(pnl)]):.2f}")

    if approved.sum() == 0:
        print(f"\nML-filtered (>= {thr:.0%})          : 0 trades approved — threshold too high.")
    else:
        fwr = y_true[approved].mean() * 100
        print(f"\nML-filtered (conf >= {thr:.0%})       : {int(approved.sum())} trades | win rate {fwr:.1f}%")
        if not np.isnan(pnl).all():
            ap = pnl[approved]
            ap = ap[~np.isnan(ap)]
            if len(ap):
                print(f"  net pnl {ap.sum():+.2f} | profit factor {_pf(ap):.2f}")

        # Verdict
        print("\n--- VERDICT ---")
        lift = fwr - base_wr
        if np.isnan(auc):
            print("Not enough data for a reliable verdict.")
        elif auc < 0.53:
            print(f"[X] ML is NOT adding edge (AUC {auc:.2f} ~ coin flip). The model is")
            print("    basically guessing. Better to simplify/trust the strategy filters")
            print("    than to rely on this ML gate.")
        elif lift > 2:
            print(f"[OK] ML IS helping - it lifts win rate by {lift:+.1f} pts out-of-sample.")
            print("     Keep it. Consider gathering more data to strengthen it.")
        else:
            print(f"[!] ML is weak - AUC {auc:.2f}, win-rate lift {lift:+.1f} pts.")
            print("    Marginal. Worth improving features before trusting it with size.")
    print("=" * 56)


def main():
    print("Gathering labelled trades for walk-forward validation...")
    df = gather_from_backtest()
    if df is None:
        df = gather_from_log()
    if df is None or len(df) < 55:
        print("\nNot enough labelled trades to validate honestly (need ~55+).")
        print("Run the bot on demo to accumulate trades, or ensure MT5 is connected")
        print("so a fresh backtest can generate them.")
        return

    res = walk_forward(df)
    if res is None:
        print("Not enough data for a walk-forward split.")
        return
    report(*res)


if __name__ == "__main__":
    main()
