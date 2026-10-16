"""L0 cost model: spread + 0.3x slippage, stress x1.5, fallbacks."""
from core.costs import CostModel


def test_round_trip_cost_default():
    cm = CostModel()
    # $0.24 spread + 30% slippage = $0.312 round trip
    assert abs(cm.round_trip_cost_price(0.24) - 0.312) < 1e-9


def test_cost_stress_multiplies():
    cm = CostModel().stressed(1.5)
    assert abs(cm.round_trip_cost_price(0.24) - 0.24 * 1.3 * 1.5) < 1e-9


def test_missing_spread_uses_default():
    cm = CostModel(default_spread_price=0.30)
    assert cm.round_trip_cost_price(None) > 0
    assert cm.round_trip_cost_price(float("nan")) > 0
    assert abs(cm.round_trip_cost_price(None) - 0.30 * 1.3) < 1e-9


def test_commission_added():
    cm = CostModel(commission_price=0.10)
    assert abs(cm.round_trip_cost_price(0.0) - 0.10) < 1e-9
