#!/usr/bin/env python3
"""One command per research phase (v3).

    python tools/run_phase.py P1                 # Windows, real MT5 data
    python tools/run_phase.py P1 --fixture       # offline, synthetic fixture

P1: download (if real data missing and MT5 is available) -> SR_H1
baseline backtest with the shared v3 backtester + cost model ->
research/P1_report.md with every number and the exact command used.

Only P1 is implemented. Later phases stop here until their phase starts.
"""
import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Documented baseline to reproduce (REAL_WORLD_TEST.md, real 2024-2025
# data, spread 0.30 stress methodology of the duel):
DOCUMENTED_SR_H1 = {"trades": 426, "win_rate_pct": 36.2, "profit_factor": 1.15,
                    "expectancy_R": 0.020, "max_dd_R": 4.0}


def _load_real(data_dir, symbol, point_hint=None):
    import pandas as pd
    from core.data.history import read_parquet, resample_bars
    h1_path = os.path.join(data_dir, f"{symbol}_H1.parquet")
    if not os.path.exists(h1_path):
        return None
    h1 = read_parquet(h1_path)
    h4 = resample_bars(h1, "H4")
    point = point_hint or 0.01
    info_path = os.path.join(data_dir, f"{symbol}_symbol_info.json")
    if os.path.exists(info_path):
        with open(info_path, encoding="utf-8") as f:
            point = float(json.load(f).get("point") or point)
    return h1, h4, point, "REAL MT5 DATA"


def _load_fixture(fixture_path):
    from core.data.history import read_parquet, resample_bars
    m1 = read_parquet(fixture_path)
    return resample_bars(m1, "H1"), resample_bars(m1, "H4"), 0.01, \
        "SYNTHETIC FIXTURE (offline pipeline check -- NOT market data, " \
        "no performance claim)"


def run_p1(args) -> int:
    import pandas as pd
    from core.backtester_v3 import run_backtest, summarise
    from core.costs import CostModel
    from core.signals.sr_h1 import generate_sr_signals

    data_dir = os.path.join(ROOT, args.data_dir)
    loaded = None
    if not args.fixture:
        loaded = _load_real(data_dir, args.symbol)
        if loaded is None and args.download:
            print("real data missing -> trying tools/download_history.py ...")
            rc = subprocess.call(
                [sys.executable, os.path.join(ROOT, "tools", "download_history.py"),
                 "--symbol", args.symbol, "--out", args.data_dir])
            print(f"downloader exit code: {rc}")
            loaded = _load_real(data_dir, args.symbol)
    if loaded is None:
        fixture = os.path.join(ROOT, args.fixture_path)
        if not os.path.exists(fixture):
            print("[FIX] no real data and no fixture found. On Windows run: "
                  "tools\\download_history.py first.")
            return 2
        print("using synthetic fixture (offline mode)")
        loaded = _load_fixture(fixture)

    h1, h4, point, source = loaded
    median_spread = float((h1["spread_points"].astype(float) * point).median()) \
        if "spread_points" in h1.columns else 0.0
    if median_spread <= 0:
        median_spread = 0.24  # documented Exness XAUUSDm live spread fallback
    cm = CostModel(default_spread_price=median_spread)

    signals = generate_sr_signals(h1, h4)
    result = run_backtest(h1, signals, cost_model=cm, point=point)
    s = summarise(result.trades)
    stressed = run_backtest(h1, signals, cost_model=cm.stressed(1.5), point=point)
    ss = summarise(stressed.trades)

    os.makedirs(os.path.join(ROOT, "research"), exist_ok=True)
    report_path = os.path.join(ROOT, "research", "P1_report.md")
    cmd = " ".join([os.path.basename(sys.executable), "tools/run_phase.py"] + sys.argv[1:])
    lines = []
    lines.append("# P1 Report — SR_H1 baseline, shared v3 backtester")
    lines.append("")
    lines.append(f"- Generated (UTC): {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- Command: `{cmd}`")
    lines.append(f"- Python: {platform.python_version()} · {platform.platform()}")
    lines.append(f"- Data source: **{source}**")
    lines.append(f"- Bars: H1 {len(h1)} ({h1['time'].min()} -> {h1['time'].max()}), H4 {len(h4)}")
    lines.append(f"- Median spread used by cost model: {median_spread:.3f} price units "
                 f"(point={point}); slippage 0.3x spread, commission 0")
    lines.append(f"- Signals: {len(signals)} · skipped (in-trade): {result.skipped_in_trade} "
                 f"· skipped (bad stop): {result.skipped_bad_stop}")
    lines.append("")
    lines.append("## Results (triple-barrier, SL-first, next-bar-open entry)")
    lines.append("")
    lines.append("| Metric | Net of cost | Costs x1.5 | Gross (no cost) |")
    lines.append("|---|---|---|---|")
    lines.append(f"| Trades | {s['trades']} | {ss['trades']} | {s['trades']} |")
    lines.append(f"| Win rate | {s['win_rate']*100:.1f}% | {ss['win_rate']*100:.1f}% | — |")
    _pf = lambda v: "inf (no losing trades)" if v == float("inf") else f"{v:.3f}"  # noqa: E731
    lines.append(f"| Profit factor | {_pf(s['profit_factor_net'])} | {_pf(ss['profit_factor_net'])} | — |")
    lines.append(f"| Expectancy / trade | {s['expectancy_R_net']:+.4f} R | {ss['expectancy_R_net']:+.4f} R | {s['expectancy_R_gross']:+.4f} R |")
    lo, hi = s["expectancy_ci95"]
    lines.append(f"| Expectancy 95% bootstrap CI | [{lo:+.4f}, {hi:+.4f}] R | — | — |")
    lines.append(f"| Max drawdown | {s['max_drawdown_R']:.2f} R | {ss['max_drawdown_R']:.2f} R | — |")
    lines.append(f"| Total net | {s['total_net_R']:+.2f} R | {ss['total_net_R']:+.2f} R | — |")
    lines.append(f"| Total cost | {s['total_cost_R']:.2f} R ({s['cost_share_of_gross_profit_pct']:.1f}% of gross profit) | {ss['total_cost_R']:.2f} R | 0 |")
    lines.append(f"| Trades/day | {s['trades_per_day']:.2f} | — | — |")
    lines.append("")
    if source.startswith("REAL"):
        d = DOCUMENTED_SR_H1
        lines.append("## Reproduction check vs documented duel (REAL_WORLD_TEST.md)")
        lines.append("")
        lines.append("Documented (real 2024-2025, 426 trades): WR 36.2%, PF 1.15, "
                     "E[R] +0.020, maxDD 4.0R. Pre-declared tolerance for "
                     "'reproduced': trades within 15% IF the data window matches "
                     "2024-2025, PF within 0.10, E[R] within 0.01R. If your data "
                     "window differs, treat this as indicative only and say so.")
        lines.append("")
        lines.append(f"| | This run | Documented | Delta |")
        lines.append("|---|---|---|---|")
        lines.append(f"| Trades | {s['trades']} | {d['trades']} | {s['trades']-d['trades']:+d} |")
        lines.append(f"| Win rate | {s['win_rate']*100:.1f}% | {d['win_rate_pct']}% | {s['win_rate']*100-d['win_rate_pct']:+.1f} pp |")
        lines.append(f"| Profit factor | {_pf(s['profit_factor_net'])} | {d['profit_factor']} | {s['profit_factor_net']-d['profit_factor']:+.3f} |")
        lines.append(f"| Expectancy | {s['expectancy_R_net']:+.4f} R | {d['expectancy_R']:+.3f} R | {s['expectancy_R_net']-d['expectancy_R']:+.4f} R |")
        lines.append(f"| Max DD | {s['max_drawdown_R']:.2f} R | {d['max_dd_R']} R | {s['max_drawdown_R']-d['max_dd_R']:+.2f} R |")
    else:
        lines.append("## Reproduction check")
        lines.append("")
        lines.append("NOT APPLICABLE: synthetic fixture. This run only proves the "
                     "pipeline works end-to-end. The reproduction check must be "
                     "run on Windows with real MT5 data (see docs/P1_WINDOWS_RUNBOOK.md).")
    lines.append("")
    lines.append("## Gate status (P1)")
    lines.append("")
    lines.append("- Backtester/cost/triple-barrier/no-look-ahead unit tests: see pytest output (tests/).")
    lines.append("- SR_H1 gate evaluation belongs to P2 (walk-forward). Nothing here "
                 "authorises demo orders.")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"source: {source}")
    print(f"H1 bars: {len(h1)} · signals: {len(signals)} · trades: {s['trades']}")
    _pfv = lambda v: "inf" if v == float("inf") else f"{v:.3f}"  # noqa: E731
    print(f"net: WR {s['win_rate']*100:.1f}% · PF {_pfv(s['profit_factor_net'])} · "
          f"E[R] {s['expectancy_R_net']:+.4f} · maxDD {s['max_drawdown_R']:.2f}R · "
          f"cost {s['total_cost_R']:.2f}R")
    print(f"stress x1.5: PF {_pfv(ss['profit_factor_net'])} · E[R] {ss['expectancy_R_net']:+.4f}")
    print(f"report -> research/P1_report.md")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run a v3 research phase")
    ap.add_argument("phase", choices=["P1", "P2", "P3", "P4", "P5", "P6"])
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--symbol", default="XAUUSDm")
    ap.add_argument("--fixture", action="store_true",
                    help="force the synthetic offline fixture")
    ap.add_argument("--fixture-path", default="tests/fixtures/XAUUSDm_M1_synthetic.parquet")
    ap.add_argument("--download", action="store_true", default=True,
                    help="download first if real data is missing (Windows/MT5)")
    args = ap.parse_args(argv)
    if args.phase != "P1":
        print(f"{args.phase} is not implemented yet (phases stop for owner approval).")
        return 2
    return run_p1(args)


if __name__ == "__main__":
    raise SystemExit(main())
