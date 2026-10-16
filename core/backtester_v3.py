"""Shared event-driven backtester for v3 (L1 evaluation).

Design rules (the opposite of the old core/backtester.py mistakes):
  * Entry at the OPEN of the bar AFTER the closed signal bar (no
    look-ahead: a signal is only known when its bar closes).
  * BID/ASK MODELLED EXPLICITLY. MT5 bars are BID prices:
      - BUY  enters at ask (bid open + spread), exits on bid; its SL/TP
        barriers trigger on bid low/high.
      - SELL enters at bid, exits at ask; its SL/TP barriers trigger on
        ask (bid high/low + that bar's spread). A SELL therefore stops
        out MORE often and takes profit LESS easily than a naive
        bid-only backtest suggests -- that asymmetry is the point.
    The spread is paid through this price path only; the CostModel
    adds slippage + commission on top (never the spread twice).
  * GAP RULE: a bar that OPENS beyond the stop exits at that open
    (worse than the stop). A bar that opens beyond the target exits at
    the TARGET price (conservative: never book the better open).
  * Intra-bar exit is SL-FIRST when both barriers are touched in one
    bar (pessimistic, matches the documented duel methodology).
  * Triple barrier: take-profit, stop-loss, vertical (time) barrier.
  * SPREAD RESOLUTION (zero-cost leak fixed in P1.1): a bar spread
    <= 0 or missing is UNKNOWN, never free. Resolution order per bar:
    bar spread -> spread-by-hour median for that UTC hour (if supplied)
    -> CostModel.default_spread_price (0.24). Trades whose realised
    spread came from a fallback are counted (result.spread_fallback_
    trades) and logged.
  * One position at a time; a signal arriving while in a trade is
    skipped (counted). SR-style min/max stop-distance rejection from
    main.py's execution path is applied at entry.
  * MFE/MAE tracked per trade in R. Results gross AND net, in R
    (1R = |executable entry - stop|).

Pure function of (bars, signals, cost model): no MT5, no config reads,
no network -- fully reproducible offline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from core.costs import CostModel
from core.signals.base import CandidateSignal

logger = logging.getLogger("BacktesterV3")


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    skipped_in_trade: int = 0
    skipped_bad_stop: int = 0
    spread_fallback_trades: int = 0
    meta: dict = field(default_factory=dict)


def _indexed(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    if "time" in df.columns:
        df = df.set_index(pd.to_datetime(df["time"], utc=True))
        df = df.drop(columns=["time"])
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def resolve_spreads(df: pd.DataFrame, point: float, cost_model: CostModel,
                    spread_by_hour_price: Optional[dict] = None):
    """Per-bar (base_spread_price, source) arrays.

    source in {"bar", "hour_median", "default"}. Base = before stress.
    """
    n = len(df)
    if "spread_points" in df.columns:
        raw = df["spread_points"].to_numpy(float) * float(point)
    else:
        raw = np.full(n, np.nan)
    hours = df.index.hour.to_numpy()
    base = np.empty(n)
    source = np.empty(n, dtype=object)
    for i in range(n):
        r = raw[i]
        if np.isfinite(r) and r > 0:
            base[i], source[i] = float(r), "bar"
            continue
        hv = None
        if spread_by_hour_price:
            hv = spread_by_hour_price.get(int(hours[i]))
        if hv is not None and np.isfinite(float(hv)) and float(hv) > 0:
            base[i], source[i] = float(hv), "hour_median"
        else:
            base[i], source[i] = float(cost_model.default_spread_price), "default"
    return base, source


def run_backtest(bars: pd.DataFrame, signals: list,
                 cost_model: Optional[CostModel] = None,
                 point: float = 0.01,
                 default_time_stop_bars: Optional[int] = None,
                 spread_by_hour_price: Optional[dict] = None) -> BacktestResult:
    """bars: execution-timeframe canonical BID bars.
    signals: CandidateSignal with bar_index referring to `bars` rows."""
    cm = cost_model or CostModel()
    df = _indexed(bars)
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    times = df.index
    n = len(df)

    spread_base, spread_source = resolve_spreads(df, point, cm,
                                                 spread_by_hour_price)
    spread_used = spread_base * float(cm.stress_multiplier)

    trades = []
    busy_until = -1
    skipped_in_trade = 0
    skipped_bad_stop = 0
    fallback_trades = 0

    for sig in sorted(signals, key=lambda s: s.bar_index):
        i = int(sig.bar_index)
        entry_i = i + 1                      # next bar after signal closes
        if entry_i >= n:
            continue
        if entry_i <= busy_until:
            skipped_in_trade += 1
            continue
        d = int(sig.direction)
        entry_bid = float(opens[entry_i])
        entry_exec = entry_bid + float(spread_used[entry_i]) if d == 1 else entry_bid
        stop = float(sig.stop_price)
        risk = abs(entry_exec - stop)
        if risk <= 0:
            skipped_bad_stop += 1
            continue
        atr = float(sig.atr or 0.0)
        if atr > 0:
            min_sl = float(sig.features.get("min_sl_atr", 0.0)) * atr
            max_sl = float(sig.features.get("max_sl_atr", 0.0)) * atr
            if (min_sl and risk < min_sl) or (max_sl and risk > max_sl):
                skipped_bad_stop += 1
                continue
        tp = entry_exec + d * float(sig.target_R) * risk
        max_bars = sig.time_stop_bars or default_time_stop_bars
        last_i = n - 1 if max_bars is None else min(n - 1, entry_i + int(max_bars))

        def close_exec(j):
            """Executable exit price at bar j's close."""
            return float(closes[j]) if d == 1 else float(closes[j] + spread_used[j])

        exit_i, exit_exec, reason = last_i, close_exec(last_i), "EOD"
        mfe = mae = 0.0
        for j in range(entry_i, last_i + 1):
            sp_j = float(spread_used[j])
            if d == 1:   # BUY: barriers on bid
                mfe = max(mfe, highs[j] - entry_exec)
                mae = max(mae, entry_exec - lows[j])
                if opens[j] <= stop:                    # gap through stop
                    exit_i, exit_exec, reason = j, float(opens[j]), "SL"
                    break
                if opens[j] >= tp:                      # gap past target
                    exit_i, exit_exec, reason = j, tp, "TP"
                    break
                if lows[j] <= stop:                     # SL first
                    exit_i, exit_exec, reason = j, stop, "SL"
                    break
                if highs[j] >= tp:
                    exit_i, exit_exec, reason = j, tp, "TP"
                    break
            else:        # SELL: barriers on ask = bid + spread
                ask_high = highs[j] + sp_j
                ask_low = lows[j] + sp_j
                ask_open = opens[j] + sp_j
                mfe = max(mfe, entry_exec - ask_low)
                mae = max(mae, ask_high - entry_exec)
                if ask_open >= stop:                    # gap through stop
                    exit_i, exit_exec, reason = j, float(ask_open), "SL"
                    break
                if ask_open <= tp:                      # gap past target
                    exit_i, exit_exec, reason = j, tp, "TP"
                    break
                if ask_high >= stop:                    # SL first (ask)
                    exit_i, exit_exec, reason = j, stop, "SL"
                    break
                if ask_low <= tp:
                    exit_i, exit_exec, reason = j, tp, "TP"
                    break
            if max_bars is not None and j == last_i:
                exit_i, exit_exec, reason = j, close_exec(j), "TIME"
                break

        # --- cost accounting (spread paid once, through the path) ---
        if d == 1:
            realised_spread = float(spread_used[entry_i])
            realised_base = float(spread_base[entry_i])
            realised_source = str(spread_source[entry_i])
            exit_bid = exit_exec
            gross_price = exit_bid - entry_bid
            exec_pnl = exit_exec - entry_exec
        else:
            realised_spread = float(spread_used[exit_i])
            realised_base = float(spread_base[exit_i])
            realised_source = str(spread_source[exit_i])
            exit_bid = exit_exec - float(spread_used[exit_i])
            gross_price = entry_bid - exit_bid
            exec_pnl = entry_exec - exit_exec
        extra = cm.slippage_commission_price(realised_base)
        cost_price = realised_spread + extra
        net_price = exec_pnl - extra
        if realised_source != "bar":
            fallback_trades += 1
        trades.append({
            "engine": sig.engine, "timeframe": sig.timeframe,
            "direction": d,
            "signal_time": sig.signal_time, "entry_time": times[entry_i],
            "exit_time": times[exit_i],
            "entry": entry_exec, "entry_bid": entry_bid,
            "stop": stop, "tp": tp,
            "exit": exit_exec, "exit_reason": reason,
            "bars_held": int(exit_i - entry_i + 1),
            "risk_price": risk,
            "spread_price": realised_spread,
            "spread_source": realised_source,
            "cost_price": float(cost_price),
            "gross_R": gross_price / risk, "cost_R": cost_price / risk,
            "net_R": net_price / risk,
            "mfe_R": mfe / risk, "mae_R": mae / risk,
            "atr": atr, "level": sig.level,
        })
        busy_until = exit_i

    if trades:
        logger.info("spread fallback (hour-median/default) used on "
                    f"{fallback_trades}/{len(trades)} trades")
    cols = ["engine", "timeframe", "direction", "signal_time", "entry_time",
            "exit_time", "entry", "entry_bid", "stop", "tp", "exit",
            "exit_reason", "bars_held", "risk_price", "spread_price",
            "spread_source", "cost_price", "gross_R", "cost_R", "net_R",
            "mfe_R", "mae_R", "atr", "level"]
    tdf = pd.DataFrame(trades, columns=cols)
    return BacktestResult(trades=tdf, skipped_in_trade=skipped_in_trade,
                          skipped_bad_stop=skipped_bad_stop,
                          spread_fallback_trades=fallback_trades)


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


def summarise_groups(trades: pd.DataFrame, groups) -> dict:
    """{group_label: summarise(subset)} preserving first-seen order."""
    out = {}
    if trades is None or len(trades) == 0:
        return out
    g = pd.Series(groups, index=trades.index)
    for label in dict.fromkeys(g.tolist()):
        out[label] = summarise(trades[g == label])
    return out
