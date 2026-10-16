#!/usr/bin/env python3
"""Download XAUUSDm history from a running MT5 terminal (Windows, DEMO).

    venv\\Scripts\\python.exe tools\\download_history.py
    venv\\Scripts\\python.exe tools\\download_history.py --tick-days 30

What it does (read-only; never places an order, never prints secrets):
  * copy_rates_range in chunks for M1/M5/M15/H1, from 2010 to now --
    you get as much history as the terminal/broker actually provides.
  * copy_ticks_range in 1-day chunks for the last --tick-days days
    (default 30; 0 = skip). If the broker returns no ticks it skips
    gracefully and the bar spread field is used instead.
  * symbol_info snapshot -> data/XAUUSDm_symbol_info.json
  * spread-by-hour (from M1 bar spreads) -> data/XAUUSDm_spread_by_hour.csv
  * Parquet bars -> data/XAUUSDm_<TF>.parquet (git-ignored)
  * data/MANIFEST.json (rows, date range, sha256) -- safe to commit.

Login: uses config.py's .env loader (MT5_LOGIN/SERVER). The password is
passed to mt5.login() only; it is never printed or written anywhere.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402  (real .env via config.py's documented search order)
from core.data.history import (build_manifest, rates_to_frame,  # noqa: E402
                               spread_by_hour, ticks_to_frame, write_parquet)
from core.execution.live_gate import check_live_start  # noqa: E402

TF_CHUNK_DAYS = {"M1": 30, "M5": 90, "M15": 180, "H1": 365}
# M1 since 2010 is millions of rows and slow: low timeframes default to
# 2023-01-01 (--start-year overrides); H1 (and derived H4) go back to 2010.
TF_DEFAULT_START_YEAR = {"M1": 2023, "M5": 2023, "M15": 2023, "H1": 2010}


def resolve_start_year(tf_name: str, start_year, h1_start_year) -> int:
    if tf_name == "H1":
        return int(h1_start_year or TF_DEFAULT_START_YEAR["H1"])
    return int(start_year or TF_DEFAULT_START_YEAR[tf_name])
SYMBOL_INFO_FIELDS = [
    "name", "description", "currency_base", "currency_profit",
    "currency_margin", "digits", "point", "spread", "spread_float",
    "trade_contract_size", "trade_tick_value", "trade_tick_size",
    "volume_min", "volume_max", "volume_step", "volume_limit",
    "trade_stops_level", "trade_freeze_level", "filling_mode",
    "trade_mode", "swap_long", "swap_short", "swap_mode",
    "margin_initial", "margin_maintenance",
]


def _chunk_ranges(start, end, chunk_days):
    cur = start
    while cur < end:
        nxt = min(end, cur + timedelta(days=chunk_days))
        yield cur, nxt
        cur = nxt


def download_rates(mt5, symbol, tf_name, tf_const, start, end):
    import pandas as pd
    frames = []
    for a, b in _chunk_ranges(start, end, TF_CHUNK_DAYS[tf_name]):
        rates = mt5.copy_rates_range(symbol, tf_const, a, b)
        if rates is not None and len(rates):
            frames.append(rates_to_frame(rates))
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download MT5 history (read-only)")
    ap.add_argument("--symbol", default="XAUUSDm")
    ap.add_argument("--out", default="data")
    ap.add_argument("--tick-days", type=int, default=30,
                    help="days of ticks to fetch (0 = skip ticks)")
    ap.add_argument("--start-year", type=int, default=2023,
                    help="first year for M1/M5/M15 (default 2023-01-01)")
    ap.add_argument("--h1-start-year", type=int, default=2010,
                    help="first year for H1 (default 2010)")
    args = ap.parse_args(argv)

    try:
        import MetaTrader5 as mt5
    except Exception as e:
        print(f"[FIX] MetaTrader5 not importable here ({type(e).__name__}). "
              "Run this on Windows inside the project venv (install.bat).")
        return 2

    if not mt5.initialize():
        print(f"[FIX] mt5.initialize() failed: {mt5.last_error()} -- is the "
              "MT5 terminal open and logged in?")
        return 2
    try:
        if config.MT5_LOGIN:
            ok = mt5.login(login=int(config.MT5_LOGIN),
                           password=config.MT5_PASSWORD,
                           server=config.MT5_SERVER)
            if not ok:
                print(f"[FIX] mt5.login() failed: {mt5.last_error()}")
                return 2
        acc = mt5.account_info()
        if acc is not None:
            gate = check_live_start(getattr(acc, "server", ""),
                                    bool(getattr(config, "ALLOW_LIVE", False)))
            print(f"account server: {getattr(acc, 'server', '?')} "
                  f"({'DEMO ok' if gate.allowed else 'WARNING: ' + gate.reason})")
            print("(read-only download: no orders are placed either way)")

        mt5.symbol_select(args.symbol, True)
        info = mt5.symbol_info(args.symbol)
        if info is None:
            print(f"[FIX] symbol {args.symbol} not found on this account.")
            return 2
        os.makedirs(args.out, exist_ok=True)
        snap = {f: getattr(info, f, None) for f in SYMBOL_INFO_FIELDS}
        snap_path = os.path.join(args.out, f"{args.symbol}_symbol_info.json")
        with open(snap_path, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=2, default=str)
        print(f"symbol_info -> {snap_path} "
              f"(digits={snap.get('digits')} point={snap.get('point')} "
              f"stops_level={snap.get('trade_stops_level')} "
              f"filling_mode={snap.get('filling_mode')})")

        tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                  "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
        end = datetime.now(timezone.utc)
        point = float(snap.get("point") or 0.01)
        m1 = None
        print("rows per timeframe (as returned by the terminal):")
        for tf_name, tf_const in tf_map.items():
            year = resolve_start_year(tf_name, args.start_year,
                                      args.h1_start_year)
            start = datetime(year, 1, 1, tzinfo=timezone.utc)
            df = download_rates(mt5, args.symbol, tf_name, tf_const, start, end)
            if df is None or df.empty:
                print(f"[i] {tf_name}: no data returned by terminal")
                continue
            path = os.path.join(args.out, f"{args.symbol}_{tf_name}.parquet")
            write_parquet(df, path)
            print(f"{tf_name}: {len(df)} bars "
                  f"{df['time'].min()} -> {df['time'].max()} -> {path}")
            if tf_name == "M1":
                m1 = df

        if m1 is not None:
            sbh = spread_by_hour(m1, point)
            sbh_path = os.path.join(args.out, f"{args.symbol}_spread_by_hour.csv")
            sbh.to_csv(sbh_path, index=False)
            print(f"spread-by-hour -> {sbh_path} "
                  f"(overall median ${float((m1['spread_points'] * point).median()):.3f})")

        if args.tick_days > 0:
            tick_dir = os.path.join(args.out, "ticks")
            os.makedirs(tick_dir, exist_ok=True)
            got = 0
            for a, b in _chunk_ranges(end - timedelta(days=args.tick_days), end, 1):
                ticks = mt5.copy_ticks_range(args.symbol, a, b, mt5.COPY_TICKS_ALL)
                if ticks is None or len(ticks) == 0:
                    continue
                tdf = ticks_to_frame(ticks)
                write_parquet(tdf, os.path.join(
                    tick_dir, f"{args.symbol}_ticks_{a:%Y%m%d}.parquet"))
                got += len(tdf)
            if got:
                print(f"ticks: {got} rows -> {tick_dir}/ (1 file/day)")
            else:
                print("[i] ticks: broker returned none -- skipped gracefully "
                      "(bar spread field will be used for costs)")

        man = build_manifest(args.out, args.symbol,
                             extra={"generated_utc": datetime.now(timezone.utc).isoformat(),
                                    "source": "tools/download_history.py (MT5 copy_rates_range/copy_ticks_range)"})
        print(f"MANIFEST.json: {len(man['files'])} parquet files indexed")
        print("DONE. Next: venv\\Scripts\\python.exe tools\\run_phase.py P1")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
