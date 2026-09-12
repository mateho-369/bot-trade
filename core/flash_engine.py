"""FLASH engine — the ORIGINAL MTF high-frequency flow + 3 user upgrades.

ENTRY_ENGINE = "FLASH" selects this engine (see config.py).

Entry (follows the original main branch — no session filter, no H1 gate):
    core.mtf_engine.scan_all_timeframes over M1/M5/M15 -> _make_decision
    -> market entry at next bar, ATR*mult SL/TP (TF default, or the tighter
    config.FLASH_SL_MULT when > 0 — upgrade U1 "lose small").

Trade management (this module owns all SL changes for FLASH pairs):
    U2 profit lock : when a leg's unrealized profit (live tick) reaches
                     FLASH_LOCK_TRIGGER x distance-to-TP, its SL ratchets to
                     entry + FLASH_LOCK_PCT x current profit (50% of the gain
                     is banked even if price reverses). Ratchet only.
    U3 hedge flip  : when the main leg's loss reaches FLASH_HEDGE_TRIGGER x
                     its initial risk, the OPPOSITE side is opened at market
                     (same lot, same SL/TP geometry). When EITHER leg of the
                     pair closes (SL/TP or market), the whole pair is
                     flattened at market — "take the one with profit, don't
                     hold so long". Max 1 pair (2 positions) at a time.
    U4 time stop   : FLASH_TIME_STOP minutes > 0 -> close the pair after that.

Safety rails (backtest: /home/user/backtest_lab/flash_mtf.py, 2024-2025):
    - FLASH_DAILY_STOP_R: after X R of realized loss in one UTC day, no new
      entries AND no new hedges until the next day (capped worst day ~-7R in
      the backtest, vs -70R uncapped).
    - Hedge opening still passes risk_manager.can_open_new_trade
      (drawdown / margin / loss-limit gates).

Backtest verdict (cost 0.30/trade, 730 days, SL-first):
    original MTF        PF 0.87   (13.8 trades/day)
    + profit lock       PF 0.85
    + hedge flip        PF 0.77   (6,900 hedges in 24 months)
    full stack          PF 0.67
    => all variants < 1.0 after costs; kept as an opt-in engine, OFF by
       default (ENTRY_ENGINE stays "SR_H1", the duel winner).
"""
import logging
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5
import config
from core import mt5_connector as mc
from core import risk_manager
from core.mtf_engine import scan_all_timeframes
from core.state_logger import event, signal_log

logger = logging.getLogger("flash_engine")

# per-leg cost used ONLY for daily-stop R accounting (spread 0.25 + slip 0.05)
COST_EST = 0.30


# ── state (stored under state['flash'] in main.py's per-symbol state) ─────
def _flash(state):
    return state.setdefault('flash', {'pair': None, 'day': None, 'day_R': 0.0,
                                      'hedges_used': 0})


def _roll_day(f):
    today = datetime.now(timezone.utc).date().toordinal()
    if f.get('day') != today:
        f['day'] = today
        f['day_R'] = 0.0


def entry_blocked(state):
    """Daily loss stop: (blocked, reason). Also blocks hedge triggers."""
    f = _flash(state)
    _roll_day(f)
    lim = float(getattr(config, 'FLASH_DAILY_STOP_R', 0.0))
    if lim > 0 and f['day_R'] <= -lim:
        return True, f"FLASH daily stop: {f['day_R']:.2f}R <= -{lim:g}R"
    return False, ''


def has_open_pair(state):
    return _flash(state).get('pair') is not None


# ── entry decision (original MTF flow) ─────────────────────────────────────
def decide(symbol, get_candles, state):
    """Original MTF high-frequency entry (no session filter, no H1 gate)."""
    blocked, why = entry_blocked(state)
    if blocked:
        signal_log.log(f"{symbol}:flash", f"● HOLD {symbol} — {why}")
        return None
    decision = scan_all_timeframes(symbol, get_candles)
    if decision is None:
        return None
    decision['engine'] = 'FLASH'
    decision['n_positions'] = 1            # one pair at a time
    slm = float(getattr(config, 'FLASH_SL_MULT', 0.0))
    if slm > 0:                            # U1: tighter initial SL
        decision['sl_mult'] = slm
    return decision


def register_main(state, leg):
    """main.py calls this after the main leg is placed."""
    _flash(state)['pair'] = {'main': leg, 'hedge': None}


# ── internals ──────────────────────────────────────────────────────────────
def _leg_unrealized(leg, tick):
    price = tick.bid if leg['dir'] == 1 else tick.ask
    return leg['dir'] * (price - leg['entry'])


def _settle_pair(state, pair, exit_prices):
    """Account the PAIR's realized P&L in R units (for the daily stop).

    R uses the MAIN leg's initial stop distance — identical to the
    backtest (flash_mtf.py): one logical trade per pair, cost per leg."""
    f = _flash(state)
    main = pair['main']
    hedge = pair.get('hedge')
    risk0 = abs(main['entry'] - main['init_sl'])
    net = 0.0
    for key, leg in (('main', main), ('hedge', hedge)):
        if leg is None:
            continue
        net += leg['dir'] * (exit_prices[key] - leg['entry']) - COST_EST
    R = net / risk0 if risk0 > 0 else 0.0
    f['day_R'] += R
    logger.info(f"FLASH settle pair (main #{main['ticket']}{' + hedge' if hedge else ''}): "
                f"net {net:+.2f} = {R:+.2f}R (day {f['day_R']:+.2f}R)")


def _estimate_exit(leg, tick):
    """A leg the broker closed via SL/TP: estimate its exit level.
    If price is on the profit side of entry assume TP hit, else SL hit
    (close-enough for daily-stop R accounting)."""
    price = tick.bid if leg['dir'] == 1 else tick.ask
    if leg['dir'] * (price - leg['entry']) >= 0:
        return leg['tp']
    return leg['sl']


def _flatten_pair(symbol, state, pair, reason):
    """Close whatever is still open and settle the pair."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    open_tix = {p.ticket for p in mc.get_open_positions(symbol)}
    exit_prices = {}
    for leg_key in ('main', 'hedge'):
        leg = pair.get(leg_key)
        if leg is None:
            continue
        if leg['ticket'] in open_tix:
            out = mc.close_position(symbol, leg['ticket'])
            exit_prices[leg_key] = out if out is not None else \
                (tick.bid if leg['dir'] == 1 else tick.ask)
        else:
            exit_prices[leg_key] = _estimate_exit(leg, tick)
    _settle_pair(state, pair, exit_prices)
    _flash(state)['pair'] = None
    event(f"FLASH PAIR CLOSED {symbol} ({reason}) | day {f_day_R(state):+.2f}R")


def f_day_R(state):
    return _flash(state)['day_R']


def _try_hedge(symbol, state, pair, tick, risk_state, digits):
    """U3: open the opposite side at market, same lot & SL/TP geometry."""
    main = pair['main']
    d = -main['dir']
    entry = tick.ask if d == 1 else tick.bid
    a, slm, tpm = main['atr'], main['slm'], main['tpm']
    sl = round(entry - d * slm * a, digits)
    tp = round(entry + d * tpm * a, digits)
    lot = main['lot']
    allowed, reason = risk_manager.can_open_new_trade(risk_state, symbol, lot)
    if not allowed:
        signal_log.log(f"{symbol}:flash", f"● HOLD hedge {symbol} — {reason}")
        return False
    ticket, err = (mc.place_buy if d == 1 else mc.place_sell)(
        symbol, lot, sl, tp, comment="BOSS FLASH HEDGE")
    if not ticket:
        logger.warning(f"FLASH hedge rejected {symbol}: {err}")
        return False
    pair['hedge'] = {'ticket': ticket, 'dir': d, 'entry': entry, 'sl': sl, 'tp': tp,
                     'init_sl': sl, 'lot': lot, 'atr': a, 'slm': slm, 'tpm': tpm,
                     'opened_ts': time.time()}
    # bookkeeping: let step-1 outcome tracking & position caps see the hedge
    state['positions'].append({'ticket': ticket, 'entry': entry, 'tp': tp, 'sl': sl,
                               'direction': d, 'atr': a, 'tf': 'FLASH-HEDGE',
                               'hedge': True})
    f = _flash(state)
    f['hedges_used'] = f.get('hedges_used', 0) + 1
    event(f"FLASH HEDGE {symbol} {'BUY' if d == 1 else 'SELL'} #{ticket} @ {entry:.2f} "
          f"| SL {sl:.2f} TP {tp:.2f} (flipped on main leg loss)")
    return True


# ── per-loop management ────────────────────────────────────────────────────
def manage(symbol, state, risk_state):
    """Called every loop (rate-limited by main.py) while the FLASH engine is
    active. Owns the pair lifecycle: flatten-on-close, time stop, hedge
    trigger, profit-lock ratchet."""
    f = _flash(state)
    _roll_day(f)
    pair = f.get('pair')
    if pair is None:
        return
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 2

    main, hedge = pair['main'], pair.get('hedge')
    open_tix = {p.ticket for p in mc.get_open_positions(symbol)}
    main_alive = main['ticket'] in open_tix
    ts_min = int(getattr(config, 'FLASH_TIME_STOP', 0))

    if hedge is None:
        # ── main-only position (no hedge opened yet) ──────────────────
        if not main_alive:
            # main hit its SL/TP -> settle, pair done
            _settle_pair(state, pair, {'main': _estimate_exit(main, tick)})
            f['pair'] = None
            event(f"FLASH MAIN CLOSED {symbol} (SL/TP) | day {f['day_R']:+.2f}R")
            return
        if ts_min > 0 and time.time() - main['opened_ts'] >= ts_min * 60:
            _flatten_pair(symbol, state, pair, f'time stop {ts_min}m')
            return
        # hedge trigger (once per pair)
        if bool(getattr(config, 'FLASH_HEDGE', True)):
            blocked, _why = entry_blocked(state)
            if not blocked:
                loss = -_leg_unrealized(main, tick)
                risk0 = abs(main['entry'] - main['init_sl'])
                trig = float(getattr(config, 'FLASH_HEDGE_TRIGGER', 0.5))
                if risk0 > 0 and loss >= trig * risk0:
                    if _try_hedge(symbol, state, pair, tick, risk_state, digits):
                        return
        legs = (main,)
    else:
        # ── full pair (main + hedge) ───────────────────────────────────
        hedge_alive = hedge['ticket'] in open_tix
        if not main_alive and not hedge_alive:
            _flatten_pair(symbol, state, pair, 'both legs hit')
            return
        # one leg closed -> flatten the whole pair ("take the one with
        # profit, close the other — don't hold so long")
        if main_alive != hedge_alive:
            _flatten_pair(symbol, state, pair, 'one leg hit')
            return
        if ts_min > 0:
            now = time.time()
            if (now - main['opened_ts'] >= ts_min * 60) or \
               (now - hedge['opened_ts'] >= ts_min * 60):
                _flatten_pair(symbol, state, pair, f'time stop {ts_min}m')
                return
        legs = (main, hedge)

    # profit lock (ratchet) on every open leg
    if bool(getattr(config, 'FLASH_PROFIT_LOCK', True)):
        trig = float(getattr(config, 'FLASH_LOCK_TRIGGER', 0.5))
        lock = float(getattr(config, 'FLASH_LOCK_PCT', 0.5))
        for leg in legs:
            prof = _leg_unrealized(leg, tick)
            tpdist = abs(leg['tp'] - leg['entry'])
            if tpdist <= 0 or prof <= 0:
                continue
            if prof >= trig * tpdist:
                ns = round(leg['entry'] + lock * prof, digits)
                better = (leg['dir'] == 1 and ns > leg['sl']) or \
                         (leg['dir'] == -1 and ns < leg['sl'])
                if better and abs(ns - leg['sl']) > 1e-9:
                    if mc.modify_position(leg['ticket'], ns, leg['tp']):
                        old = leg['sl']
                        leg['sl'] = ns
                        signal_log.log(
                            f"{symbol}:flash",
                            f"● LOCK {symbol} #{leg['ticket']} "
                            f"{'BUY' if leg['dir'] == 1 else 'SELL'}: "
                            f"SL {old:.2f} → {ns:.2f} (locks {lock:g} of profit)")
