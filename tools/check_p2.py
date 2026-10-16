#!/usr/bin/env python3
"""Read-only P2 v1 gate audit; never runs a strategy or changes trial counts.

Usage: python tools/check_p2.py --leaderboard research/P2_leaderboard.md \
                              --trades-dir data/research
Exit 0: all rows pass; 1: valid results contain gate failures; 2: invalid
or incomplete evidence. Rounded report-only metrics are explicitly attributed
to the runner, not presented as independent full-precision recomputations.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.backtester_v3 import summarise
from core.research.plan import ENGINE_SPECS, TF_MINUTES
from core.research.stats import deflated_sharpe_ratio

# Labels exactly as emitted by frozen runner v1. Do not modify the runner.
LABELS = (
    "trades<200", "E[R]/CI<=0", "PF<1.20", "stressPF<1.05",
    "folds profitable<70%", "maxDD>25R", "DSR<0.90", "PBO>0.30",
    "perturbation PF<1.10", "below random p95", "below long-only p95",
)


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    reason: str


def _cells(line: str) -> list[str]:
    return [c.strip().replace("**", "") for c in line.strip().strip("|").split("|")]


def read_leaderboard(path: Path) -> tuple[int, dict[str, list[str]], dict[str, list[list[str]]]]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"Trial counter \(engine x TF x combo\): (\d+)", text)
    if not match or int(match[1]) < 1:
        raise ValueError("missing/invalid global trial counter")
    header = re.search(r"engines: ([^\n]+) · timeframes: ([^\n]+)", text)
    if not header or "plan: research/P2_plan.md v1" not in text:
        raise ValueError("missing engine/TF header or unsupported plan version")
    engines = [v.strip() for v in header[1].split(",")]
    tfs = [v.strip() for v in header[2].split(",")]
    if any(e not in ENGINE_SPECS for e in engines) or any(t not in TF_MINUTES for t in tfs):
        raise ValueError("unknown engine/timeframe")
    expected = {f"{e}-{t}" for e in engines for t in tfs}
    rows: dict[str, list[str]] = {}
    folds: dict[str, list[list[str]]] = {}
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].split(" — ")[0]
        if not line.startswith("|"):
            continue
        c = _cells(line)
        if c[0] in expected:
            if len(c) != 17 or c[0] in rows:
                raise ValueError("malformed/duplicate leaderboard row")
            rows[c[0]] = c
        elif section in expected and c[0].isdigit():
            # Combo strings contain literal pipes, including in '(short)' cells.
            if len(c) < 7:
                raise ValueError("malformed fold row")
            folds.setdefault(section, []).append(c)
    if set(rows) != expected:
        raise ValueError("leaderboard rows do not cover declared engine x TF grid")
    return int(match[1]), rows, folds


def _number(value: str) -> float:
    v = float(value.rstrip("%"))
    if math.isnan(v) or v == -math.inf:
        raise ValueError("missing/non-finite metric")
    return v


def _matches(actual: float, shown: str, decimals: int, scale: float = 1) -> bool:
    """Allow only the report's declared rounding, not research tolerances."""
    v = _number(shown)
    return (actual == v if math.isinf(v) else
            abs(actual * scale - v) <= 0.5 * 10 ** -decimals + 1e-10)


def check_row(c: list[str], fold_rows: list[list[str]], trials: int,
              trades_dir: Path) -> tuple[list[Gate], dict]:
    key, status = c[:2]
    if status == "NO DATA":
        return [Gate(label, False, "no bars / no evidence") for label in LABELS], {}
    if status not in {"PASS", "FAIL"}:
        raise ValueError(f"{key}: unknown verdict")
    failed = set() if c[16] == "-" else set(c[16].split("; "))
    if not failed.issubset(LABELS) or (status == "PASS") != (not failed):
        raise ValueError(f"{key}: inconsistent verdict/failure reasons")
    count = int(c[2])
    if count < 0:
        raise ValueError(f"{key}: negative trade count")
    engine, tf = key.rsplit("-", 1)
    path = trades_dir / f"{engine}_{tf}_trades.csv"
    if count:
        trades = pd.read_csv(path, float_precision="round_trip")
        required = {"engine", "timeframe", "direction", "entry_time", "exit_time",
                    "net_R", "gross_R", "cost_R", "fold"}
        if not required.issubset(trades.columns):
            raise ValueError(f"{key}: missing CSV columns {sorted(required - set(trades.columns))}")
        if len(trades) != count or not trades.engine.eq(engine).all() or not trades.timeframe.eq(tf).all():
            raise ValueError(f"{key}: CSV count/identity mismatch")
        if not trades.direction.isin([-1, 1]).all():
            raise ValueError(f"{key}: invalid trade direction")
        if not np.isfinite(trades[["net_R", "gross_R", "cost_R"]].to_numpy(float)).all():
            raise ValueError(f"{key}: non-finite returns/costs")
        if (trades.cost_R < 0).any() or not np.allclose(
                trades.gross_R - trades.cost_R, trades.net_R, rtol=1e-10, atol=1e-10):
            raise ValueError(f"{key}: invalid gross/cost/net accounting")
        for col in ("entry_time", "exit_time"):
            trades[col] = pd.to_datetime(trades[col], utc=True, errors="raise", format="ISO8601")
            if trades[col].isna().any():
                raise ValueError(f"{key}: missing timestamps")
        if not trades.entry_time.is_monotonic_increasing or (trades.exit_time < trades.entry_time).any():
            raise ValueError(f"{key}: invalid timestamp order")
    else:
        # The runner does not emit zero-trade files; never consume stale files.
        trades = pd.DataFrame()
    s = summarise(trades)
    dsr = deflated_sharpe_ratio(trades.net_R.to_numpy() if count else [], trials)
    ci = re.fullmatch(r"([^ ]+) \[([^,]+),([^]]+)\]", c[5])
    if not ci:
        raise ValueError(f"{key}: malformed expectancy/CI")
    checks = [
        (s["win_rate"], c[3], 1, 100),
        (s["profit_factor_net"], c[4], 3, 1),
        (s["expectancy_R_net"], ci[1], 4, 1),
        (s["expectancy_ci95"][0], ci[2], 4, 1),
        (s["expectancy_ci95"][1], ci[3], 4, 1),
        (s["max_drawdown_R"], c[8], 1, 1),
        (s["cost_share_of_gross_profit_pct"], c[9], 1, 1),
        (s["trades_per_day"], c[10], 2, 1), (dsr, c[11], 3, 1),
    ]
    if any(not _matches(*v) for v in checks):
        raise ValueError(f"{key}: CSV metrics disagree with rounded leaderboard")
    active_folds = []
    seen = set()
    for fr in fold_rows:
        idx, n, net, profitable = int(fr[0]), int(fr[-3]), fr[-2], fr[-1]
        if idx in seen or n < 0 or profitable not in {"True", "False"}:
            raise ValueError(f"{key}: invalid/duplicate fold")
        seen.add(idx)
        part = trades[trades.fold == idx] if count else trades
        if len(part) != n or not _matches(float(part.net_R.sum()) if n else 0.0, net, 2):
            raise ValueError(f"{key}: fold CSV/leaderboard mismatch")
        if (profitable == "True") != (bool(n) and float(part.net_R.sum()) > 0):
            raise ValueError(f"{key}: fold profitable flag mismatch")
        if n:
            active_folds.append(profitable == "True")
    if count and (not fold_rows or not set(trades.fold).issubset(seen)):
        raise ValueError(f"{key}: missing fold evidence")
    folds_prof = float(np.mean(active_folds)) if active_folds else 0.0
    if not _matches(folds_prof, c[7], 0, 100):
        raise ValueError(f"{key}: profitable-fold fraction mismatch")

    gates: list[Gate] = []

    def exact(label: str, ok: bool, reason: str) -> None:
        if ok != (label not in failed):
            raise ValueError(f"{key}: recomputed gate disagrees: {label}")
        gates.append(Gate(label, ok, reason + " [CSV recomputed]"))

    def reported(label: str, margin: float, rounding: float, reason: str) -> None:
        # A rounded boundary cannot override the runner's exact failure labels.
        # Outside the rounding interval, contradictory evidence is an error.
        ok = label not in failed
        if not math.isnan(margin) and ((ok and margin < -rounding - 1e-10) or
                                      (not ok and margin > rounding + 1e-10)):
            raise ValueError(f"{key}: report metric contradicts gate: {label}")
        gates.append(Gate(label, ok, reason + " [runner-reported, rounded; not independently recomputed]"))

    exact(LABELS[0], count >= 200, f"trades={count}; require >=200")
    exact(LABELS[1], s["expectancy_R_net"] > 0 and s["expectancy_ci95"][0] > 0,
          f"E[R]={s['expectancy_R_net']:.17g}, CI95={s['expectancy_ci95']}; both lower/mean >0")
    exact(LABELS[2], s["profit_factor_net"] >= 1.20,
          f"PF={s['profit_factor_net']:.17g}; require >=1.20")
    reported(LABELS[3], _number(c[6]) - 1.05, .0005, f"stress PF={c[6]}; require >=1.05")
    exact(LABELS[4], folds_prof >= .70,
          f"profitable active folds={sum(active_folds)}/{len(active_folds)} ({folds_prof:.17g}); require >=0.70; empty folds excluded by v1")
    exact(LABELS[5], s["max_drawdown_R"] <= 25,
          f"maxDD={s['max_drawdown_R']:.17g}R; require <=25R (v1 formula)")
    exact(LABELS[6], dsr >= .90, f"DSR={dsr:.17g}; require >=0.90; global trials={trials}")
    pbo = float(c[12])  # nan is emitted by runner when not computable (FAIL).
    if not (math.isnan(pbo) or 0 <= pbo <= 1):
        raise ValueError(f"{key}: invalid PBO")
    if math.isnan(pbo) and LABELS[7] not in failed:
        raise ValueError(f"{key}: NaN PBO cannot pass")
    reported(LABELS[7], .30 - pbo, .0005, f"PBO={c[12]}; require finite <=0.30")
    pert = c[13].split("/")
    if len(pert) != 2:
        raise ValueError(f"{key}: malformed perturbation PF")
    reported(LABELS[8], min(map(_number, pert)) - 1.10, .0005,
             f"PF -20%/+20%={c[13]}; both >=1.10; v1 uses full sample modal combo")
    for label, shown in zip(LABELS[9:], c[14:16]):
        reported(label, s["expectancy_R_net"] - _number(shown), .00005,
                 f"engine E[R]={s['expectancy_R_net']:.17g} > control p95={shown} (strict)")
    s.update(dsr=dsr, folds_prof=folds_prof)
    if count:
        s.update(entry_start=str(trades.entry_time.min()), entry_end=str(trades.entry_time.max()))
    return gates, s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--leaderboard", type=Path, default=Path("research/P2_leaderboard.md"))
    ap.add_argument("--trades-dir", type=Path, default=Path("data/research"))
    args = ap.parse_args(argv)
    try:
        trials, rows, folds = read_leaderboard(args.leaderboard)
        # Validate every row before printing any PASS.
        results = [(key, *check_row(c, folds.get(key, []), trials, args.trades_dir))
                   for key, c in rows.items()]
    except (OSError, ValueError, KeyError, TypeError, pd.errors.ParserError) as exc:
        print(f"ERROR: incomplete/invalid P2 evidence: {exc}", file=sys.stderr)
        return 2
    print(f"P2 v1 audit | global trial counter={trials} (read, never reset)")
    print("Report-only gates retain runner failure labels at rounding boundaries.")
    print("This is runner parity, NOT independent section-6 certification or order permission.")
    print("Original exports cannot verify seed count, data period, cumulative prior trials, CSCV inputs, or OOS robustness.")
    any_fail = False
    for key, gates, summary in results:
        passed = all(g.passed for g in gates)
        any_fail |= not passed
        print(f"\n{key}: {'PASS' if passed else 'FAIL'} (v1 runner gates)")
        if summary:
            print(f"  OOS trades={summary['trades']}; E[R]={summary['expectancy_R_net']:.17g}")
            if summary["trades"]:
                print(f"  OOS entry range: {summary['entry_start']} -> {summary['entry_end']} (not source-bar coverage)")
        for g in gates:
            print(f"  {'PASS' if g.passed else 'FAIL'} | {g.name} | {g.reason}")
    print("\nDemo-forward gates: NOT EVALUATED (requires >=100 real demo trades and fill/model evidence).")
    return int(any_fail)


if __name__ == "__main__":
    raise SystemExit(main())
