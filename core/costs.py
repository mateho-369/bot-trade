"""L0 cost model (v3).

Cost is the number-one enemy of this bot (see docs/V3_AUDIT.md). In the
v3 backtester the SPREAD itself is modelled through bid/ask prices
(Buy enters at ask, Sell exits at ask), so this model owns only:

  * spread resolution: a bar spread <= 0 or missing is UNKNOWN, never
    free -- resolved by the backtester from spread-by-hour medians,
    else `default_spread_price` (0.24, the documented Exness XAUUSDm
    live spread). See core/backtester_v3.py.
  * slippage: default 0.3 x spread.
  * commission: price-units equivalent of round-trip commission for
    the traded lot (0 when the broker charges none / not yet known).
  * stress: `stress_multiplier` (gates check x1.5) scales the spread
    used on the bid/ask path AND the slippage.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    slippage_frac_of_spread: float = 0.3
    stress_multiplier: float = 1.0
    commission_price: float = 0.0
    default_spread_price: float = 0.24  # documented Exness XAUUSDm spread

    def spread_or_default(self, spread_price) -> float:
        """Resolve an unknown spread. <= 0 / NaN / missing => default.

        A spread of exactly 0 on a gold bar is a data hole, not a free
        trade (zero-cost leak fixed in P1.1)."""
        try:
            s = float(spread_price)
        except (TypeError, ValueError):
            return float(self.default_spread_price)
        if s != s or s <= 0:  # NaN, zero or negative: unknown, not free
            return float(self.default_spread_price)
        return s


    def effective_spread_price(self, spread_price) -> float:
        """Spread actually used on the bid/ask path (stress applied)."""
        return self.spread_or_default(spread_price) * float(self.stress_multiplier)

    def slippage_commission_price(self, spread_price) -> float:
        """Extra cost charged ONCE per trade, on top of the bid/ask
        spread (which the backtester's price path already pays)."""
        s = self.spread_or_default(spread_price)
        return (s * float(self.slippage_frac_of_spread)
                * float(self.stress_multiplier)
                + float(self.commission_price))

    def round_trip_cost_price(self, spread_price) -> float:
        """TOTAL round-trip drag (spread + slippage + commission).

        Kept for reporting/accounting: total = effective spread +
        slippage/commission. The backtester does NOT deduct this on top
        of the bid/ask path (that would charge the spread twice)."""
        return (self.effective_spread_price(spread_price)
                + self.slippage_commission_price(spread_price))

    def stressed(self, multiplier: float = 1.5) -> "CostModel":
        """Return a copy with costs multiplied (gate: PF must survive x1.5)."""
        return CostModel(
            slippage_frac_of_spread=self.slippage_frac_of_spread,
            stress_multiplier=float(multiplier),
            commission_price=self.commission_price,
            default_spread_price=self.default_spread_price,
        )
