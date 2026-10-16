"""Backtester v3: fills, SL-first, triple barrier, costs, no look-ahead."""
import pandas as pd
import pytest

from core.backtester_v3 import run_backtest, summarise
from core.costs import CostModel
from core.signals.base import CandidateSignal


def bars(rows):
    """rows: list of (open, high, low, close[, spread_points])"""
    out = []
    t0 = pd.Timestamp("2026-01-05 08:00", tz="UTC")
    for i, r in enumerate(rows):
        o, h, l, c = r[:4]
        out.append({"time": t0 + pd.Timedelta(hours=i), "open": o, "high": h,
                    "low": l, "close": c, "tick_volume": 100,
                    "spread_points": r[4] if len(r) > 4 else 0.0,
                    "real_volume": 0})
    return pd.DataFrame(out)


def sig(bar_index=0, direction=1, stop=99.0, target_R=2.0, tsb=None):
    return CandidateSignal(engine="TEST", timeframe="H1", direction=direction,
                           bar_index=bar_index,
                           signal_time=pd.Timestamp("2026-01-05 09:00", tz="UTC"),
                           stop_price=stop, target_R=target_R,
                           time_stop_bars=tsb, atr=1.0)


FREE = CostModel(slippage_frac_of_spread=0.0)


def test_tp_fill_next_bar_entry():
    # entry at bar1 open=100, TP=102 hit on bar2
    df = bars([(100, 100.5, 99.5, 100), (100, 101, 99.5, 100.5),
               (100.5, 103, 100, 102.5), (102.5, 103, 102, 102.5)])
    res = run_backtest(df, [sig()], cost_model=FREE)
    t = res.trades.iloc[0]
    assert t.exit_reason == "TP" and t.entry == 100.0
    assert t.gross_R == pytest.approx(2.0) and t.net_R == pytest.approx(2.0)


def test_no_lookahead_signal_bar_cannot_fill():
    # TP would be hit ON the signal bar itself; entry is next bar, so the
    # trade must NOT be scored as a win from that bar.
    df = bars([(100, 105, 99, 104), (100, 100.5, 99.5, 100),
               (100, 100.5, 99.5, 100)])
    res = run_backtest(df, [sig()], cost_model=FREE)
    t = res.trades.iloc[0]
    # entry bar opens exactly when the signal bar closes (09:00) -- the
    # signal bar's own high (105) must not count as a fill.
    assert t.entry_time == t.signal_time
    assert t.exit_reason == "EOD"  # never reached TP after entry


def test_sl_first_when_both_barriers_in_one_bar():
    df = bars([(100, 100.5, 99.5, 100), (100, 103, 98, 100),
               (100, 100.5, 99.5, 100)])
    res = run_backtest(df, [sig()], cost_model=FREE)
    t = res.trades.iloc[0]
    assert t.exit_reason == "SL" and t.gross_R == pytest.approx(-1.0)


def test_time_barrier_exits_at_close():
    df = bars([(100, 100.4, 99.6, 100), (100, 100.4, 99.6, 100.2),
               (100.2, 100.5, 99.8, 100.4), (100.4, 100.6, 100, 100.5),
               (100.5, 100.7, 100.2, 100.6)])
    res = run_backtest(df, [sig(tsb=2)], cost_model=FREE)
    t = res.trades.iloc[0]
    assert t.exit_reason == "TIME" and t.bars_held == 3


def test_costs_reduce_net_and_are_reported():
    df = bars([(100, 100.5, 99.5, 100, 24), (100, 101, 99.5, 100.5, 24),
               (100.5, 103, 100, 102.5, 24), (102.5, 103, 102, 102.5, 24)])
    cm = CostModel()  # spread 24pts*0.01=$0.24, +30% slippage
    res = run_backtest(df, [sig()], cost_model=cm, point=0.01)
    t = res.trades.iloc[0]
    assert t.cost_price == pytest.approx(0.312)
    assert t.net_R == pytest.approx(2.0 - 0.312)  # risk = $1
    s = summarise(res.trades)
    assert s["trades"] == 1 and s["total_cost_R"] == pytest.approx(0.312)


def test_one_position_at_a_time_and_sell_direction():
    df = bars([(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100),
               (100, 100.5, 99.5, 100), (100, 100.5, 97, 98),
               (98, 98.5, 97.5, 98)])
    s1 = sig(0, direction=-1, stop=101.0)
    s2 = sig(1, direction=-1, stop=101.0)
    res = run_backtest(df, [s1, s2], cost_model=FREE)
    assert len(res.trades) == 1 and res.skipped_in_trade == 1
    assert res.trades.iloc[0].exit_reason == "TP"


def test_summarise_empty():
    s = summarise(pd.DataFrame())
    assert s["trades"] == 0 and s["profit_factor_net"] == 0.0
