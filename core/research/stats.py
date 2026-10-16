"""Overfitting statistics for P2: Deflated Sharpe Ratio and PBO (CSCV).

DSR (Bailey & Lopez de Prado): probability that the true Sharpe of the
selected strategy exceeds the expected maximum Sharpe obtainable from
`n_trials` independent zero-edge trials, given sample skew/kurtosis.

PBO (Bailey et al.): with 16 time blocks, for every CSCV split
(8 blocks in-sample / 8 out-of-sample), take the in-sample best trial
and check its out-of-sample rank; PBO = fraction of splits where that
rank is in the bottom half (logit <= 0).
"""
from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p):
    try:
        from scipy.stats import norm
        return float(norm.ppf(p))
    except Exception:  # Acklam approximation fallback
        a = [-3.969683028665376e+01, 2.209460984245205e+02,
             -2.759285984469534e+02, 1.383577518672690e+02,
             -3.066479806614716e+01, 2.506628277459239e+00]
        b = [-5.447609879822406e+01, 1.615858368580409e+02,
             -1.556989798598866e+02, 6.680131188771972e+01,
             -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01,
             -2.400758277161838e+00, -2.549732539343734e+00,
             4.374664141464968e+00, 2.938163982698783e+00]
        d = [7.784695709041462e-03, 3.224671290700398e-01,
             2.445134137142996e+00, 3.754408661907416e+00]
        plow, phigh = 0.02425, 1 - 0.02425
        if p < plow:
            q = math.sqrt(-2 * math.log(p))
            return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                   ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
        if p > phigh:
            q = math.sqrt(-2 * math.log(1 - p))
            return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                    ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5]) * q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def deflated_sharpe_ratio(returns, n_trials: int) -> float:
    """DSR in [0,1] for a per-trade return series (e.g. net_R)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    T = len(r)
    if T < 10 or n_trials < 1:
        return 0.0
    sd = r.std(ddof=1)
    if sd <= 0:
        return 0.0
    sr = r.mean() / sd
    if sr <= 0:
        return 0.0
    g3 = float(((r - r.mean()) ** 3).mean() / sd ** 3)
    g4 = float(((r - r.mean()) ** 4).mean() / sd ** 4)
    # variance of the Sharpe estimator under the null ~ 1/T
    sigma_sr = 1.0 / math.sqrt(T)
    e = math.e
    gamma = 0.5772156649015329
    sr_bench = sigma_sr * ((1 - gamma) * _norm_ppf(1 - 1.0 / max(n_trials, 2))
                           + gamma * _norm_ppf(1 - 1.0 / (max(n_trials, 2) * e)))
    denom = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4.0 * sr * sr))
    z = (sr - sr_bench) * math.sqrt(T - 1) / denom
    return float(min(1.0, max(0.0, _norm_cdf(z))))


def pbo_cscv(block_perf, n_blocks: int = 16) -> float:
    """block_perf: (n_trials, n_blocks) performance per time block
    (e.g. summed net_R). Returns PBO in [0,1]; nan if not computable."""
    M = np.asarray(block_perf, dtype=float)
    if M.ndim != 2 or M.shape[0] < 2 or M.shape[1] < 4 or M.shape[1] % 2:
        return float("nan")
    B = M.shape[1]
    half = B // 2
    blocks = list(range(B))
    below = 0
    total = 0
    for is_blocks in itertools.combinations(blocks, half):
        is_set = set(is_blocks)
        oos_blocks = [b for b in blocks if b not in is_set]
        is_perf = M[:, list(is_blocks)].mean(axis=1)
        oos_perf = M[:, oos_blocks].mean(axis=1)
        best = int(np.argmax(is_perf))
        # relative rank of the IS winner out-of-sample, in (0,1)
        rank = (np.sum(oos_perf < oos_perf[best])
                + 0.5 * np.sum(oos_perf == oos_perf[best])) / M.shape[0]
        total += 1
        if rank <= 0.5:
            below += 1
    return below / total if total else float("nan")


def block_sums(entry_times, values, n_blocks: int = 16,
               start=None, end=None) -> np.ndarray:
    """Sum `values` into n_blocks equal time blocks between start/end."""
    t = pd.to_datetime(entry_times)
    v = np.asarray(values, dtype=float)
    if len(t) == 0:
        return np.zeros(n_blocks)
    start = t.min() if start is None else pd.to_datetime(start)
    end = t.max() if end is None else pd.to_datetime(end)
    edges = pd.date_range(start, end, periods=n_blocks + 1)
    idx = np.clip(np.searchsorted(edges.values, t.values, side="right") - 1,
                  0, n_blocks - 1)
    out = np.zeros(n_blocks)
    np.add.at(out, idx, v)
    return out
