"""Mandatory random-entry controls for P2 (see research/P2_plan.md).

Random signals matched to an engine's OOS trade count and BUY/SELL mix,
with stop distances sampled from the engine's own risk/ATR distribution
and the same target_R / time-stop / backtester / cost model. If an
engine cannot beat the 95th percentile of random entries (and of
random LONG-only entries, gold having tripled since 2018), its "edge"
is indistinguishable from luck and beta.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.backtester_v3 import run_backtest, summarise
from core.signals.base import CandidateSignal


def random_signals(bars: pd.DataFrame, feat: pd.DataFrame, n: int,
                   buy_fraction: float, seed: int,
                   risk_atr_samples, target_R: float,
                   time_stop_bars, warmup: int = 210, allowed_hours=None):
    rng = np.random.default_rng(seed)
    times = pd.to_datetime(feat["time"])
    atr = feat["atr"].to_numpy(float)
    close = feat["close"].to_numpy(float)
    hi_i = len(feat) - 2
    if hi_i <= warmup or n <= 0:
        return []
    pool = np.arange(warmup, hi_i)
    if allowed_hours is not None and "hour_utc" in feat.columns:
        hrs = feat["hour_utc"].to_numpy(int)
        pool = pool[np.isin(hrs[pool], list(allowed_hours))]
    if len(pool) == 0:
        return []
    idx = np.sort(rng.choice(pool, size=int(n), replace=True))
    risks = np.asarray(risk_atr_samples, dtype=float)
    risks = risks[np.isfinite(risks) & (risks > 0)]
    if len(risks) == 0:
        risks = np.array([1.0])
    sigs = []
    for i in idx:
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        direction = 1 if rng.random() < buy_fraction else -1
        risk = float(rng.choice(risks)) * float(atr[i])
        stop = close[i] - direction * risk
        sigs.append(CandidateSignal(
            engine="RANDOM", timeframe="?", direction=int(direction),
            bar_index=int(i), signal_time=times.iloc[i],
            stop_price=float(stop), target_R=float(target_R),
            time_stop_bars=time_stop_bars, atr=float(atr[i]),
            features={"hour_utc": int(times.iloc[i].hour), "h1_trend": 0}))
    return sigs


def random_baseline(bars, feat, n_target: int, buy_fraction: float,
                    seeds, risk_atr_samples, target_R: float,
                    time_stop_bars, cost_model, point: float,
                    spread_by_hour_price=None, allowed_hours=None) -> dict:
    """Distribution of per-seed net expectancy over `seeds` runs."""
    exps = []
    for seed in seeds:
        sigs = random_signals(bars, feat, n_target, buy_fraction, int(seed),
                              risk_atr_samples, target_R, time_stop_bars,
                              allowed_hours=allowed_hours)
        if not sigs:
            continue
        res = run_backtest(bars, sigs, cost_model=cost_model, point=point,
                           spread_by_hour_price=spread_by_hour_price)
        if len(res.trades):
            exps.append(float(res.trades["net_R"].mean()))
    if not exps:
        return {"seeds_used": 0, "expectancy_mean": 0.0, "pct95": 0.0,
                "expectancies": []}
    arr = np.array(exps)
    return {"seeds_used": int(len(arr)), "expectancy_mean": float(arr.mean()),
            "pct95": float(np.percentile(arr, 95)), "expectancies": exps}
