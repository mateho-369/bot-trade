"""L0 cost model (v3).

Cost is the number-one enemy of this bot (see docs/V3_AUDIT.md), so every
backtest number must be NET of this model:

    round-trip cost (price units) =
        (spread + slippage_frac * spread) * stress_multiplier
        + commission_price

- spread: the real bid/ask spread in price units at entry time
  (from the bar's spread field / ticks; caller supplies a fallback).
- slippage: default 0.3 x spread (spec default).
- stress_multiplier: 1.5 under the "cost stress" switch (gate check).
- commission: price-units equivalent of round-trip commission for the
  traded lot; 0.0 when the broker charges none / not yet known. The
  caller converts $ commission to price units via tick value.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    slippage_frac_of_spread: float = 0.3
    stress_multiplier: float = 1.0
    commission_price: float = 0.0
    default_spread_price: float = 0.0  # fallback when a bar has no spread

    def spread_or_default(self, spread_price) -> float:
        try:
            s = float(spread_price)
        except (TypeError, ValueError):
            return float(self.default_spread_price)
        if s != s or s < 0:  # NaN / negative
            return float(self.default_spread_price)
        return s

    def round_trip_cost_price(self, spread_price) -> float:
        """Total round-trip cost in price units for one trade."""
        s = self.spread_or_default(spread_price)
        return (s * (1.0 + float(self.slippage_frac_of_spread))
                * float(self.stress_multiplier)
                + float(self.commission_price))

    def stressed(self, multiplier: float = 1.5) -> "CostModel":
        """Return a copy with costs multiplied (gate: PF must survive x1.5)."""
        return CostModel(
            slippage_frac_of_spread=self.slippage_frac_of_spread,
            stress_multiplier=float(multiplier),
            commission_price=self.commission_price,
            default_spread_price=self.default_spread_price,
        )
