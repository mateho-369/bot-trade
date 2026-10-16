"""P2 research: no-look-ahead engines, baselines, walk-forward bounds,
DSR/PBO on known inputs, plan grid, SR diagnostic funnel."""
import importlib.util
import os

import numpy as np
import pandas as pd
import pytest

from core.data.history import resample_bars
from core.data.synthetic import generate_synthetic_m1
from core.research.controls import random_signals
from core.research.engines import EVENT_GENERATORS
from core.research.features import compute_features
from core.research.plan import combos_for, trial_count
from core.research.stats import deflated_sharpe_ratio, pbo_cscv
from core.research.walkforward import make_folds, run_walkforward
from core.signals.sr_h1 import diagnose_sr_funnel


@pytest.fixture(scope="module")
def frames():
    m1 = generate_synthetic_m1(days=10, seed=7)
    bars = resample_bars(m1, "M15")
    h1 = resample_bars(m1, "H1")
    feat = compute_features(bars, "M15", trend_bars=h1)
    return bars, h1, feat


# ── no look-ahead: truncating the future must not change past signals ─
@pytest.mark.parametrize("engine,variant", [("ORB", 60), ("PULLBACK", False),
                                            ("SMC", False), ("RANGE", 0.15),
                                            ("SQUEEZE", 20)])
def test_engine_no_lookahead(frames, engine, variant):
    bars, h1, feat = frames
    cut = int(len(feat) * 0.6)
    feat_trunc = compute_features(bars.iloc[:cut].copy(), "M15", trend_bars=h1)
    full = EVENT_GENERATORS[engine](feat, "M15", variant)
    trunc = EVENT_GENERATORS[engine](feat_trunc, "M15", variant)
    key = lambda s: (s.bar_index, s.direction, round(s.stop_price, 8))
    full_keys = {key(s) for s in full if s.bar_index < cut - 10}
    trunc_keys = {key(s) for s in trunc if s.bar_index < cut - 10}
    assert trunc_keys == full_keys


def test_engines_produce_signals_on_synthetic(frames):
    _, _, feat = frames
    total = 0
    for name, gen in EVENT_GENERATORS.items():
        variant = {"ORB": 60, "PULLBACK": False, "SMC": False,
                   "RANGE": 0.15, "SQUEEZE": 20}[name]
        sigs = gen(feat, "M15", variant)
        total += len(sigs)
        for s in sigs:
            assert s.stop_price != 0 and s.atr > 0
    assert total > 0


# ── random baseline generator ──────────────────────────────────────────
def test_random_baseline_deterministic_and_long_only(frames):
    bars, _, feat = frames
    a = random_signals(bars, feat, 100, 1.0, 42, np.array([1.0, 1.5]),
                       1.5, 12)
    b = random_signals(bars, feat, 100, 1.0, 42, np.array([1.0, 1.5]),
                       1.5, 12)
    assert [s.bar_index for s in a] == [s.bar_index for s in b]
    assert all(s.direction == 1 for s in a)  # long-only control
    assert len(a) == 100


# ── walk-forward boundaries ────────────────────────────────────────────
def test_fold_boundaries_no_overlap():
    folds = make_folds("2020-01-15", "2026-01-01")
    assert folds
    f0 = folds[0]
    assert f0.train_start == pd.Timestamp("2020-01-01", tz="UTC")
    assert f0.train_end == f0.test_start          # test starts where train ends
    assert f0.test_end > f0.test_start
    for a, b in zip(folds, folds[1:]):
        assert b.train_start >= a.train_start     # rolling forward


def test_walkforward_selects_on_train_only():
    folds = make_folds("2020-01-01", "2023-06-01")
    f0 = folds[0]
    def trades(rows):
        return pd.DataFrame(rows, columns=["entry_time", "net_R"])
    good_train = trades([(f0.train_start + pd.Timedelta(days=30), 1.0)] * 25 +
                        [(f0.test_start + pd.Timedelta(days=5), -5.0)] * 3)
    good_test = trades([(f0.train_start + pd.Timedelta(days=30), -1.0)] * 25 +
                       [(f0.test_start + pd.Timedelta(days=5), 2.0)] * 3)
    out = run_walkforward({"A": good_train, "B": good_test}, folds[:1])
    # A wins on train even though B is better in the test window
    assert out["selected"][0] == "A"
    # a trade exactly at test_start counts as OOS; one at train_end does not
    assert len(out["oos_trades"]) == 3


# ── DSR / PBO on known inputs ──────────────────────────────────────────
def test_dsr_known_inputs():
    rng = np.random.default_rng(0)
    strong = rng.normal(0.15, 1.0, 800)
    noise = rng.normal(0.0, 1.0, 800)
    assert deflated_sharpe_ratio(strong, 10) > 0.90
    assert deflated_sharpe_ratio(noise, 10) < 0.90
    # more trials => harder to be significant
    assert deflated_sharpe_ratio(strong, 100000) < \
        deflated_sharpe_ratio(strong, 10)
    assert deflated_sharpe_ratio([], 10) == 0.0


def test_pbo_dominant_strategy_is_zero():
    M = np.zeros((3, 16))
    M[0, :] = 1.0      # trial 0 best in every block
    M[1, :] = 0.5
    M[2, :] = 0.0
    assert pbo_cscv(M) == 0.0


def test_pbo_overfit_pattern_is_high():
    rng = np.random.default_rng(3)
    M = rng.normal(0, 1, (6, 16))
    assert 0.0 <= pbo_cscv(M) <= 1.0


# ── plan grid ──────────────────────────────────────────────────────────
def test_plan_grid_within_budget():
    for engine in ("ORB", "PULLBACK", "SMC", "RANGE", "SQUEEZE"):
        assert 0 < len(combos_for(engine, "M15")) <= 40
    assert trial_count(["ORB"], ["M5", "M15"]) == 2 * len(combos_for("ORB", "M5"))


# ── SR diagnostic funnel ───────────────────────────────────────────────
def test_sr_diagnose_funnel_on_synthetic():
    m1 = generate_synthetic_m1(days=10, seed=7)
    h1 = resample_bars(m1, "H1")
    h4 = resample_bars(m1, "H4")
    out = diagnose_sr_funnel(h1, h4)
    assert out["first_h4_time"] == h4["time"].iloc[0]
    assert out["first_h1_time"] == h1["time"].iloc[0]
    assert out["warmup_ready_time"] is not None
    assert set(["year", "bars", "session_ok", "signals"]).issubset(
        out["funnel"].columns)


# ── downloader full-history option ─────────────────────────────────────
def test_downloader_full_history_years():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "tools", "download_history.py")
    spec = importlib.util.spec_from_file_location("download_history2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.resolve_start_year("M1", None, None) == 2023
    assert mod.resolve_start_year("M1", None, None, full_history=True) == 2018
    assert mod.resolve_start_year("H1", None, None, full_history=True) == 2010
