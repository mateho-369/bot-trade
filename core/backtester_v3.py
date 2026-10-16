"""Shared event-driven backtester for v3 (L1 evaluation).

Design rules (the opposite of the old core/backtester.py mistakes):
  * Entry at the OPEN of the bar AFTER the closed signal bar (no
    look-ahead: a signal is only known when its bar closes).
  * Intra-bar exit is SL-FIRST when both barriers are touched in one
    bar (pessimistic, matches the documented duel methodology).
  * Triple barrier: take-profit, stop-loss, vertical (time) barrier.
  * Every trade is charged the L0 cost model (spread at entry bar +
    slippage + commission, optionally stressed x1.5) and reported
    gross AND net, in R (1R = |entry - stop|) and price units.
  * One position at a time; a signal arriving while in a trade is
    skipped (counted). SR_H1 min/max stop-distance rejection from
    main.py's execution path is applied at entry when the signal
    carries atr + min/max limits in `features` (see engines).
  * MFE/MAE tracked per trade in R.

Pure function of (bars, signals, cost model): no MT5, no config reads,
no network -- fully reproducible offline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from core.costs import CostModel
from core.signals.base import CandidateSignal


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    skipped_in_trade: int = 0
    skipped_bad_stop: int = 0
    meta: dict = field(default_factory=dict)


def _indexed(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    if "time" in df.columns:
        df = df.set_index(pd.to_datetime(df["time"], utc=True))
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def run_backtest(bars: pd.DataFrame, signals: list,
                 cost_model: Optional[CostModel] = None,
                 point: float = 0.01,
                 default_time_stop_bars: Optional[int] = None) -> BacktestResult:
    """bars: execution-timeframe canonical bars. signals: CandidateSignal
    with bar_index referring to `bars` row position."""
    cm = cost_model or CostModel()
    df = _indexed(bars)
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    spreads_pts = (df["spread_points"].to_numpy(float)
                   if "spread_points" in df.columns
                   else np.zeros(len(df)))
    times = df.index
    n = len(df)

    trades = []
    busy_until = -1
    skipped_in_trade = 0
    skipped_bad_stop = 0

    for sig in sorted(signals, key=lambda s: s.bar_index):
        i = int(sig.bar_index)
        entry_i = i + 1                      # next bar after signal closes
        if entry_i >= n:
            continue
        if entry_i <= busy_until:
            skipped_in_trade += 1
            continue
        d = int(sig.direction)
        entry = float(opens[entry_i])
        stop = float(sig.stop_price)
        risk = abs(entry - stop)
        if risk <= 0:
            skipped_bad_stop += 1
            continue
        # SR-style stop-distance sanity (main.py execution path)
        atr = float(sig.atr or 0.0)
        if atr > 0:
            min_sl = float(sig.features.get("min_sl_atr", 0.0)) * atr
            max_sl = float(sig.features.get("max_sl_atr", 0.0)) * atr
            if (min_sl and risk < min_sl) or (max_sl and risk > max_sl):
                skipped_bad_stop += 1
                continue
        tp = entry + d * float(sig.target_R) * risk
        max_bars = sig.time_stop_bars or default_time_stop_bars
        last_i = n - 1 if max_bars is None else min(n - 1, entry_i + int(max_bars))

        exit_i, exit_price, reason = last_i, float(closes[last_i]), "EOD"
        mfe = mae = 0.0
        for j in range(entry_i, last_i + 1):
            # excursions in price units, direction-adjusted
            fav = (highs[j] - entry) if d == 1 else (entry - lows[j])
            adv = (entry - lows[j]) if d == 1 else (highs[j] - entry)
            mfe = max(mfe, fav)
            mae = max(mae, adv)
            hit_sl = (lows[j] <= stop) if d == 1 else (highs[j] >= stop)
            hit_tp = (highs[j] >= tp) if d == 1 else (lows[j] <= tp)
            if hit_sl:                       # SL first: pessimistic
                exit_i, exit_price, reason = j, stop, "SL"
                break
            if hit_tp:
                exit_i, exit_price, reason = j, tp, "TP"
                break
            if max_bars is not None and j == last_i:
                exit_i, exit_price, reason = j, float(closes[j]), "TIME"
                break

        gross_price = (exit_price - entry) * d
        cost_price = cm.round_trip_cost_price(spreads_pts[entry_i] * point)
        net_price = gross_price - cost_price
        trades.append({
            "engine": sig.engine, "timeframe": sig.timeframe,
            "direction": d,
            "signal_time": sig.signal_time, "entry_time": times[entry_i],
            "exit_time": times[exit_i],
            "entry": entry, "stop": stop, "tp": tp,
            "exit": exit_price, "exit_reason": reason,
            "bars_held": int(exit_i - entry_i + 1),
            "risk_price": risk,
            "spread_price": float(spreads_pts[entry_i] * point),
            "cost_price": float(cost_price),
            "gross_R": gross_price / risk, "cost_R": cost_price / risk,
            "net_R": net_price / risk,
            "mfe_R": mfe / risk, "mae_R": mae / risk,
            "atr": atr, "level": sig.level,
        })
        busy_until = exit_i

    cols = ["engine", "timeframe", "direction", "signal_time", "entry_time",
            "exit_time", "entry", "stop", "tp", "exit", "exit_reason",
            "bars_held", "risk_price", "spread_price", "cost_price",
            "gross_R", "cost_R", "net_R", "mfe_R", "mae_R", "atr", "level"]
    tdf = pd.DataFrame(trades, columns=cols)
    return BacktestResult(trades=tdf, skipped_in_trade=skipped_in_trade,
                          skipped_bad_stop=skipped_bad_stop)


def summarise(trades: pd.DataFrame, seed: int = 42,
              n_boot: int = 2000) -> dict:
    """Headline metrics, NET and gross, with a bootstrap 95% CI on the
    net expectancy (per-trade net_R). Deterministic for a fixed seed."""
    if trades is None or len(trades) == 0:
        return {"trades": 0, "win_rate": 0.0, "profit_factor_net": 0.0,
                "expectancy_R_net": 0.0, "expectancy_R_gross": 0.0,
                "expectancy_ci95": (0.0, 0.0), "max_drawdown_R": 0.0,
                "total_net_R": 0.0, "total_cost_R": 0.0,
                "cost_share_of_gross_profit_pct": 0.0, "trades_per_day": 0.0}
    net = trades["net_R"].to_numpy(float)
    gross = trades["gross_R"].to_numpy(float)
    wins = net[net > 0]
    losses = net[net < 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 \
        else (float("inf") if len(wins) else 0.0)
    equity = np.cumsum(net)
    dd = float((equity - np.maximum.accumulate(equity)).min()) if len(equity) else 0.0
    rng = np.random.default_rng(seed)
    if len(net) >= 2 and n_boot > 0:
        means = rng.choice(net, size=(n_boot, len(net)), replace=True).mean(axis=1)
        lo, hi = float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))
    else:
        lo = hi = float(net.mean()) if len(net) else 0.0
    span_days = 0.0
    if "entry_time" in trades and len(trades) > 1:
        span = (pd.to_datetime(trades["entry_time"]).max()
                - pd.to_datetime(trades["entry_time"]).min())
        span_days = max(span.total_seconds() / 86400.0, 1e-9)
    gross_profit = float(gross[gross > 0].sum())
    total_cost = float(trades["cost_R"].sum())
    return {
        "trades": int(len(trades)),
        "win_rate": float((net > 0).mean()),
        "profit_factor_net": pf,
        "expectancy_R_net": float(net.mean()),
        "expectancy_R_gross": float(gross.mean()),
        "expectancy_ci95": (lo, hi),
        "max_drawdown_R": abs(dd),
        "total_net_R": float(net.sum()),
        "total_cost_R": total_cost,
        "cost_share_of_gross_profit_pct":
            float(100.0 * total_cost / gross_profit) if gross_profit > 0 else 0.0,
        "trades_per_day": float(len(trades) / span_days) if span_days else 0.0,
    }
