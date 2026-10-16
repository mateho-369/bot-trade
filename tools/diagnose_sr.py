#!/usr/bin/env python3
"""Diagnose the SR_H1 'zero trades before 2021' question on real data.

    venv\\Scripts\\python.exe tools\\diagnose_sr.py [--data-dir data]

Read-only. Prints first H1/H4 timestamps, when H4 EMA50 warm-up is
ready, the per-year filter funnel and the first signal date.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data.history import read_parquet, resample_bars  # noqa: E402
from core.signals.sr_h1 import diagnose_sr_funnel  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--symbol", default="XAUUSDm")
    args = ap.parse_args(argv)
    h1_path = os.path.join(args.data_dir, f"{args.symbol}_H1.parquet")
    if not os.path.exists(h1_path):
        print(f"[FIX] {h1_path} not found - run tools/download_history.py first")
        return 2
    h1 = read_parquet(h1_path)
    h4 = resample_bars(h1, "H4")
    out = diagnose_sr_funnel(h1, h4)
    print(f"first H1 bar : {out['first_h1_time']}")
    print(f"first H4 bar : {out['first_h4_time']}  (derived from H1, {out['h4_bars']} bars)")
    print(f"H4 EMA50 warm-up ready from: {out['warmup_ready_time']}")
    print(f"total signals: {out['total_signals']}  first signal: {out['first_signal_time']}")
    print(out["funnel"].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
