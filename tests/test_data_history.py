"""L0 data layer: canonical frames, resample, spread-by-hour, manifest."""
import hashlib
import os

import pandas as pd

from core.data.history import (build_manifest, read_parquet, resample_bars,
                               spread_by_hour, write_parquet)
from core.data.synthetic import generate_synthetic_m1


def test_synthetic_fixture_shape_and_determinism():
    a = generate_synthetic_m1(days=2, seed=7)
    b = generate_synthetic_m1(days=2, seed=7)
    assert len(a) == 2 * 24 * 60
    assert a["close"].equals(b["close"])  # fixed seed => reproducible
    assert str(a["time"].dt.tz) == "UTC"
    assert (a["high"] >= a["low"]).all()


def test_resample_m1_to_h1_ohlc():
    m1 = generate_synthetic_m1(days=1, seed=3)
    h1 = resample_bars(m1, "H1")
    assert len(h1) == 24
    first = m1.iloc[:60]
    assert h1.iloc[0]["open"] == first.iloc[0]["open"]
    assert h1.iloc[0]["high"] == first["high"].max()
    assert h1.iloc[0]["close"] == first.iloc[-1]["close"]
    assert h1.iloc[0]["tick_volume"] == first["tick_volume"].sum()


def test_spread_by_hour_uses_point():
    m1 = generate_synthetic_m1(days=1, seed=3)
    out = spread_by_hour(m1, point=0.01)
    assert set(out["hour_utc"]) == set(range(24))
    london = out[out["hour_utc"] == 10].iloc[0]
    assert abs(london["spread_median"] - 0.24) < 1e-9  # 24 points x 0.01


def test_parquet_roundtrip_and_manifest(tmp_path):
    m1 = generate_synthetic_m1(days=1, seed=5)
    path = str(tmp_path / "XAUUSDm_M1.parquet")
    write_parquet(m1, path)
    back = read_parquet(path)
    assert len(back) == len(m1)
    man = build_manifest(str(tmp_path), "XAUUSDm")
    entry = man["files"][0]
    assert entry["rows"] == len(m1)
    with open(path, "rb") as f:
        assert entry["sha256"] == hashlib.sha256(f.read()).hexdigest()
    assert os.path.exists(tmp_path / "MANIFEST.json")
