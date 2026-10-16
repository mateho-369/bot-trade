"""Read-only checker parity with frozen runner exports; synthetic evidence only."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.backtester_v3 import summarise
from core.research.runner import _write_leaderboard, run_p2
from core.research.stats import deflated_sharpe_ratio
from tools.check_p2 import LABELS, check_row, main, read_leaderboard


def export(tmp_path, *, failed=(), overrides=None, trades=None):
    if trades is None:
        n = 240
        times = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        net = np.tile([2.0, -1.0], n // 2)
        trades = pd.DataFrame({"engine": "PULLBACK", "timeframe": "H1",
                               "direction": 1, "entry_time": times,
                               "exit_time": times + pd.Timedelta(minutes=30),
                               "net_R": net, "gross_R": net + .1,
                               "cost_R": .1, "fold": 0})
    s = summarise(trades)
    row = dict(engine="PULLBACK", tf="H1", status="FAIL" if failed else "PASS",
               reasons="; ".join(failed) if failed else "-", trades=s["trades"],
               wr=s["win_rate"], pf=s["profit_factor_net"], exp=s["expectancy_R_net"],
               ci=s["expectancy_ci95"], dd=s["max_drawdown_R"],
               cost_share=s["cost_share_of_gross_profit_pct"], tpd=s["trades_per_day"],
               stress_pf=1.2, folds_prof=float(bool(len(trades)) and trades.net_R.sum() > 0),
               dsr=deflated_sharpe_ratio(trades.net_R, 12), pbo=.1,
               pert_pf=(1.2, 1.2), base95=.01, long95=.02, modal_combo="synthetic|fixture",
               fold_rows=[dict(fold=0, train_start="2024-01-01", test_start="2025-01-01",
                               combo="synthetic|fixture", test_trades=len(trades),
                               test_net_R=float(trades.net_R.sum()), profitable=bool(len(trades) and trades.net_R.sum() > 0))],
               oos=trades, summ_groups_dir={}, summ_groups_year={})
    row.update(overrides or {})
    md = tmp_path / "leaderboard.md"
    _write_leaderboard(str(md), [row], 12, ["PULLBACK"], ["H1"], "XAUUSDm", 0)
    trades.to_csv(tmp_path / "PULLBACK_H1_trades.csv", index=False)
    trials, rows, folds = read_leaderboard(md)
    return row, rows["PULLBACK-H1"], folds["PULLBACK-H1"], trials, md


def test_every_gate_pass_and_exact_csv_metrics(tmp_path):
    row, cells, folds, trials, md = export(tmp_path)
    gates, s = check_row(cells, folds, trials, tmp_path)
    assert len(gates) == 11 and all(g.passed for g in gates)
    assert s["trades"] == row["trades"]
    assert s["expectancy_R_net"] == row["exp"]
    assert s["expectancy_ci95"] == row["ci"]
    assert s["profit_factor_net"] == row["pf"]
    assert s["max_drawdown_R"] == row["dd"]
    assert s["dsr"] == row["dsr"]
    assert main(["--leaderboard", str(md), "--trades-dir", str(tmp_path)]) == 0


@pytest.mark.parametrize("label,override", [
    (LABELS[3], {"stress_pf": 1.0499}),  # rounds to 1.050, still FAIL
    (LABELS[7], {"pbo": .3001}),         # rounds to .300, still FAIL
    (LABELS[7], {"pbo": float("nan")}),
    (LABELS[8], {"pert_pf": (1.0999, 1.2)}),
    (LABELS[9], {"base95": .500001}),    # engine E=.5; strict comparison
    (LABELS[10], {"long95": .5}),
])
def test_report_only_failures_and_rounding_are_not_promoted(tmp_path, label, override):
    _, cells, folds, trials, md = export(tmp_path, failed=[label], overrides=override)
    gates, _ = check_row(cells, folds, trials, tmp_path)
    assert [g.name for g in gates if not g.passed] == [label]
    assert main(["--leaderboard", str(md), "--trades-dir", str(tmp_path)]) == 1


def test_contradictory_report_metric_rejected(tmp_path):
    _, cells, folds, trials, _ = export(tmp_path, overrides={"stress_pf": .9})
    with pytest.raises(ValueError, match="contradicts"):
        check_row(cells, folds, trials, tmp_path)


@pytest.mark.parametrize("corruption", ["missing", "wrong_count", "wrong_engine", "nan",
                                        "wrong_metric", "missing_fold", "bad_time", "false_verdict"])
def test_incomplete_or_inconsistent_evidence_is_error(tmp_path, corruption):
    _, cells, folds, trials, md = export(tmp_path)
    path = tmp_path / "PULLBACK_H1_trades.csv"
    t = pd.read_csv(path)
    if corruption == "missing":
        path.rename(tmp_path / "other.csv")
    elif corruption == "wrong_count":
        t.iloc[:-1].to_csv(path, index=False)
    elif corruption == "wrong_engine":
        t["engine"] = "ORB"
        t.to_csv(path, index=False)
    elif corruption == "nan":
        t.loc[0, "net_R"] = np.nan
        t.to_csv(path, index=False)
    elif corruption == "wrong_metric":
        md.write_text(md.read_text().replace("2.000 |", "2.100 |"), encoding="utf-8")
    elif corruption == "missing_fold":
        md.write_text("\n".join(l for l in md.read_text().splitlines() if not l.startswith("| 0 |")))
    elif corruption == "bad_time":
        t.loc[0, "entry_time"] = "not a timestamp"
        t.to_csv(path, index=False)
    elif corruption == "false_verdict":
        md.write_text(md.read_text().replace("**PASS**", "**FAIL**"))
    assert main(["--leaderboard", str(md), "--trades-dir", str(tmp_path)]) == 2


def test_no_data_all_gates_fail(tmp_path):
    md = tmp_path / "leaderboard.md"
    _write_leaderboard(str(md), [dict(engine="ORB", tf="H1", status="NO DATA")],
                       24, ["ORB"], ["H1"], "XAUUSDm", 0)
    trials, rows, _ = read_leaderboard(md)
    gates, s = check_row(rows["ORB-H1"], [], trials, tmp_path)
    assert not s and len(gates) == 11 and not any(g.passed for g in gates)


def test_declared_grid_must_be_complete(tmp_path):
    _, _, _, _, md = export(tmp_path)
    md.write_text(md.read_text().replace("timeframes: H1", "timeframes: H1, M5"))
    assert main(["--leaderboard", str(md), "--trades-dir", str(tmp_path)]) == 2


def test_real_runner_short_fixture_parity_all_engines(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "XAUUSDm_M1_synthetic.parquet"
    md = tmp_path / "leaderboard.md"
    out = run_p2(str(tmp_path / "data"), fixture_path=str(fixture),
                 engines=("ORB", "PULLBACK", "SMC", "RANGE", "SQUEEZE"),
                 timeframes=("H1",), baseline_seeds=2,
                 out_md=str(md), trades_dir=str(tmp_path), progress=lambda _: None)
    trials, rows, folds = read_leaderboard(md)
    assert trials == out["trials"]
    for row in out["rows"]:
        key = f"{row['engine']}-{row['tf']}"
        gates, s = check_row(rows[key], folds.get(key, []), trials, tmp_path)
        assert s["trades"] == row["trades"] == 0  # fixture shorter than train window
        assert [g.name for g in gates if not g.passed] == row["reasons"].split("; ")


def test_real_runner_nonempty_oos_fixture_exact_parity(tmp_path):
    # Stretch the existing seeded fixture's timestamps to hourly bars over
    # >12 months. This is synthetic H1, NOT real data or a strategy trial.
    fixture = Path(__file__).parent / "fixtures" / "XAUUSDm_M1_synthetic.parquet"
    bars = pd.read_parquet(fixture)
    bars["time"] = pd.date_range("2024-01-01", periods=len(bars), freq="h", tz="UTC")
    data = tmp_path / "data"
    data.mkdir()
    bars.to_parquet(data / "XAUUSDm_H1.parquet", index=False)
    md = tmp_path / "leaderboard.md"
    out = run_p2(str(data), engines=("PULLBACK",), timeframes=("H1",),
                 baseline_seeds=2, out_md=str(md), trades_dir=str(tmp_path), progress=lambda _: None)
    row = out["rows"][0]
    assert row["trades"] > 0
    trials, rows, folds = read_leaderboard(md)
    gates, s = check_row(rows["PULLBACK-H1"], folds["PULLBACK-H1"], trials, tmp_path)
    assert [g.name for g in gates if not g.passed] == row["reasons"].split("; ")
    for key, orig in [("expectancy_R_net", "exp"), ("profit_factor_net", "pf"),
                      ("expectancy_ci95", "ci"), ("max_drawdown_R", "dd"),
                      ("dsr", "dsr"), ("folds_prof", "folds_prof")]:
        assert s[key] == row[orig]  # same full-precision numbers, not just tolerance
    before = {p: p.read_bytes() for p in tmp_path.glob("*.*")}
    assert main(["--leaderboard", str(md), "--trades-dir", str(tmp_path)]) == 1
    assert all(p.read_bytes() == contents for p, contents in before.items())


@pytest.mark.parametrize("returns,fail_indices", [
    (np.tile([2., -1.], 10), [0, 1, 6]),
    (np.full(240, -1.), [1, 2, 4, 5, 6]),
    (np.tile([1.1, -1.], 120), [1, 2, 6]),
    (np.ones(240), [6]),
    (np.r_[np.tile([2., -1.], 120), -30.], [5, 6]),
])
def test_each_csv_gate_can_fail_without_relabelling(tmp_path, returns, fail_indices):
    times = pd.date_range("2025-01-01", periods=len(returns), freq="h", tz="UTC")
    trades = pd.DataFrame(dict(engine="PULLBACK", timeframe="H1", direction=1,
                               entry_time=times, exit_time=times,
                               net_R=returns, gross_R=returns + .1,
                               cost_R=.1, fold=0))
    failed = [LABELS[i] for i in fail_indices]
    _, cells, folds, trials, _ = export(tmp_path, trades=trades, failed=failed,
                                       overrides={"base95": -2., "long95": -2.})
    gates, _ = check_row(cells, folds, trials, tmp_path)
    assert [g.name for g in gates if not g.passed] == failed


def test_false_exact_gate_reason_rejected(tmp_path):
    _, cells, folds, trials, _ = export(tmp_path, failed=[LABELS[0]])
    with pytest.raises(ValueError, match="recomputed gate disagrees"):
        check_row(cells, folds, trials, tmp_path)


def test_rounded_pass_near_threshold_keeps_runner_verdict(tmp_path):
    _, cells, folds, trials, _ = export(tmp_path, overrides={
        "stress_pf": 1.050001, "pbo": .299999, "pert_pf": (1.100001, 1.2),
        "base95": .499999, "long95": .499999})
    gates, _ = check_row(cells, folds, trials, tmp_path)
    assert all(g.passed for g in gates)
