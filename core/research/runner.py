"""P2 runner: plan -> events -> combos -> walk-forward -> leaderboard.

Performance notes: features are computed once per timeframe and cached
to Parquet under data/cache/; each engine's base events are generated
once per primary variant; every combo then just filters events and
runs the (trade-scanning) backtester. Signals/features are causal, so
walk-forward selection partitions full-sample trades by entry time
(see core/research/walkforward.py for the exact rules).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import replace

import numpy as np
import pandas as pd

from core.backtester_v3 import run_backtest, summarise, summarise_groups
from core.costs import CostModel
from core.data.history import read_parquet, resample_bars, spread_by_hour
from core.research import engines as eng
from core.research.controls import random_baseline
from core.research.features import compute_features
from core.research.plan import ENGINE_SPECS, combos_for, trial_count
from core.research.stats import block_sums, deflated_sharpe_ratio, pbo_cscv
from core.research.walkforward import make_folds, run_walkforward

MAX_DD_LIMIT_R = 25.0


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_bars(data_dir, symbol, tf, fixture_m1=None):
    path = os.path.join(data_dir, f"{symbol}_{tf}.parquet")
    if os.path.exists(path):
        return read_parquet(path), path
    if fixture_m1 is not None:
        return resample_bars(fixture_m1, tf), None
    return None, None


def _features_cached(data_dir, symbol, tf, bars, src_path, trend_bars):
    cache_dir = os.path.join(data_dir, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    cpath = os.path.join(cache_dir, f"features_{symbol}_{tf}.parquet")
    mpath = cpath + ".meta.json"
    key = _sha(src_path) if src_path else f"fixture:{len(bars)}:{bars['time'].min()}"
    if os.path.exists(cpath) and os.path.exists(mpath):
        try:
            if json.load(open(mpath))["source_sha"] == key:
                return read_parquet(cpath)
        except Exception:
            pass
    feat = compute_features(bars, tf, trend_bars=trend_bars)
    feat.to_parquet(cpath, index=False)
    json.dump({"source_sha": key}, open(mpath, "w"))
    return feat


def _spread_by_hour_map(bars, point):
    if bars is None or "spread_points" not in bars.columns:
        return None
    sbh = spread_by_hour(bars, point)
    if sbh.empty:
        return None
    return {int(r.hour_utc): float(r.spread_median) for r in sbh.itertuples()}


def _perturbation_pf(bars, signals, combo, factor, cm, point, sbh):
    if not signals:
        return 0.0
    pert = [replace(s, target_R=max(0.5, s.target_R * factor)) for s in signals]
    res = run_backtest(bars, pert, cost_model=cm, point=point,
                       spread_by_hour_price=sbh)
    return summarise(res.trades)["profit_factor_net"]


def run_p2(data_dir, symbol="XAUUSDm", engines=("ORB", "PULLBACK", "SMC",
           "RANGE", "SQUEEZE"), timeframes=("M5", "M15", "H1"),
           start=None, end=None, fixture_path=None, baseline_seeds=200,
           out_md="research/P2_leaderboard.md", trades_dir="data/research",
           progress=print) -> dict:
    t_start = time.time()
    fixture_m1 = read_parquet(fixture_path) if fixture_path else None

    h1_bars, _ = _load_bars(data_dir, symbol, "H1", fixture_m1)
    if h1_bars is None:
        raise FileNotFoundError("no H1 bars (needed as trend context); "
                                "run tools/download_history.py first")
    point = 0.001
    info_path = os.path.join(data_dir, f"{symbol}_symbol_info.json")
    if os.path.exists(info_path):
        point = float(json.load(open(info_path)).get("point") or point)
    cm = CostModel()
    total_trials = trial_count(list(engines), list(timeframes))

    units = [(e, tf) for e in engines for tf in timeframes]
    rows = []
    all_oos = {}
    for ui, (engine, tf) in enumerate(units):
        elapsed = time.time() - t_start
        eta = (elapsed / (ui + 1) * len(units) - elapsed) if ui else 0
        progress(f"[{ui+1}/{len(units)}] {engine} {tf} "
                 f"(elapsed {elapsed/60:.1f}m, ETA ~{eta/60:.1f}m)")
        bars, src = _load_bars(data_dir, symbol, tf, fixture_m1)
        if bars is None:
            rows.append({"engine": engine, "tf": tf, "status": "NO DATA"})
            continue
        if start:
            bars = bars[bars["time"] >= pd.Timestamp(start, tz="UTC")]
        if end:
            bars = bars[bars["time"] <= pd.Timestamp(end, tz="UTC")]
        bars = bars.reset_index(drop=True)
        sbh = _spread_by_hour_map(bars, point)
        feat = _features_cached(data_dir, symbol, tf, bars, src, h1_bars)

        combos = combos_for(engine, tf)
        # base events once per primary variant
        variants = ENGINE_SPECS[engine][0]
        events = {v: eng.EVENT_GENERATORS[engine](feat, tf, v) for v in variants}
        combo_trades = {}
        combo_objs = {}
        for combo in combos:
            combo_objs[combo.combo_id] = combo
            sigs = eng.apply_combo(events[combo.variant], combo, tf)
            res = run_backtest(bars, sigs, cost_model=cm, point=point,
                               spread_by_hour_price=sbh)
            combo_trades[combo.combo_id] = res.trades

        folds = make_folds(bars["time"].min(), bars["time"].max())
        wf = run_walkforward(combo_trades, folds)
        oos = wf["oos_trades"]
        summ = summarise(oos) if len(oos) else summarise(pd.DataFrame())
        selected_ids = [c for c in wf["selected"].values() if c]
        modal = max(set(selected_ids), key=selected_ids.count) if selected_ids else None

        # stress x1.5 on the OOS trades (rerun modal-combo style: rerun all
        # selected combos stressed, then re-pick each fold's winner trades)
        stress_summ = {"profit_factor_net": 0.0}
        if modal:
            stressed_trades_parts = []
            uniq = sorted(set(selected_ids))
            stressed_map = {}
            for cid in uniq:
                combo = combo_objs[cid]
                sigs = eng.apply_combo(events[combo.variant], combo, tf)
                r = run_backtest(bars, sigs, cost_model=cm.stressed(1.5),
                                 point=point, spread_by_hour_price=sbh)
                stressed_map[cid] = r.trades
            for fr in wf["fold_rows"]:
                if fr["combo"]:
                    t_ = stressed_map[fr["combo"]]
                    tt = pd.to_datetime(t_["entry_time"]) if len(t_) else None
                    if tt is not None:
                        fold = folds[fr["fold"]]
                        part = t_[(tt >= fold.test_start) & (tt < fold.test_end)]
                        if len(part):
                            stressed_trades_parts.append(part)
            if stressed_trades_parts:
                stress_summ = summarise(pd.concat(stressed_trades_parts))

        # random baselines matched to OOS
        base_pct95 = long_pct95 = 0.0
        if len(oos):
            risk_atr = (oos["risk_price"] / oos["atr"].replace(0, np.nan)).dropna()
            buy_frac = float((oos["direction"] == 1).mean())
            modal_combo = combo_objs.get(modal)
            tgt = float(oos["target_R"].median()) if "target_R" in oos else 1.5
            tsb = modal_combo.time_stop_bars if modal_combo else None
            # sessions of the modal combo
            allowed = None
            if modal_combo:
                lo, hi = modal_combo.session_range
                allowed = set(range(lo, hi))
            seeds = range(1, int(baseline_seeds) + 1)
            rb = random_baseline(bars, feat, len(oos), buy_frac, seeds,
                                 risk_atr.to_numpy(), tgt, tsb, cm, point,
                                 spread_by_hour_price=sbh, allowed_hours=allowed)
            base_pct95 = rb["pct95"]
            rl = random_baseline(bars, feat, len(oos), 1.0, seeds,
                                 risk_atr.to_numpy(), tgt, tsb, cm, point,
                                 spread_by_hour_price=sbh, allowed_hours=allowed)
            long_pct95 = rl["pct95"]

        dsr = deflated_sharpe_ratio(oos["net_R"].to_numpy() if len(oos) else [],
                                    total_trials)
        # PBO: combos x 16 blocks (summed net_R)
        span_start, span_end = bars["time"].min(), bars["time"].max()
        block_mat = []
        for combo in combos:
            tr = combo_trades[combo.combo_id]
            if len(tr):
                block_mat.append(block_sums(tr["entry_time"], tr["net_R"],
                                            16, span_start, span_end))
            else:
                block_mat.append(np.zeros(16))
        pbo = pbo_cscv(np.array(block_mat)) if block_mat else float("nan")

        # +-20% perturbation on modal combo (full sample PF)
        pf_m = pf_p = 0.0
        if modal:
            combo = combo_objs[modal]
            sigs = eng.apply_combo(events[combo.variant], combo, tf)
            bars_i = bars.set_index("time").reset_index() if False else bars
            pf_m = _perturbation_pf(bars_i, sigs, combo, 0.8, cm, point, sbh)
            pf_p = _perturbation_pf(bars_i, sigs, combo, 1.2, cm, point, sbh)

        folds_prof = (float(np.mean([r["profitable"] for r in wf["fold_rows"]
                                     if r["test_trades"] > 0]))
                      if any(r["test_trades"] > 0 for r in wf["fold_rows"]) else 0.0)
        ci_lo = summ["expectancy_ci95"][0]
        reasons = []
        def gate(ok, label):
            if not ok:
                reasons.append(label)
        gate(summ["trades"] >= 200, "trades<200")
        gate(summ["expectancy_R_net"] > 0 and ci_lo > 0, "E[R]/CI<=0")
        gate(summ["profit_factor_net"] >= 1.20, "PF<1.20")
        gate(stress_summ["profit_factor_net"] >= 1.05, "stressPF<1.05")
        gate(folds_prof >= 0.70, "folds profitable<70%")
        gate(summ["max_drawdown_R"] <= MAX_DD_LIMIT_R, "maxDD>25R")
        gate(dsr >= 0.90, "DSR<0.90")
        gate(not (pbo != pbo) and pbo <= 0.30, "PBO>0.30")
        gate(min(pf_m, pf_p) >= 1.10, "perturbation PF<1.10")
        gate(summ["expectancy_R_net"] > base_pct95, "below random p95")
        gate(summ["expectancy_R_net"] > long_pct95, "below long-only p95")
        passed = not reasons

        if len(oos):
            os.makedirs(trades_dir, exist_ok=True)
            oos.to_csv(os.path.join(trades_dir, f"{engine}_{tf}_trades.csv"),
                       index=False)
            all_oos[(engine, tf)] = oos

        net = oos["net_R"].to_numpy() if len(oos) else np.array([])
        rows.append({
            "engine": engine, "tf": tf, "status": "PASS" if passed else "FAIL",
            "reasons": "; ".join(reasons) if reasons else "-",
            "trades": summ["trades"], "wr": summ["win_rate"],
            "pf": summ["profit_factor_net"], "exp": summ["expectancy_R_net"],
            "ci": summ["expectancy_ci95"], "dd": summ["max_drawdown_R"],
            "cost_share": summ["cost_share_of_gross_profit_pct"],
            "tpd": summ["trades_per_day"],
            "stress_pf": stress_summ["profit_factor_net"],
            "folds_prof": folds_prof, "dsr": dsr, "pbo": pbo,
            "pert_pf": (pf_m, pf_p), "base95": base_pct95, "long95": long_pct95,
            "modal_combo": modal, "fold_rows": wf["fold_rows"],
            "oos": oos, "summ_groups_dir": summarise_groups(
                oos, ["BUY" if v == 1 else "SELL" for v in oos["direction"]])
            if len(oos) else {},
            "summ_groups_year": summarise_groups(
                oos, [str(y) for y in pd.to_datetime(oos["entry_time"]).dt.year])
            if len(oos) else {},
        })

    _write_leaderboard(out_md, rows, total_trials, engines, timeframes,
                       symbol, time.time() - t_start)
    return {"rows": rows, "trials": total_trials}


def _pf(v):
    return "inf" if v == float("inf") else f"{v:.3f}"


def _write_leaderboard(path, rows, trials, engines, timeframes, symbol, secs):
    lines = []
    lines.append("# P2 Leaderboard — SR/strategy research (walk-forward OOS)")
    lines.append("")
    lines.append(f"Symbol: {symbol} · engines: {', '.join(engines)} · "
                 f"timeframes: {', '.join(timeframes)}")
    lines.append(f"**Trial counter (engine x TF x combo): {trials}** · "
                 f"runtime {secs/60:.1f} min · plan: research/P2_plan.md v1")
    lines.append("")
    lines.append("| Engine-TF | Verdict | OOS trades | WR | PF | E[R] net (95% CI) | "
                 "Stress PF | Folds prof | MaxDD R | Cost share | Trades/day | DSR | PBO | "
                 "Perturb PF -20%/+20% | Random p95 | Long p95 | Fail reasons |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("trades") is None and r["status"] == "NO DATA":
            lines.append(f"| {r['engine']}-{r['tf']} | NO DATA | — | — | — | — | — | — | — | — | — | — | — | — | — | — | no bars |")
            continue
        lo, hi = r["ci"]
        lines.append(
            f"| {r['engine']}-{r['tf']} | **{r['status']}** | {r['trades']} | "
            f"{r['wr']*100:.1f}% | {_pf(r['pf'])} | {r['exp']:+.4f} "
            f"[{lo:+.4f},{hi:+.4f}] | {_pf(r['stress_pf'])} | "
            f"{r['folds_prof']*100:.0f}% | {r['dd']:.1f} | "
            f"{r['cost_share']:.1f}% | {r['tpd']:.2f} | {r['dsr']:.3f} | "
            f"{r['pbo']:.3f} | {_pf(r['pert_pf'][0])}/{_pf(r['pert_pf'][1])} | "
            f"{r['base95']:+.4f} | {r['long95']:+.4f} | {r['reasons']} |")
    lines.append("")
    for r in rows:
        if r.get("oos") is None or not len(r.get("oos", [])):
            continue
        lines.append(f"## {r['engine']}-{r['tf']} — modal combo: `{r['modal_combo']}`")
        lines.append("")
        lines.append("| Fold | Train from | Test from | Combo (short) | Test trades | Test net R | Profitable |")
        lines.append("|---|---|---|---|---|---|---|")
        for fr in r["fold_rows"]:
            lines.append(f"| {fr['fold']} | {str(fr['train_start'])[:10]} | "
                         f"{str(fr['test_start'])[:10]} | "
                         f"{(fr['combo'] or '-')[:60]} | {fr['test_trades']} | "
                         f"{fr['test_net_R']:+.2f} | {fr['profitable']} |")
        lines.append("")
        lines.append("| Split | Trades | WR | PF | E[R] |")
        lines.append("|---|---|---|---|---|")
        for lbl, g in list(r["summ_groups_dir"].items()) + \
                [(f"Year {k}", v) for k, v in r["summ_groups_year"].items()]:
            lines.append(f"| {lbl} | {g['trades']} | {g['win_rate']*100:.1f}% | "
                         f"{_pf(g['profit_factor_net'])} | {g['expectancy_R_net']:+.4f} R |")
        lines.append("")
    lines.append("Gates (all required): OOS trades>=200; E[R]>0 with 95% CI "
                 "lower>0; PF>=1.20; stress PF>=1.05; >=70% folds profitable; "
                 "maxDD<=25R; DSR>=0.90 (real trial count); PBO<=0.30; "
                 "perturbation PF>=1.10; E[R] above both random baselines' p95.")
    lines.append("Nothing here authorises demo orders; P3/P5 need separate approval.")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
