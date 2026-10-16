"""P1.1 regression tests: zero-spread fallback, bid/ask asymmetry, gaps,
shared demo detection, downloader start-year defaults."""
import importlib.util
import os

import pandas as pd
import pytest

from core.backtester_v3 import run_backtest
from core.costs import CostModel
from core.execution.live_gate import is_demo_server
from core.signals.base import CandidateSignal


def bars(rows, with_spread=True):
    out = []
    t0 = pd.Timestamp("2026-01-05 08:00", tz="UTC")
    for i, r in enumerate(rows):
        row = {"time": t0 + pd.Timedelta(hours=i), "open": r[0], "high": r[1],
               "low": r[2], "close": r[3], "tick_volume": 100, "real_volume": 0}
        if with_spread:
            row["spread_points"] = r[4] if len(r) > 4 else 0.0
        out.append(row)
    return pd.DataFrame(out)


def sig(bar_index=0, direction=1, stop=99.0, target_R=2.0):
    return CandidateSignal(engine="TEST", timeframe="H1", direction=direction,
                           bar_index=bar_index,
                           signal_time=pd.Timestamp("2026-01-05 09:00", tz="UTC"),
                           stop_price=stop, target_R=target_R, atr=1.0)


# --- fix 1: zero/missing spread is never free -------------------------
def test_zero_spread_bars_still_pay_fallback_cost():
    df = bars([(100, 100.5, 99.5, 100, 0), (100, 101, 99.5, 100.5, 0),
               (100.5, 103, 100, 102.5, 0), (102.5, 103, 102, 102.5, 0)])
    res = run_backtest(df, [sig()], cost_model=CostModel(), point=0.01)
    t = res.trades.iloc[0]
    assert t.spread_source == "default"
    assert t.spread_price == pytest.approx(0.24)   # fallback, stress x1
    assert t.cost_price > 0.24                      # + slippage on top
    assert res.spread_fallback_trades == 1


def test_missing_spread_column_uses_hour_median_before_default():
    df = bars([(100, 100.5, 99.5, 100), (100, 101, 99.5, 100.5),
               (100.5, 103, 100, 102.5), (102.5, 103, 102, 102.5)],
              with_spread=False)
    sbh = {9: 0.40, 10: 0.40, 11: 0.40}   # entry bar is 09:00 UTC
    res = run_backtest(df, [sig()], cost_model=CostModel(), point=0.01,
                       spread_by_hour_price=sbh)
    t = res.trades.iloc[0]
    assert t.spread_source == "hour_median"
    assert t.spread_price == pytest.approx(0.40)
    assert res.spread_fallback_trades == 1


# --- fix 2: SELL barriers trigger on ASK, not bid ----------------------
def test_sell_stopped_by_ask_even_if_bid_stays_below_stop():
    # spread $0.20 (20 pts). Entry bar1: bid open 100 (SELL entry at bid).
    # Bar2 bid high = 100.9 = 0.1 BELOW the 101 stop on bid, but ask high
    # = 101.1 touches the stop -> must be stopped out.
    df = bars([(100, 100.5, 99.5, 100, 20), (100, 100.5, 99.5, 100, 20),
               (100, 100.9, 99.5, 100.2, 20), (100.2, 100.4, 99.8, 100, 20)])
    s = sig(direction=-1, stop=101.0)
    res = run_backtest(df, [s], cost_model=CostModel(
        slippage_frac_of_spread=0.0, default_spread_price=0.0), point=0.01)
    t = res.trades.iloc[0]
    assert t.exit_reason == "SL"
    assert t.entry == pytest.approx(100.0)  # SELL enters at bid


def test_sell_tp_not_triggered_by_bid_when_ask_stays_above():
    # TP = 98. Bar2 bid low = 97.9 dips below TP on bid (old bug: easy TP)
    # but ask low = 98.1 stays above TP -> must NOT take profit.
    df = bars([(100, 100.5, 99.5, 100, 20), (100, 100.5, 99.5, 100, 20),
               (100, 100.4, 97.9, 99.0, 20), (99.0, 99.5, 98.6, 99.2, 20)])
    s = sig(direction=-1, stop=101.0)
    res = run_backtest(df, [s], cost_model=CostModel(
        slippage_frac_of_spread=0.0, default_spread_price=0.0), point=0.01)
    t = res.trades.iloc[0]
    assert t.exit_reason == "EOD"  # still open at the end; never hit TP


def test_buy_enters_at_ask():
    df = bars([(100, 100.5, 99.5, 100, 20), (100, 101, 99.5, 100.5, 20),
               (100.5, 104, 100, 103, 20)])
    res = run_backtest(df, [sig()], cost_model=CostModel(
        slippage_frac_of_spread=0.0, default_spread_price=0.0), point=0.01)
    t = res.trades.iloc[0]
    assert t.entry_bid == pytest.approx(100.0)
    assert t.entry == pytest.approx(100.20)  # ask = bid + $0.20 spread


# --- fix 3: gaps --------------------------------------------------------
def test_gap_through_stop_exits_at_open_not_stop():
    # Bar2 opens at 98, beyond the 99 stop -> exit 98 (worse), not 99.
    df = bars([(100, 100.5, 99.5, 100, 0), (100, 100.5, 99.5, 100, 0),
               (98, 98.5, 97.5, 98, 0), (98, 98.5, 97.5, 98, 0)])
    res = run_backtest(df, [sig()], cost_model=CostModel(
        slippage_frac_of_spread=0.0, default_spread_price=0.0), point=0.01)
    t = res.trades.iloc[0]
    assert t.exit_reason == "SL" and t.exit == pytest.approx(98.0)
    assert t.net_R == pytest.approx(-2.0)


def test_gap_past_target_takes_target_not_better_open():
    # Bar2 opens at 105, beyond the 102 target -> book 102, not 105.
    df = bars([(100, 100.5, 99.5, 100, 0), (100, 100.5, 99.5, 100, 0),
               (105, 106, 104, 105, 0), (105, 106, 104, 105, 0)])
    res = run_backtest(df, [sig()], cost_model=CostModel(
        slippage_frac_of_spread=0.0, default_spread_price=0.0), point=0.01)
    t = res.trades.iloc[0]
    assert t.exit_reason == "TP" and t.exit == pytest.approx(102.0)
    assert t.net_R == pytest.approx(2.0)


def test_sell_gap_through_stop_exits_at_ask_open():
    # SELL stop 101, spread $0.20: bar2 bid open 101.0 -> ask open 101.2.
    df = bars([(100, 100.5, 99.5, 100, 20), (100, 100.5, 99.5, 100, 20),
               (101.0, 101.5, 100.5, 101.2, 20), (101.2, 101.4, 100.8, 101, 20)])
    s = sig(direction=-1, stop=101.0)
    res = run_backtest(df, [s], cost_model=CostModel(
        slippage_frac_of_spread=0.0, default_spread_price=0.0), point=0.01)
    t = res.trades.iloc[0]
    assert t.exit_reason == "SL" and t.exit == pytest.approx(101.2)


# --- fix 4: one shared demo check --------------------------------------
def test_demo_detection_shared_and_investing_dropped():
    assert is_demo_server("Exness-MT5Trial6")
    assert is_demo_server("MetaQuotes-Demo")
    assert not is_demo_server("Exness-MT5Real7")
    assert not is_demo_server("SomeBroker-Investing")
    assert not is_demo_server("Investing-Live")


def test_preflight_uses_live_gate_function():
    src = open(os.path.join(os.path.dirname(__file__), "..", "preflight.py"),
               encoding="utf-8").read()
    assert "from core.execution.live_gate import is_demo_server" in src
    assert "investing" not in src.split("is_demo_server(server)")[0].split("check_broker")[-1]


# --- fix 5: downloader start-year defaults ------------------------------
def _load_downloader():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "tools", "download_history.py")
    spec = importlib.util.spec_from_file_location("download_history", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_downloader_start_year_defaults():
    mod = _load_downloader()
    assert mod.resolve_start_year("M1", None, None) == 2023
    assert mod.resolve_start_year("M5", None, None) == 2023
    assert mod.resolve_start_year("M15", None, None) == 2023
    assert mod.resolve_start_year("H1", None, None) == 2010
    assert mod.resolve_start_year("M1", 2020, None) == 2020   # override
    assert mod.resolve_start_year("H1", 2020, 2015) == 2015   # H1 override
