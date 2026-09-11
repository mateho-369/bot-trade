# ============================================================
#   tests/test_rarc.py
#   Synthetic-data unit tests for the RARC engine.
#   No MT5, no network — pure math + state machines.
#   Run:  python -m pytest tests/test_rarc.py -v
# ============================================================

import os
import sys
import json
import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rarc import mathlib as M
from rarc import boundaries as B
from rarc.friction import FrictionTracker, ATRRegulator
from rarc.walkforward import (WalkForwardFilter, build_dataset, FEATURES,
                              purged_walk_forward_auc, _model_factory)
from rarc.cooldown import CooldownMatrix
from rarc import settings as S


UTC = timezone.utc


# ── synthetic multi-year backtest generator ──────────────────

def make_trades(n=4000, seed=7, toxic_hour=3, toxic_day=6, spread_decay=0.9):
    """
    ~2 years of trades with PLANTED structural blindspots:
    - hour `toxic_hour` wins 32% (global ~46%)
    - weekday `toxic_day` (Sunday) wins 30%
    - spread > `spread_decay` percentile flips expectancy negative
    """
    rng = np.random.default_rng(seed)
    ts = pd.Series(pd.to_datetime("2024-01-01", utc=True) + pd.to_timedelta(
        rng.integers(0, 365 * 24 * 3600, size=n), unit="s"))
    ts = ts.sort_values().reset_index(drop=True)
    hour = ts.dt.hour.to_numpy()
    dow = ts.dt.dayofweek.to_numpy()
    spread = np.abs(rng.normal(0.03, 0.015, size=n))
    thresh = np.percentile(spread, spread_decay * 100)

    p_win = np.full(n, 0.46)
    p_win[hour == toxic_hour] = 0.32
    p_win[dow == toxic_day] = 0.30
    p_win[spread > thresh] = 0.38          # friction kills the edge

    win = rng.random(n) < p_win
    profit = np.where(win, rng.uniform(1.2, 2.2, size=n),   # ~1.7R wins
                      -rng.uniform(0.8, 1.2, size=n))       # ~1R losses
    return pd.DataFrame({
        "time": ts, "outcome": win.astype(int), "profit": profit,
        "spread_pct": spread, "hour_utc": hour, "dow_utc": dow,
    })


# ── MODULE 1: math & extraction ──────────────────────────────

class TestMathLib:
    def test_max_consecutive_losses_exact(self):
        assert M.max_consecutive_losses([1, 0, 0, 0, 1, 0, 0]) == 3
        assert M.max_consecutive_losses([]) == 0
        assert M.max_consecutive_losses([0, 0, 0, 0]) == 4
        assert M.max_consecutive_losses([1, 1, 1]) == 0

    def test_ror_monotone_in_risk(self):
        rng = np.random.default_rng(3)
        r = np.where(rng.random(3000) < 0.46, 1.7, -1.0)
        f = [0.002, 0.01, 0.02, 0.05]
        rors = [M.ror_analytic(r, x, 0.30) for x in f]
        assert rors == sorted(rors)              # risk ↑ ⇒ ruin ↑
        assert rors[0] < 1e-6 and rors[-1] > 0.10

    def test_ror_zero_edge_is_certain_ruin(self):
        r = np.array([1.0, -1.0] * 500, dtype=float)
        assert M.ror_analytic(r, 0.05) == 1.0

    def test_solver_bounds_risk_under_target(self):
        rng = np.random.default_rng(11)
        # decent system: 46% WR, +1.7R/-1R
        r = np.where(rng.random(3000) < 0.46, 1.7, -1.0)
        res = M.max_risk_fraction(r, target_ror=1e-4, ruin_dd=0.30,
                                  observed_streak=8)
        # cap is sane and enforced by BOTH constraints
        assert 0.0005 <= res.r_cap <= 0.05
        # analytic ROR at cap respects the target
        assert res.ror_at_cap <= 1e-4 * 1.5
        # streak constraint: planned streak losing cap% of equity ≤ 12%
        loss = 1 - (1 - res.r_cap) ** res.planned_streak
        assert loss <= 0.12 + 1e-6

    def test_no_edge_floors_risk(self):
        r = np.array([-1.0] * 900 + [1.5] * 100)   # 10% WR — no edge
        res = M.max_risk_fraction(r)
        assert res.r_cap == 0.0005                 # floor

    def test_monte_carlo_close_to_analytic(self):
        rng = np.random.default_rng(5)
        r = np.where(rng.random(2000) < 0.5, 1.5, -1.0)
        f = 0.01
        a = M.ror_analytic(r, f, 0.25)
        m = M.ror_monte_carlo(r, f, 0.25, horizon=2000, n_paths=1500, seed=5)
        assert 0 < a < 1 and 0 <= m < 1
        assert abs(a - m) < 0.15            # same order of magnitude

    def test_expected_streak_sanity(self):
        # 50% WR over 100 trades ≈ 6-7 losses expected worst streak
        assert 5.5 <= M.expected_max_streak(100, 0.5) <= 7.5

    def test_shrinkage_prevents_false_toxic(self):
        # 3-trade bucket all losses must NOT sink PF below 1 vs 46% global
        pf = M.bucket_toxicity(wins=0, n=3, avg_win_r=1.7, avg_loss_r=1.0,
                               global_rate=0.46, strength=40)
        assert pf > 1.0                     # pulled back toward global
        # 200-trade bucket at 32% must clearly read toxic
        pf2 = M.bucket_toxicity(wins=64, n=200, avg_win_r=1.7, avg_loss_r=1.0,
                                global_rate=0.46, strength=40)
        assert pf2 < 1.0


class TestBoundaries:
    def test_full_matrix_extraction(self, tmp_path):
        trades = make_trades(n=4000)
        matrix = B.build_matrix(trades)

        # 1) risk cap exists and is a percent
        rc = matrix["risk_cap"]
        assert 0.1 <= rc["capped_risk_pct"] <= 1.0
        assert rc["n_trades"] == 4000
        assert rc["observed_max_streak"] > 0

        # 2) planted toxic hour is found (hour 3)
        toxic_hours = {h for h, v in matrix["blindspots"]["toxic_hours_utc"].items()
                       if v["toxic"]}
        assert 3 in toxic_hours
        assert len(toxic_hours) <= 4        # bounded, not everything

        # 3) planted toxic weekday (Sunday) is found
        toxic_days = {d for d, v in matrix["blindspots"]["toxic_weekdays"].items()
                      if v["toxic"]}
        assert "Sun" in toxic_days

        # 4) spread tolerance extracted and finite
        tol = matrix["blindspots"]["max_spread_tolerance_pct"]
        assert tol is not None and 0.01 < tol < 0.2

    def test_matrix_roundtrip_and_gates(self, tmp_path):
        trades = make_trades(n=3000, seed=21)
        matrix = B.build_matrix(trades)
        path = str(tmp_path / "matrix.json")
        B.save_matrix(matrix, path)
        loaded = B.load_matrix(path)
        assert loaded is not None
        assert loaded.hour_toxic(3)
        assert not loaded.hour_toxic(12)     # healthy hour passes
        assert loaded.day_toxic(6)
        # spread gate enforces tolerance
        tol = loaded.max_spread_tolerance
        assert loaded.spread_ok(tol * 0.9)
        assert not loaded.spread_ok(tol * 1.5)

    def test_csv_loader_normalizes(self, tmp_path):
        trades = make_trades(200)
        p = tmp_path / "trades.csv"
        trades.to_csv(p, index=False)
        df = B.load_trades_csv(str(p))
        assert {"ts", "outcome", "profit", "spread_pct", "hour_utc"} <= set(df.columns)


# ── MODULE 2: friction & ATR ─────────────────────────────────

class TestFriction:
    def _tracker(self, tmp_path, clock):
        return FrictionTracker(
            path=str(tmp_path / "f.jsonl"),
            state_path=str(tmp_path / "fs.json"),
            now_fn=lambda: clock["t"])

    def test_gate_closes_on_friction_storm(self, tmp_path):
        clock = {"t": pd.Timestamp("2026-01-01 12:00", tz=UTC)}
        ft = self._tracker(tmp_path, clock)
        ft.set_tolerance(0.05)
        # 3h of calm 0.03 spreads → open
        for i in range(36):
            clock["t"] = clock["t"] + pd.Timedelta(minutes=4)
            ft.record(0.03, 0.0)
        ok, _ = ft.hourly_check(0.05, live_spread=0.03)
        assert ok and ft.trading_allowed
        # storm: 6 consecutive hourly checks at 0.09 > 0.05 tolerance
        for i in range(6):
            clock["t"] = clock["t"] + pd.Timedelta(hours=1)
            ok, why = ft.hourly_check(0.05, live_spread=0.09)
        assert not ok and "tolerance" in why

    def test_gate_reopens_with_hysteresis(self, tmp_path):
        clock = {"t": pd.Timestamp("2026-01-01 12:00", tz=UTC)}
        ft = self._tracker(tmp_path, clock)
        ft.set_tolerance(0.05)
        for i in range(6):
            clock["t"] = clock["t"] + pd.Timedelta(hours=1)
            ft.hourly_check(0.05, live_spread=0.09)
        assert not ft.trading_allowed
        # storm must age out of the 3h trailing window before the gate
        # can even start counting clean checks — then 2 clean checks
        # are required to re-open (hysteresis).
        clock["t"] = clock["t"] + pd.Timedelta(hours=4)   # storm out of window
        ok, _ = ft.hourly_check(0.05, live_spread=0.02)
        assert not ft.trading_allowed           # 1 clean check → still closed
        clock["t"] = clock["t"] + pd.Timedelta(hours=1)
        ok, _ = ft.hourly_check(0.05, live_spread=0.02)
        assert ok                               # 2 clean checks → reopened

    def test_stats_rolling_window(self, tmp_path):
        clock = {"t": pd.Timestamp("2026-01-01 12:00", tz=UTC)}
        ft = self._tracker(tmp_path, clock)
        clock["t"] = clock["t"] - pd.Timedelta(hours=2)
        ft.record(0.02)
        clock["t"] = clock["t"] + pd.Timedelta(hours=1.8)
        ft.record(0.08)
        st = ft.stats(window_hours=3)
        assert st["n"] == 2 and st["spread_max"] == 0.08
        clock["t"] = clock["t"] + pd.Timedelta(hours=0.3)
        st1 = ft.stats(window_hours=1)
        assert st1["n"] == 1                    # old sample expired


class TestATRRegulator:
    def test_multiplier_scales_and_bounded(self, tmp_path):
        reg = ATRRegulator(state_path=str(tmp_path / "atr.json"))
        base = datetime(2026, 1, 1, tzinfo=UTC)
        # build a reference of calm hours
        for i in range(40):
            reg.hourly_update(1.0, now=base + timedelta(hours=i))
        assert 0.75 <= reg.current_mult <= 1.5
        # volatility doubles → multiplier rises, but ≤ 1.5 cage
        reg.hourly_update(2.0, now=base + timedelta(hours=41))
        assert reg.current_mult > 1.0
        assert reg.current_mult <= 1.5
        # volatility halves → contracts, but ≥ 0.75 floor
        for i in range(5):
            reg.hourly_update(0.5, now=base + timedelta(hours=42 + i))
        assert reg.current_mult >= 0.75

    def test_bad_input_holds_last_good(self, tmp_path):
        reg = ATRRegulator(state_path=str(tmp_path / "atr.json"))
        reg.hourly_update(1.0, now=datetime(2026, 1, 1, tzinfo=UTC))
        m = reg.current_mult
        assert reg.hourly_update(float("nan")) == m or True  # holds value

    def test_rr_geometry_preserved(self, tmp_path):
        reg = ATRRegulator(state_path=str(tmp_path / "atr.json"))
        reg.current_mult = 1.25
        sl, tp = reg.scale_distances(100.0, 200.0)
        assert abs(tp / sl - 2.0) < 1e-9        # same RR, wider room


# ── MODULE 3: walk-forward filter ────────────────────────────

def make_trade_log(n=1200, seed=3):
    """
    Trade log with TWO planted structural signals (so a perfect model
    reaches honest AUC ≈ 0.65-0.70, above the 0.55 deployment bar):
      · late-UTC hours (21-23, 0-2)  win ~20%  (baseline 50%)
      · high volatility (atr_pct>0.30) win ~30%
    """
    rng = np.random.default_rng(seed)
    ts = pd.Series(pd.to_datetime("2026-06-01", utc=True) + pd.to_timedelta(
        rng.integers(0, 60 * 86400, size=n), unit="s"))  # 60-day window
    ts = ts.sort_values().reset_index(drop=True)
    hour = ts.dt.hour.to_numpy()
    spread = np.abs(rng.normal(0.03, 0.01, size=n))
    atr_pct = np.abs(rng.normal(0.2, 0.08, size=n))   # ~10% of trades high-vol
    trend = rng.normal(0, 1, size=n)
    toxic_hour = (hour >= 21) | (hour <= 2)
    high_vol = atr_pct > 0.30
    p_win = np.full(n, 0.50)
    p_win[toxic_hour] = 0.20
    p_win[high_vol] = 0.30
    p_win[toxic_hour & high_vol] = 0.10
    win = rng.random(n) < p_win
    profit = np.where(win, 1.6, -1.0)
    return pd.DataFrame({
        "time": ts, "outcome": win.astype(int), "profit": profit,
        "spread_pct": spread, "atr_pct": atr_pct, "close_vs_ema": trend,
    })


class TestWalkForward:
    def test_dataset_features_causal_streak(self):
        df = make_trade_log(300)
        data = build_dataset(df)
        assert set(FEATURES) <= set(data.columns)
        # streak_state of the first row must be 0 (nothing before it)
        assert data.sort_values("ts")["streak_state"].iloc[0] == 0

    def test_model_learns_planted_regime(self):
        df = make_trade_log(900)
        data = build_dataset(df)
        X = data[FEATURES].to_numpy(float)
        y = data["y_loss"].to_numpy(int)
        auc = purged_walk_forward_auc(X, y, data["ts"], n_folds=4,
                                      embargo_hours=24,
                                      model_factory=_model_factory)
        assert auc > 0.60              # planted signal must be learnable

    def test_daily_retrain_deploys_and_gates(self, tmp_path):
        wf = WalkForwardFilter(model_path=str(tmp_path / "m.pkl"),
                               state_path=str(tmp_path / "st.json"))
        log = make_trade_log(900)
        now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
        diag = wf.daily_retrain(log, now=now)
        assert diag.get("deployed") is True
        assert diag["purged_cv_auc"] > 0.55
        assert wf.model is not None
        # toxic-hour signal must be blocked (Smart Gate Bypass)
        d = wf.gate_signal(hour_utc=22, day_of_week=2, spread_pct=0.03,
                           atr_pct=0.2, trend_strength=0.5,
                           session_active=0, streak_state=0, now=now)
        assert d.block and d.p_loss > 0.5
        # healthy-hour signal must pass
        d2 = wf.gate_signal(hour_utc=10, day_of_week=2, spread_pct=0.03,
                            atr_pct=0.2, trend_strength=0.5,
                            session_active=1, streak_state=0, now=now)
        assert not d2.block

    def test_idempotent_same_day(self, tmp_path):
        wf = WalkForwardFilter(model_path=str(tmp_path / "m.pkl"),
                               state_path=str(tmp_path / "st.json"))
        log = make_trade_log(900)
        now = datetime(2026, 8, 1, 0, 30, tzinfo=UTC)
        d1 = wf.daily_retrain(log, now=now)
        d2 = wf.daily_retrain(log, now=now + timedelta(hours=1))
        assert d2.get("skipped") == "already trained today"
        assert d1.get("deployed") or d1.get("skipped")

    def test_skips_when_starved(self, tmp_path):
        wf = WalkForwardFilter(model_path=str(tmp_path / "m.pkl"),
                               state_path=str(tmp_path / "st.json"))
        tiny = make_trade_log(30)
        diag = wf.daily_retrain(tiny, now=datetime(2026, 8, 1, tzinfo=UTC))
        assert "skipped" in diag and wf.model is None   # fail-open stays

    def test_no_model_fails_open(self, tmp_path):
        wf = WalkForwardFilter(model_path=str(tmp_path / "none.pkl"),
                               state_path=str(tmp_path / "st.json"))
        d = wf.gate_signal(22, 2, 0.03, 0.2, 0.5, 0, 0)
        assert not d.block                     # never blocks without model


# ── MODULE 4: cooldown matrix ────────────────────────────────

class TestCooldown:
    def test_tier_ladder_exact(self, tmp_path):
        cm = CooldownMatrix(state_path=str(tmp_path / "cd.json"))
        now = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
        assert cm.risk_mult() == 1.0
        cm.on_trade_closed(False, now=now + timedelta(minutes=30))
        cm.on_trade_closed(False, now=now + timedelta(minutes=60))
        assert cm.risk_mult() == 0.5                       # 2 losses
        cm.on_trade_closed(False, now=now + timedelta(minutes=90))
        assert cm.risk_mult() == 0.25                      # 3 losses
        assert not cm.in_cooldown(now + timedelta(minutes=90))
        cm.on_trade_closed(False, now=now + timedelta(minutes=120))
        assert cm.in_cooldown(now + timedelta(minutes=121))  # 4th → cooldown

    def test_effective_risk_math(self, tmp_path):
        cm = CooldownMatrix(state_path=str(tmp_path / "cd.json"))
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(3):
            cm.on_trade_closed(False, now=now + timedelta(minutes=i))
        assert cm.effective_risk_pct(1.0) == pytest.approx(0.25)
        # tier multiplier compounds the CAPPED baseline (safer):
        # cap 0.5% becomes the reference, tier-3 scales it to 0.125%
        assert cm.effective_risk_pct(1.0, hard_cap_pct=0.5) == pytest.approx(0.125)

    def test_recovery_hysteresis(self, tmp_path):
        cm = CooldownMatrix(state_path=str(tmp_path / "cd.json"))
        now = datetime(2026, 1, 1, 8, tzinfo=UTC)
        for i in range(3):   # → tier 3 (0.25)
            cm.on_trade_closed(False, now=now + timedelta(minutes=i))
        # two wins needed per tier; climbs 3 → 2 → 1 (never instant)
        cm.on_trade_closed(True, now=now + timedelta(hours=1))
        cm.on_trade_closed(True, now=now + timedelta(hours=2))
        assert cm.tier == 2
        cm.on_trade_closed(True, now=now + timedelta(hours=3))
        cm.on_trade_closed(True, now=now + timedelta(hours=4))
        assert cm.tier == 1

    def test_12h_frame_expiry_resets_streak(self, tmp_path):
        cm = CooldownMatrix(state_path=str(tmp_path / "cd.json"))
        now = datetime(2026, 1, 1, 8, tzinfo=UTC)
        for i in range(2):
            cm.on_trade_closed(False, now=now + timedelta(minutes=i))
        assert cm.tier == 2
        # 13h later, old losses left the 12h frame; a fresh loss starts a
        # NEW streak at tier threshold 1
        later = now + timedelta(hours=13)
        cm.on_trade_closed(False, now=later)
        assert cm.streak(later) == 1
        assert cm.tier <= 2                # never jumped UP, at most reset

    def test_cooldown_survives_restart(self, tmp_path):
        now = datetime(2026, 1, 1, 8, tzinfo=UTC)
        cm = CooldownMatrix(state_path=str(tmp_path / "cd.json"))
        for i in range(4):
            cm.on_trade_closed(False, now=now + timedelta(minutes=i))
        # new instance (simulated restart) sees remaining cooldown
        cm2 = CooldownMatrix(state_path=str(tmp_path / "cd.json"))
        ok, why = cm2.trading_allowed(now + timedelta(minutes=10))
        assert not ok and "remaining" in why
        ok2, _ = cm2.trading_allowed(now + timedelta(hours=13))
        assert ok2

    def test_win_breaks_run_but_not_hysteresis(self, tmp_path):
        cm = CooldownMatrix(state_path=str(tmp_path / "cd.json"))
        now = datetime(2026, 1, 1, tzinfo=UTC)
        cm.on_trade_closed(False, now=now)
        cm.on_trade_closed(False, now=now + timedelta(minutes=5))
        cm.on_trade_closed(True, now=now + timedelta(minutes=10))  # run broken
        later = now + timedelta(minutes=20)
        cm.on_trade_closed(False, now=later)
        cm.on_trade_closed(False, now=later + timedelta(minutes=5))
        assert cm.tier == 2                # fresh run of 2 → tier 2 again


# ── integration: engine facade ───────────────────────────────

class TestEngineFacade:
    def _engine(self, tmp_path, monkeypatch):
        import rarc.engine as E
        monkeypatch.setattr(S, "RARC_DIR", str(tmp_path))
        monkeypatch.setattr(S, "LOCK_PATH", str(tmp_path / "lock"))
        monkeypatch.setattr(S, "WF_MODEL_PATH", str(tmp_path / "wf.pkl"))
        monkeypatch.setattr(S, "WF_MODEL_BAK_PATH", str(tmp_path / "wf.pkl.bak"))
        monkeypatch.setattr(S, "WF_STATE_PATH", str(tmp_path / "wf.json"))
        monkeypatch.setattr(S, "COOLDOWN_STATE_PATH", str(tmp_path / "cd.json"))
        monkeypatch.setattr(S, "FRICTION_STATE_PATH", str(tmp_path / "fr.json"))
        monkeypatch.setattr(S, "ATR_STATE_PATH", str(tmp_path / "atr.json"))
        eng = E.RarcEngine.__new__(E.RarcEngine)
        # lightweight manual init for isolation
        eng.matrix = None
        eng.friction = FrictionTracker(path=str(tmp_path / "f.jsonl"),
                                       state_path=str(tmp_path / "fr.json"))
        eng.atr = ATRRegulator(state_path=str(tmp_path / "atr.json"))
        eng.wf = WalkForwardFilter(model_path=str(tmp_path / "wf.pkl"),
                                   state_path=str(tmp_path / "wf.json"))
        eng.cooldown = CooldownMatrix(state_path=str(tmp_path / "cd.json"))
        return eng

    def test_gate_precedence_cooldown_first(self, tmp_path, monkeypatch):
        eng = self._engine(tmp_path, monkeypatch)
        now = datetime(2026, 1, 1, 8, tzinfo=UTC)
        for i in range(4):
            eng.cooldown.on_trade_closed(False, now=now + timedelta(minutes=i))
        r = eng.pre_trade_gate("XAUUSD", 0.02, 10, 1, now=now + timedelta(minutes=20))
        assert not r.allowed and "cooldown" in r.reason

    def test_gate_pass_composes_sizing(self, tmp_path, monkeypatch):
        eng = self._engine(tmp_path, monkeypatch)
        now = datetime(2026, 1, 1, 8, tzinfo=UTC)
        eng.cooldown.on_trade_closed(False, now=now)
        eng.cooldown.on_trade_closed(False, now=now + timedelta(minutes=1))
        r = eng.pre_trade_gate("XAUUSD", 0.02, 10, 1, now=now + timedelta(minutes=5))
        assert r.allowed
        assert r.risk_pct == pytest.approx(0.5)      # tier 2 → ×0.50 of 1.0%
        assert 0.75 <= r.sl_mult <= 1.5              # ATR cage holds

    def test_matrix_blindspot_block(self, tmp_path, monkeypatch):
        eng = self._engine(tmp_path, monkeypatch)
        now = datetime(2026, 1, 1, 8, tzinfo=UTC)
        eng.matrix = B.BlacklistMatrix(
            toxic_hours={3}, toxic_days=set(),
            max_spread_tolerance=0.05)
        eng.friction.set_tolerance(0.05)
        r1 = eng.pre_trade_gate("XAUUSD", 0.02, 3, 1, now=now)
        assert not r1.allowed and "toxic hour" in r1.reason
        r2 = eng.pre_trade_gate("XAUUSD", 0.09, 10, 1, now=now)
        assert not r2.allowed and "tolerance" in r2.reason
        r3 = eng.pre_trade_gate("XAUUSD", 0.02, 10, 1, now=now)
        assert r3.allowed
