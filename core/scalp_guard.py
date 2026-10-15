# ============================================================
#   core/scalp_guard.py
#   The deterministic half of the GUARDIAN (v2.3 twin-brain scalper).
#
#   Why it exists: an LLM answer takes seconds, and a flash scalp is over in
#   seconds. "Is this trade working? close it before it costs money" has to be
#   answered every loop, locally, with no network. The Guardian AI (the
#   position reviewer, now briefed in scalper terms) still looks at each trade
#   every ~15 s for the judgement calls; this module covers the rest:
#
#     BANK       floating profit reached the $ target -> take it now
#     GIVEBACK   it was well in profit, then gave back a chunk -> bank the rest
#     STALL      open a while and it never went anywhere -> not working, out
#     EARLY_CUT  it never worked AND it is already most of the way to the
#                stop -> cut the loss short instead of paying all of it
#     MAX_HOLD   a scalp is not an investment -> out after N seconds
#
#   It can only CLOSE a trade at market. It never moves, widens or removes a
#   stop, never opens anything and never raises risk, so it can sit next to
#   the trade manager (break-even / trailing) without the two fighting.
#
#   evaluate() is a pure function of (trade record, floating $, now) so every
#   rule is unit-testable without a broker.
# ============================================================

import time
from dataclasses import dataclass

import config


@dataclass
class GuardDecision:
    action: str            # "HOLD" or "CLOSE"
    code: str = ""         # SCALP_BANK / SCALP_GIVEBACK / ...
    detail: str = ""


HOLD = GuardDecision("HOLD")


def note_open(ps: dict, *, target_usd=None, sl_usd=None, lot=None, now=None):
    """Stamp a freshly opened position record with what the guard needs."""
    ps["opened_ts"] = float(now if now is not None else time.time())
    ps["peak_usd"] = 0.0
    if target_usd is not None:
        ps["target_usd"] = float(target_usd)
    if sl_usd is not None:
        ps["sl_usd"] = float(sl_usd)
    if lot is not None:
        ps["lot"] = float(lot)
    return ps


def evaluate(ps: dict, floating_usd: float, now=None) -> GuardDecision:
    """Decide HOLD / CLOSE for one open scalp. Updates ps['peak_usd']."""
    now = float(now if now is not None else time.time())
    if "opened_ts" not in ps:            # adopted after a restart: clock starts now
        ps["opened_ts"] = now
    floating = float(floating_usd)
    peak = max(float(ps.get("peak_usd", 0.0)), floating)
    ps["peak_usd"] = peak

    target = float(ps.get("target_usd") or getattr(config, "SCALP_TARGET_USD", 3.0))
    age = now - float(ps["opened_ts"])
    if age < float(getattr(config, "SCALP_GUARD_MIN_AGE_SECONDS", 3.0)):
        return HOLD                      # spread noise in the first seconds

    if target > 0 and floating >= target:
        return GuardDecision("CLOSE", "SCALP_BANK",
                             f"+${floating:.2f} reached the ${target:.2f} target")

    max_hold = float(getattr(config, "SCALP_MAX_HOLD_SECONDS", 240.0))
    if max_hold > 0 and age >= max_hold:
        return GuardDecision("CLOSE", "SCALP_MAX_HOLD",
                             f"open {age:.0f}s (max {max_hold:.0f}s), {floating:+.2f}")

    arm = float(getattr(config, "SCALP_GIVEBACK_ARM_PCT", 0.50)) * target
    give = float(getattr(config, "SCALP_GIVEBACK_PCT", 0.40))
    if target > 0 and peak >= arm and give > 0 and floating <= peak * (1.0 - give):
        return GuardDecision("CLOSE", "SCALP_GIVEBACK",
                             f"peak +${peak:.2f} -> {floating:+.2f} "
                             f"(gave back {give:.0%}+)")

    worked = peak >= float(getattr(config, "SCALP_STALL_MIN_PEAK_PCT", 0.25)) * target
    stall = float(getattr(config, "SCALP_STALL_SECONDS", 75.0))
    if stall > 0 and age >= stall and not worked:
        return GuardDecision("CLOSE", "SCALP_STALL",
                             f"{age:.0f}s and never above +${peak:.2f} "
                             f"(needs +${getattr(config, 'SCALP_STALL_MIN_PEAK_PCT', 0.25) * target:.2f}) "
                             f"- not working, {floating:+.2f}")

    sl_usd = float(ps.get("sl_usd") or 0.0)
    cut = float(getattr(config, "SCALP_EARLY_CUT_PCT", 0.70))
    if (sl_usd > 0 and cut > 0 and not worked
            and age >= float(getattr(config, "SCALP_EARLY_CUT_MIN_SECONDS", 20.0))
            and floating <= -cut * sl_usd):
        return GuardDecision("CLOSE", "SCALP_EARLY_CUT",
                             f"{floating:+.2f} is {abs(floating) / sl_usd:.0%} of "
                             f"the ${sl_usd:.2f} stop and the trade never worked")
    return HOLD
