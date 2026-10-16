#!/usr/bin/env python3
"""Regenerate the committed OFFLINE test fixture (synthetic, seeded).

    python tools/make_fixture.py

Writes tests/fixtures/XAUUSDm_M1_synthetic.parquet (~10 days of synthetic
M1 bars, well under the 2 MB fixture budget). This data is NOT real market
data and must never be used for performance claims.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data.history import write_parquet  # noqa: E402
from core.data.synthetic import generate_synthetic_m1  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "tests", "fixtures", "XAUUSDm_M1_synthetic.parquet")


def main() -> int:
    df = generate_synthetic_m1(days=10, seed=7)
    write_parquet(df, OUT)
    print(f"wrote {OUT}: {len(df)} bars, {os.path.getsize(OUT)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
