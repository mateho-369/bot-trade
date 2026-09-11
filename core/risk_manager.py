# ============================================================
#   core/risk_manager.py
#   Capital protection & position sizing for small accounts.
#
#   Responsibilities:
#   1. Risk-based lot sizing (each trade risks a fixed % of balance)
#   2. Daily loss limit  — halt NEW trades for the day after -X%
#   3. Max drawdown halt — halt NEW trades if equity falls X% from peak
#   4. Free-margin guard — never open a trade we can't afford
#   5. Small-balance mode — fewer simultaneous positions
#   6. Withdrawal milestones — alert the user to take profit off the table
#
#   IMPORTANT: This module only ever BLOCKS new entries. It never
#   touches or closes existing trades — those stay under the
#   trade_manager's control until TP/SL.
# ============================================================

import logging
from datetime import datetime, date

import MetaTrader5 as mt5
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger("RiskManager")


class RiskState:
    """Tracks equity/PnL context needed for the safety checks."""

    def __init__(self):
        self.day               = date.today()
        self.day_start_balance = None    # balance at the start of the trading day
        self.peak_equity       = None    # high-water mark of equity
        self.last_milestone    = None    # last withdrawal milestone alerted
        self.halted_reason     = None    # None = trading allowed

    def _roll_day_if_needed(self, balance):
        today = date.today()
        if today != self.day or self.day_start_balance is None:
            self.day               = today
            self.day_start_balance = balance
            self.halted_reason     = None
            logger.info(f"New trading day — start balance ${balance:.2f}")

    def update(self, account_info):
        """Refresh state from a live MT5 account_info object. Call once/loop."""
        if account_info is None:
            return
        balance = float(account_info.balance)
        equity  = float(account_info.equity)

        self._roll_day_if_needed(balance)

        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity


# ── LOT SIZING ───────────────────────────────────────────────

def _round_to_step(value, step):
    if step <= 0:
        return value
    return round(round(value / step) * step, 8)


def calc_lot_size(symbol, entry, sl, balance, risk_pct=None):
    """
    Size the position so the loss at SL ≈ risk_pct % of balance.
    Falls back to config.RISK_PERCENT_PER_TRADE when risk_pct is None,
    and to config.LOT_SIZE if broker info is unavailable.
    Returns a broker-valid lot (clamped to min/max/step).

    RARC integration: the RARC engine passes its BOUNDED effective risk
    here (Module 1 ROR cap × Module 4 cooldown tier). The override can
    only ever be <= the configured baseline — enforced below.
    """
    fallback = config.LOT_SIZE
    if not getattr(config, "USE_RISK_BASED_LOT", False):
        return fallback

    info = mt5.symbol_info(symbol)
    if info is None:
        logger.warning(f"[{symbol}] No symbol_info — using fallback lot {fallback}")
        return fallback

    tick_value = getattr(info, "trade_tick_value", 0.0)   # $ per tick per 1.0 lot
    tick_size  = getattr(info, "trade_tick_size", 0.0)
    if tick_value <= 0 or tick_size <= 0:
        logger.warning(f"[{symbol}] Bad tick value/size — using fallback lot {fallback}")
        return fallback

    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return fallback

    # Loss (in account currency) for 1.0 lot if price travels sl_distance.
    loss_per_lot = (sl_distance / tick_size) * tick_value
    if loss_per_lot <= 0:
        return fallback

    # Effective risk %: RARC override if given, else configured baseline.
    # Belt & suspenders: an override can never size ABOVE the baseline.
    eff_pct = config.RISK_PERCENT_PER_TRADE
    if risk_pct is not None:
        try:
            eff_pct = min(float(risk_pct), float(config.RISK_PERCENT_PER_TRADE))
        except (TypeError, ValueError):
            pass

    risk_money = balance * (eff_pct / 100.0)
    raw_lot    = risk_money / loss_per_lot

    lot_min  = getattr(info, "volume_min",  0.01)
    lot_max  = getattr(info, "volume_max",  100.0)
    lot_step = getattr(info, "volume_step", 0.01)

    lot = _round_to_step(raw_lot, lot_step)
    lot = max(lot_min, min(lot, lot_max))

    logger.debug(
        f"[{symbol}] Lot sizing | bal ${balance:.2f} risk "
        f"{config.RISK_PERCENT_PER_TRADE}% (${risk_money:.2f}) "
        f"SLdist {sl_distance:.2f} → lot {lot}"
    )
    return lot


# ── POSITION COUNT ───────────────────────────────────────────

def positions_allowed(balance):
    """Fewer concurrent positions when the account is small."""
    threshold = getattr(config, "SMALL_BALANCE_THRESHOLD", 50)
    if balance < threshold:
        return 1
    return config.MAX_POSITIONS


# ── ENTRY GATE ───────────────────────────────────────────────

def can_open_new_trade(state: RiskState, symbol, lot):
    """
    Returns (allowed: bool, reason: str).
    Blocks NEW entries on daily-loss, drawdown, or insufficient margin.
    Never affects trades already open.
    """
    acc = mt5.account_info()
    if acc is None:
        return False, "no account info"

    balance = float(acc.balance)
    equity  = float(acc.equity)

    # 1. Daily loss limit (measured on equity vs day-start balance)
    if state.day_start_balance:
        day_pnl_pct = (equity - state.day_start_balance) / state.day_start_balance * 100.0
        limit = -abs(getattr(config, "DAILY_MAX_LOSS_PERCENT", 8.0))
        if day_pnl_pct <= limit:
            return False, f"daily loss limit ({day_pnl_pct:.1f}%)"

    # 2. Max drawdown from peak equity
    if state.peak_equity:
        dd_pct = (equity - state.peak_equity) / state.peak_equity * 100.0
        max_dd = -abs(getattr(config, "MAX_DRAWDOWN_PERCENT", 20.0))
        if dd_pct <= max_dd:
            return False, f"max drawdown ({dd_pct:.1f}% from peak)"

    # 3. Free-margin guard — make sure we can actually afford this lot
    buffer = getattr(config, "MIN_FREE_MARGIN_BUFFER", 1.5)
    price  = None
    tick   = mt5.symbol_info_tick(symbol)
    if tick is not None:
        price = tick.ask
    if price:
        needed = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, symbol, float(lot), price)
        if needed is not None and needed * buffer > acc.margin_free:
            # whole-dollar rounding keeps the message stable for change-only logging
            return False, f"low free margin (need ~${needed*buffer:.0f}, have ~${acc.margin_free:.0f})"

    return True, ""


# ── WITHDRAWAL MILESTONES ────────────────────────────────────

def check_withdrawal_milestone(state: RiskState):
    """
    Capital-preservation ladder. Once profit above the protected base crosses
    another WITHDRAW_MILESTONE_STEP, return a dict describing how much to pull.
    Returns None if the feature is off or no new milestone was reached.
    """
    if not getattr(config, "WITHDRAW_MILESTONE_ENABLED", False):
        return None

    base = getattr(config, "PROTECTED_BASE_CAPITAL", 0)
    step = getattr(config, "WITHDRAW_MILESTONE_STEP", 100)
    if base <= 0 or step <= 0:
        return None

    acc = mt5.account_info()
    if acc is None:
        return None

    balance = float(acc.balance)
    profit  = balance - base
    if profit < step:
        return None

    milestone = int(profit // step) * step   # e.g. profit 240, step 100 → 200
    if state.last_milestone is not None and milestone <= state.last_milestone:
        return None

    state.last_milestone = milestone
    return {
        "balance":    balance,
        "base":       base,
        "profit":     profit,
        "withdraw":   milestone,          # amount safe to take out
        "keep":       balance - milestone # what stays working in the account
    }
