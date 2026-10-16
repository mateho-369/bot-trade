"""Canonical market-data handling for v3 (L0).

Canonical bar columns (UTC, tz-aware `time` as a column, not index):
    time, open, high, low, close, tick_volume, spread_points, real_volume
`spread_points` is the MT5 bar spread field (in points). Price spread =
spread_points * symbol point (e.g. XAUUSDm point=0.01, 24 points = $0.24).

Canonical tick columns: time, bid, ask (+ spread_price derived on demand).

Everything here is pure/offline: MT5 fetching lives in
tools/download_history.py (Windows), this module only converts, stores
(Parquet), resamples, summarises spread-by-hour and builds MANIFEST.json.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Iterable, Optional

import numpy as np
import pandas as pd

BAR_COLUMNS = ["time", "open", "high", "low", "close",
               "tick_volume", "spread_points", "real_volume"]
TF_RULE = {"M1": "1min", "M3": "3min", "M5": "5min", "M15": "15min",
           "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1D"}


def ensure_utc(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    t = pd.to_datetime(df["time"], utc=True)
    df["time"] = t
    return df.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def rates_to_frame(rates) -> pd.DataFrame:
    """MT5 copy_rates_* structured array -> canonical bars."""
    df = pd.DataFrame(rates)
    if df.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    for col in ("spread", "real_volume"):
        if col not in df.columns:
            df[col] = 0
    out = pd.DataFrame({
        "time": df["time"],
        "open": df["open"].astype(float), "high": df["high"].astype(float),
        "low": df["low"].astype(float), "close": df["close"].astype(float),
        "tick_volume": df["tick_volume"].astype("int64"),
        "spread_points": df["spread"].astype(float),
        "real_volume": df["real_volume"].astype("int64"),
    })
    return ensure_utc(out)


def ticks_to_frame(ticks) -> pd.DataFrame:
    """MT5 copy_ticks_* structured array -> canonical ticks."""
    df = pd.DataFrame(ticks)
    if df.empty:
        return pd.DataFrame(columns=["time", "bid", "ask", "last", "volume"])
    df["time"] = pd.to_datetime(df["time_msc"] if "time_msc" in df.columns
                                else df["time"], unit="ms" if "time_msc" in df.columns else "s",
                                utc=True)
    out = pd.DataFrame({
        "time": df["time"],
        "bid": df["bid"].astype(float), "ask": df["ask"].astype(float),
        "last": df["last"].astype(float) if "last" in df.columns else np.nan,
        "volume": df["volume"].astype(float) if "volume" in df.columns else 0.0,
    })
    return out.sort_values("time").reset_index(drop=True)


def write_parquet(df: pd.DataFrame, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def read_parquet(path: str) -> pd.DataFrame:
    return ensure_utc(pd.read_parquet(path)) if path.endswith(".parquet") else pd.read_parquet(path)


def resample_bars(bars: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate canonical bars to a higher timeframe (UTC-aligned).

    spread_points of the aggregated bar = median of child bars (a typical,
    not worst-case, spread for that bar); volumes are summed.
    """
    if bars.empty:
        return bars.copy()
    rule = TF_RULE[timeframe]
    df = bars.set_index("time")
    agg = pd.DataFrame({
        "open": df["open"].resample(rule).first(),
        "high": df["high"].resample(rule).max(),
        "low": df["low"].resample(rule).min(),
        "close": df["close"].resample(rule).last(),
        "tick_volume": df["tick_volume"].resample(rule).sum(),
        "spread_points": df["spread_points"].resample(rule).median(),
        "real_volume": df["real_volume"].resample(rule).sum(),
    }).dropna(subset=["open", "close"]).reset_index()
    return ensure_utc(agg)


def spread_by_hour(bars: pd.DataFrame, point: float) -> pd.DataFrame:
    """Median/mean/p95 spread (price units) per UTC hour of day."""
    if bars.empty or point <= 0:
        return pd.DataFrame(columns=["hour_utc", "bars", "spread_median",
                                     "spread_mean", "spread_p95"])
    sp = bars["spread_points"].astype(float) * float(point)
    tmp = pd.DataFrame({"hour_utc": bars["time"].dt.hour, "spread_price": sp})
    g = tmp.groupby("hour_utc")["spread_price"]
    out = pd.DataFrame({
        "bars": g.size(), "spread_median": g.median(),
        "spread_mean": g.mean(), "spread_p95": g.quantile(0.95),
    }).reset_index()
    return out.round(4)


def spread_by_hour_ticks(ticks: pd.DataFrame) -> pd.DataFrame:
    if ticks.empty:
        return pd.DataFrame(columns=["hour_utc", "ticks", "spread_median",
                                     "spread_mean", "spread_p95"])
    tmp = pd.DataFrame({"hour_utc": ticks["time"].dt.hour,
                        "spread_price": ticks["ask"] - ticks["bid"]})
    g = tmp.groupby("hour_utc")["spread_price"]
    return pd.DataFrame({"ticks": g.size(), "spread_median": g.median(),
                         "spread_mean": g.mean(),
                         "spread_p95": g.quantile(0.95)}).reset_index().round(4)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(data_dir: str, symbol: str,
                   extra: Optional[dict] = None) -> dict:
    """MANIFEST.json entries for every Parquet file of `symbol` in data_dir.

    The manifest (rows, date range, sha256) is small and safe to commit;
    the Parquet files themselves stay git-ignored.
    """
    files = []
    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            if not name.endswith(".parquet") or symbol not in name:
                continue
            path = os.path.join(data_dir, name)
            try:
                df = pd.read_parquet(path, columns=["time"])
                tmin = str(pd.to_datetime(df["time"]).min())
                tmax = str(pd.to_datetime(df["time"]).max())
                rows = int(len(df))
            except Exception:
                tmin = tmax = None
                rows = -1
            files.append({"file": name, "rows": rows, "time_min": tmin,
                          "time_max": tmax, "bytes": os.path.getsize(path),
                          "sha256": _sha256(path)})
    manifest = {"symbol": symbol, "files": files}
    if extra:
        manifest.update(extra)
    with open(os.path.join(data_dir, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest
