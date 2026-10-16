"""SR_H1 port: generates breakout signals on synthetic data, respects
session/news filters, and is a pure function (same input -> same output)."""
import pandas as pd

from core.data.history import resample_bars
from core.data.synthetic import generate_synthetic_m1
from core.signals.sr_h1 import generate_sr_signals


def _frames(days=10, seed=7):
    m1 = generate_synthetic_m1(days=days, seed=seed)
    return resample_bars(m1, "H1"), resample_bars(m1, "H4")


def test_generates_some_signals_with_valid_stops():
    h1, h4 = _frames()
    sigs = generate_sr_signals(h1, h4)
    assert len(sigs) > 0
    for s in sigs:
        assert s.engine == "SR_H1" and s.direction in (1, -1)
        if s.direction == 1:
            assert s.stop_price < s.level + 1e-9
        else:
            assert s.stop_price > s.level - 1e-9
        hour = (s.signal_time - pd.Timedelta(hours=1)).hour
        assert 7 <= hour < 20 and not (12.5 <= hour < 14.5)


def test_pure_function_deterministic():
    h1, h4 = _frames(days=6, seed=11)
    a = generate_sr_signals(h1, h4)
    b = generate_sr_signals(h1, h4)
    assert [(s.bar_index, s.direction, s.stop_price) for s in a] == \
           [(s.bar_index, s.direction, s.stop_price) for s in b]


def test_too_little_data_returns_empty():
    h1, h4 = _frames(days=1, seed=7)
    assert generate_sr_signals(h1.iloc[:30], h4) == []
