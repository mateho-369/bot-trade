"""L0 cost model: spread resolution, slippage+commission, stress."""
from core.costs import CostModel


def test_zero_or_missing_spread_is_never_free():
    cm = CostModel()
    assert cm.spread_or_default(0) == 0.24       # zero = unknown data hole
    assert cm.spread_or_default(-1) == 0.24
    assert cm.spread_or_default(None) == 0.24
    assert cm.spread_or_default(float("nan")) == 0.24
    assert cm.spread_or_default(0.30) == 0.30


def test_slippage_commission_only_on_top_of_spread():
    cm = CostModel()
    # slippage 0.3 x $0.24, no commission; spread itself is paid via bid/ask
    assert abs(cm.slippage_commission_price(0.24) - 0.072) < 1e-9
    # total drag accounting = spread + slippage
    assert abs(cm.round_trip_cost_price(0.24) - 0.312) < 1e-9


def test_cost_stress_multiplies():
    cm = CostModel().stressed(1.5)
    assert abs(cm.effective_spread_price(0.24) - 0.36) < 1e-9
    assert abs(cm.round_trip_cost_price(0.24) - 0.24 * 1.3 * 1.5) < 1e-9


def test_commission_added():
    cm = CostModel(commission_price=0.10)
    assert abs(cm.slippage_commission_price(0.0) - (0.24 * 0.3 + 0.10)) < 1e-9
