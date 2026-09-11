# ============================================================
#   rarc_demo.py
#   End-to-end demonstration of the Regime Adaptation & Risk
#   Correction Engine on SYNTHETIC data — no MT5, no network.
#
#   Run:  python rarc_demo.py
#
#   This is also the reference for how each module is driven:
#     Module 1 → boundaries.build_matrix()      (offline, multi-year)
#     Module 2 → FrictionTracker + ATRRegulator (hourly)
#     Module 3 → WalkForwardFilter.daily_retrain + gate_signal
#     Module 4 → CooldownMatrix.on_trade_closed + effective_risk_pct
# ============================================================

import os
import sys
import shutil
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rarc import settings as S
from rarc.boundaries import build_matrix, save_matrix, load_matrix
from rarc.friction import FrictionTracker, ATRRegulator
from rarc.walkforward import WalkForwardFilter
from rarc.cooldown import CooldownMatrix
from rarc.store import atomic_write_json
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# Isolated demo state so the demo never pollutes the bot's live data.
DEMO_DIR = "demo_rarc_state"


def banner(t):
    print("\n" + "=" * 64)
    print(f"  {t}")
    print("=" * 64)


# ── synthetic multi-year backtest trades (2 years, XAUUSD-like) ──

def gen_multi_year(n=6000, seed=7):
    """
    Planted structural reality:
      · base win rate 46%, avg win +1.7R, avg loss -1.0R
      · toxic hours: 3 (rollover), 13 (US news) → win 28%
      · toxic weekday: Sunday → win 28%
      · spread > 90th percentile → win 36% (friction decay)
    """
    rng = np.random.default_rng(seed)
    ts = pd.Series(pd.to_datetime("2024-01-01", utc=True)
                   + pd.to_timedelta(rng.integers(0, 730 * 86400, size=n), unit="s"))
    ts = ts.sort_values().reset_index(drop=True)
    hour, dow = ts.dt.hour.to_numpy(), ts.dt.dayofweek.to_numpy()
    spread = np.abs(rng.normal(0.035, 0.015, size=n))
    thresh = np.percentile(spread, 90)
    p = np.full(n, 0.46)
    p[spread > thresh] = 0.36
    p[dow == 6] = 0.28
    p[np.isin(hour, [3, 13])] = 0.28
    win = rng.random(n) < p
    profit = np.where(win, rng.uniform(1.2, 2.2, n), -rng.uniform(0.8, 1.2, n))
    return pd.DataFrame({"time": ts, "outcome": win.astype(int),
                         "profit": profit, "spread_pct": spread,
                         "hour_utc": hour, "dow_utc": dow})


def main():
    shutil.rmtree(DEMO_DIR, ignore_errors=True)
    os.makedirs(DEMO_DIR, exist_ok=True)
    now = datetime.now(UTC)

    # ══════════════ MODULE 1 ══════════════
    banner("MODULE 1 — Historical Risk Boundary & Blindspot Extraction")
    trades = gen_multi_year()
    print(f"Parsed {len(trades):,} historical trades "
          f"({trades['time'].min():%Y-%m} → {trades['time'].max():%Y-%m})")

    matrix = build_matrix(trades)
    atomic_write_json(os.path.join(DEMO_DIR, "risk_blacklist_matrix.json"), matrix)
    rc, b = matrix["risk_cap"], matrix["blindspots"]

    print(f"\n  Worst observed loss streak : {rc['observed_max_streak']} "
          f"(i.i.d. expectation ≈ {rc['expected_max_streak_iid']})")
    print(f"  Planned streak (+1.5x, +2): {rc['planned_max_streak']} losses")
    print(f"  Edge measured             : expectancy {rc['expectancy_r']:+.3f}R, "
          f"WR {rc['win_rate']:.1%}")
    print(f"  ROR at cap (analytic)     : {rc['ror_at_cap_analytic']:.2e} "
          f"(target ≤ {S.TARGET_RISK_OF_RUIN:.0%})")
    print(f"  ROR at cap (Monte Carlo)  : {rc['ror_at_cap_montecarlo']:.2e}")
    print(f"  ⇒ Risk_Per_Trade capped   : {rc['baseline_risk_pct']}% → "
          f"{rc['capped_risk_pct']}%")

    toxic_hours = sorted(int(h) for h, v in b["toxic_hours_utc"].items() if v["toxic"])
    toxic_days = [d for d, v in b["toxic_weekdays"].items() if v["toxic"]]
    print(f"\n  Toxic hours (UTC)         : {toxic_hours}  ← planted [3, 13]")
    print(f"  Toxic weekdays            : {toxic_days}  ← planted ['Sun']")
    print(f"  Max spread tolerance      : {b['max_spread_tolerance_pct']}%  "
          f"(PF decays < 1.0 beyond this)")
    m = load_matrix(os.path.join(DEMO_DIR, "risk_blacklist_matrix.json"))
    print(f"  Matrix loads back cleanly : toxic_hours={sorted(m.toxic_hours)}, "
          f"days={sorted(m.toxic_days)}")

    # ══════════════ MODULE 2 ══════════════
    banner("MODULE 2 — Hourly Friction & Volatility Tracker")
    clock = {"t": pd.Timestamp("2026-09-01 00:00", tz=UTC)}
    ft = FrictionTracker(path=os.path.join(DEMO_DIR, "f.jsonl"),
                         state_path=os.path.join(DEMO_DIR, "fs.json"),
                         now_fn=lambda: clock["t"])
    ft.set_tolerance(m.max_spread_tolerance)
    print(f"Spread tolerance from Module 1: {ft._tolerance:.4f}%")

    calm = 0.020
    for day in range(2):                      # two calm days → gate open
        for h in range(24):
            clock["t"] += pd.Timedelta(hours=1)
            ft.hourly_check(m.max_spread_tolerance, live_spread=calm)
    print(f"After 48h calm (spread {calm}%): trading_allowed = "
          f"{ft.trading_allowed}")

    for h in range(3):                        # friction storm
        clock["t"] += pd.Timedelta(hours=1)
        ok, why = ft.hourly_check(m.max_spread_tolerance, live_spread=0.12)
    print(f"During spread storm (0.12%)  : trading_allowed = {ok}")
    print(f"  gate reason: '{ft.block_reason}'")
    st = ft.stats()
    print(f"  trailing {st['window_hours']}h stats: p50={st['spread_p50']} "
          f"p95={st['spread_p95']} max={st['spread_max']} (n={st['n']})")

    reg = ATRRegulator(state_path=os.path.join(DEMO_DIR, "atr.json"))
    for h in range(40):
        reg.hourly_update(2.4, now=(now - timedelta(hours=40 - h)))  # calm ref
    reg.hourly_update(4.1, now=now)           # volatility expands
    print(f"ATR regulator: calm ATR 2.4 → live 4.1 ⇒ SL/TP mult "
          f"{reg.current_mult:.2f}× (cage {S.ATR_MULT_MIN}-{S.ATR_MULT_MAX})")
    sl, tp = reg.scale_distances(5.0, 10.0)
    print(f"  SL 5.0 → {sl:.2f}, TP 10.0 → {tp:.2f} (RR preserved)")

    # ══════════════ MODULE 3 ══════════════
    banner("MODULE 3 — Daily Rolling Walk-Forward ML Filter")
    # 45-day rolling window with planted structure (validated in tests):
    #   hours 21-23 & 0-2 win ~20%, high-vol (atr>0.30) wins ~30%
    rng = np.random.default_rng(3)
    n = 1200
    wts = pd.Series(pd.to_datetime(now) - pd.to_timedelta(
        rng.integers(0, 45 * 86400, size=n), unit="s")).sort_values().reset_index(drop=True)
    wh = wts.dt.hour.to_numpy()
    w_atr = np.abs(rng.normal(0.2, 0.08, size=n))
    w_spread = np.abs(rng.normal(0.035, 0.012, size=n))
    trend = rng.normal(0, 1, n)
    toxic = (wh >= 21) | (wh <= 2)
    high_vol = w_atr > 0.30
    p = np.full(n, 0.50)
    p[toxic] = 0.20
    p[high_vol] = 0.30
    p[toxic & high_vol] = 0.10
    win = rng.random(n) < p
    wlog = pd.DataFrame({
        "time": wts, "outcome": win.astype(int), "profit": np.where(win, 1.6, -1.0),
        "spread_pct": w_spread, "atr_pct": w_atr, "close_vs_ema": trend,
    })
    wf = WalkForwardFilter(model_path=os.path.join(DEMO_DIR, "wf.pkl"),
                           state_path=os.path.join(DEMO_DIR, "wf.json"))
    diag = wf.daily_retrain(wlog, now=now)
    print(f"Retrain @{now:%Y-%m-%d %H:%M} UTC on last {diag.get('window_days')}d: "
          f"{diag.get('closed_trades')} closed trades")
    print(f"  honest purged+embargoed CV AUC : {diag.get('purged_cv_auc')}")
    print(f"  deployment decision            : "
          f"{'DEPLOYED' if diag.get('deployed') else 'KEPT OLD (' + diag.get('reason', '') + ')'}")

    if wf.model is not None:
        d_block = wf.gate_signal(hour_utc=23, day_of_week=3, spread_pct=0.04,
                                 atr_pct=0.22, trend_strength=0.6,
                                 session_active=0, streak_state=0, now=now)
        print(f"\n  Signal @ 23:00 UTC            → block={d_block.block} "
              f"(p_loss={d_block.p_loss:.2f}) {d_block.reason}")
        d_ok = wf.gate_signal(hour_utc=10, day_of_week=3, spread_pct=0.03,
                              atr_pct=0.20, trend_strength=0.6,
                              session_active=1, streak_state=0, now=now)
        print(f"  Signal @ 10:00 UTC (session)  → block={d_ok.block} "
              f"(p_loss={d_ok.p_loss:.2f})")

    # ══════════════ MODULE 4 ══════════════
    banner("MODULE 4 — Automated Self-Correction / Cooldown Matrix")
    cd = CooldownMatrix(state_path=os.path.join(DEMO_DIR, "cd.json"))
    t0 = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)

    def feed(res, mins):
        ts = t0 + timedelta(minutes=mins)
        cd.on_trade_closed(win=(res == "W"), now=ts)
        risk = cd.effective_risk_pct(1.0, hard_cap_pct=rc["capped_risk_pct"], now=ts)
        print(f"{mins:>7} | {res:>3} | {cd.tier:>4}  {cd.risk_mult():.2f}  "
              f"{risk:>5.2f}%   {cd.action()}")

    def probe(mins):
        ok, why = cd.trading_allowed(t0 + timedelta(minutes=mins))
        print(f"  entry attempt t+{mins}m : allowed={ok} {('(' + why + ')') if why else ''}")

    print("— descent: 4 live losses inside the 12h frame —")
    print(f"{'t(min)':>7} | {'res':>3} | tier  mult   risk%    action")
    feed("L", 0); feed("L", 30); feed("L", 60); feed("L", 95)
    probe(100)
    probe(800)     # cooldown runs t+95m → t+815m
    print("— cooldown expired at t+815m; recovery climbs ONE tier per 2 trades —")
    probe(820)
    feed("W", 880); feed("W", 910); feed("W", 940); feed("W", 970)

    banner("SUMMARY")
    cd_open, _ = cd.trading_allowed(datetime.now(UTC))
    print(f"""The full safety stack, in gate order:
  1. Cooldown hard gate      : {'ACTIVE (tier %d)' % cd.tier if not cd_open else 'open'}
  2. Friction gate           : {'CLOSED' if not ft.trading_allowed else 'open'}
  3. Blindspot matrix        : {len(m.toxic_hours)} toxic hours, {len(m.toxic_days)} toxic days,
                               spread ≤ {m.max_spread_tolerance}
  4. Smart Gate (ML)         : {'deployed' if wf.model is not None else 'fail-open (no model)'}
  Sizing authority           : {rc['capped_risk_pct']}% hard cap (ROR < 0.01% verified),
                               × cooldown tier multiplier, floor {S.RISK_PCT_FLOOR}%.
State persisted in ./{DEMO_DIR}/ — delete freely.""")


if __name__ == "__main__":
    main()
