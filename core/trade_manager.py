# ============================================================
#   core/trade_manager.py
#   Smart Trade Management — runs every bar while in trade
#   Features:
#   1. Break-even: move SL to entry after price moves 1R
#   2. Progressive Trailing Stop: dynamic ATR-based trail
#   3. Partial TP: tighten TP if trend weakens
#   4. SL buffer: give SL extra room if ML thinks reversal temporary
# ============================================================

import logging
import MetaTrader5 as mt5
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import config
from tg_alerts.alerts import msg_adjusted, send
from core.state_logger import trade_log as _change_log, event

logger = logging.getLogger("TradeManager")

MAGIC = 505001


def manage_open_trade(symbol, state, df_feat, ml_model):
    """
    Called every bar while a trade is open.
    Adjusts SL and TP intelligently based on:
    - How far price has moved toward TP
    - ML trend confidence
    - ATR-based volatility
    
    state keys used: ticket, entry, tp, sl, direction
    """

    ticket = state['ticket']
    pos = _get_position_by_ticket(ticket)
    if pos is None:
        return

    entry    = state['entry']
    tp       = state['tp']
    sl       = state['sl']
    direction = state['direction']  # 1=BUY, -1=SELL

    # Current market price
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    info   = mt5.symbol_info(symbol)
    digits = info.digits if info else 5

    current_price = tick.bid if direction == 1 else tick.ask

    # ATR for dynamic distances. Use the ATR of the timeframe the trade was
    # OPENED on (stored in state) so higher-TF trades don't get a too-tight
    # M1-sized trailing stop that cuts winners short. Fall back to live M1 ATR.
    last     = df_feat.dropna(subset=['atr']).iloc[-1]
    live_atr = float(last['atr'])
    atr      = float(state.get('atr') or live_atr)

    # How far has price moved toward TP (as % of full TP distance)
    tp_distance   = abs(tp - entry)
    price_moved   = abs(current_price - entry) if direction == 1 else abs(entry - current_price)
    progress_pct  = price_moved / tp_distance if tp_distance > 0 else 0

    # Current unrealized profit in price units
    if direction == 1:
        unrealized = current_price - entry
    else:
        unrealized = entry - current_price

    # Change-only progress line: speaks when progress crosses a 10% bucket
    # (or on the 60s heartbeat), instead of spamming every loop. The bucket
    # keeps the deduped message stable; the heartbeat carries fresh numbers.
    bucket = int(progress_pct * 10) * 10
    _change_log.log(
        ticket,
        f"[{symbol}] #{ticket} {'BUY' if direction==1 else 'SELL'} | ~{bucket}% toward TP"
    )

    new_sl = None
    new_tp = None
    reason = ""

    # ── RULE 1: BREAK-EVEN ───────────────────────────────────
    # If price moved to threshold toward TP → move SL to entry + small buffer
    be_progress = getattr(config, 'BREAK_EVEN_PROGRESS', 0.60)
    if progress_pct >= be_progress:
        buffer = atr * 0.1  # small buffer above entry
        if direction == 1:
            be_sl = round(entry + buffer, digits)
            if be_sl > pos.sl and be_sl < current_price:
                new_sl = be_sl
                reason = "Break-even"
                logger.debug(f"[{symbol}] BREAK-EVEN candidate → SL {new_sl:.2f}")
        else:
            be_sl = round(entry - buffer, digits)
            if be_sl < pos.sl and be_sl > current_price:
                new_sl = be_sl
                reason = "Break-even"
                logger.debug(f"[{symbol}] BREAK-EVEN candidate → SL {new_sl:.2f}")

    # ── RULE 2: PROGRESSIVE TRAILING STOP ───────────────────
    if config.PROGRESSIVE_TRAIL and new_sl is None:
        trail_mult = None
        if progress_pct >= config.TRAIL_STAGE_3_PROGRESS:
            trail_mult = config.TRAIL_STAGE_3_ATR_MULT
        elif progress_pct >= config.TRAIL_STAGE_2_PROGRESS:
            trail_mult = config.TRAIL_STAGE_2_ATR_MULT
        elif progress_pct >= config.TRAIL_STAGE_1_PROGRESS:
            trail_mult = config.TRAIL_STAGE_1_ATR_MULT

        if trail_mult is not None:
            if direction == 1:
                trail_sl = round(current_price - atr * trail_mult, digits)
                if trail_sl > pos.sl and trail_sl < current_price:
                    new_sl = trail_sl
                    reason = f"Trailing stop ({trail_mult}x ATR)"
                    logger.debug(f"[{symbol}] {reason} candidate → {new_sl:.2f}")
            else:
                trail_sl = round(current_price + atr * trail_mult, digits)
                if trail_sl < pos.sl and trail_sl > current_price:
                    new_sl = trail_sl
                    reason = f"Trailing stop ({trail_mult}x ATR)"
                    logger.debug(f"[{symbol}] {reason} candidate → {new_sl:.2f}")

    # ── RULE 3: SMART SL BUFFER (anti-stop-hunt) ────────────
    # Only if no trailing/BE triggered this bar
    sl_distance = abs(current_price - pos.sl)
    sl_buffer_zone = atr * 0.3

    if getattr(config, 'SL_BUFFER_ENABLED', False) and sl_distance < sl_buffer_zone and new_sl is None:
        # Check ML confidence on current signal
        ml_confident = _check_ml_confidence(last, state, ml_model)

        if ml_confident:
            # Give SL extra room — price might bounce back
            if direction == 1:
                expanded_sl = round(pos.sl - atr * 0.2, digits)
                # Only expand if we haven't hit break-even yet
                if expanded_sl < entry and expanded_sl != pos.sl:
                    new_sl = expanded_sl
                    reason = "ML buffer (anti-stop-hunt)"
                    logger.debug(
                        f"[{symbol}] SL BUFFER candidate → {new_sl:.2f} "
                        f"(ML confident, anti-stop-hunt)"
                    )
            else:
                expanded_sl = round(pos.sl + atr * 0.2, digits)
                if expanded_sl > entry and expanded_sl != pos.sl:
                    new_sl = expanded_sl
                    reason = "ML buffer (anti-stop-hunt)"
                    logger.debug(
                        f"[{symbol}] SL BUFFER candidate → {new_sl:.2f} "
                        f"(ML confident, anti-stop-hunt)"
                    )
        else:
            logger.debug(f"[{symbol}] Near SL but ML not confident — keeping SL tight")

    # ── RULE 4: TIGHTEN TP IF TREND WEAKENS ─────────────────
    if progress_pct >= 0.80:
        ml_confident = _check_ml_confidence(last, state, ml_model)

        if not ml_confident and progress_pct < 1.0:
            # Move TP to 90% of distance = guarantee profit
            if direction == 1:
                tight_tp = round(entry + tp_distance * 0.90, digits)
                if tight_tp < pos.tp and tight_tp > current_price:
                    new_tp = tight_tp
                    reason = reason + " + TP tighten" if reason else "TP tightening"
                    logger.debug(
                        f"[{symbol}] TP TIGHTEN candidate → {new_tp:.2f} "
                        f"(trend weakening, locking profit)"
                    )
            else:
                tight_tp = round(entry - tp_distance * 0.90, digits)
                if tight_tp > pos.tp and tight_tp < current_price:
                    new_tp = tight_tp
                    reason = reason + " + TP tighten" if reason else "TP tightening"
                    logger.debug(
                        f"[{symbol}] TP TIGHTEN candidate → {new_tp:.2f} "
                        f"(trend weakening, locking profit)"
                    )

    # ── APPLY CHANGES ────────────────────────────────────────
    if new_sl is not None or new_tp is not None:
        final_sl = new_sl if new_sl is not None else pos.sl
        final_tp = new_tp if new_tp is not None else pos.tp

        # Validate before sending
        min_stop = info.trade_stops_level * info.point if info else 0

        valid = True
        if direction == 1:
            if final_sl >= current_price:
                logger.warning(f"[{symbol}] Invalid SL {final_sl} >= price {current_price}")
                valid = False
            if final_tp <= current_price:
                logger.warning(f"[{symbol}] Invalid TP {final_tp} <= price {current_price}")
                valid = False
        else:
            if final_sl <= current_price:
                logger.warning(f"[{symbol}] Invalid SL {final_sl} <= price {current_price}")
                valid = False
            if final_tp >= current_price:
                logger.warning(f"[{symbol}] Invalid TP {final_tp} >= price {current_price}")
                valid = False

        if valid:
            old_sl, old_tp = pos.sl, pos.tp
            result = _modify_position(pos.ticket, final_sl, final_tp)
            if result:
                # Update state
                state['sl'] = final_sl
                state['tp'] = final_tp
                event(
                    f"MODIFY {symbol} #{ticket} | SL {old_sl:.2f}→{final_sl:.2f} "
                    f"TP {old_tp:.2f}→{final_tp:.2f} | uPnL {unrealized:+.1f} | {reason}"
                )
                # Send Telegram Alert
                send(msg_adjusted(symbol, ticket, old_sl, final_sl, old_tp, final_tp, reason))
            else:
                logger.warning(f"[{symbol}] Position {ticket} modify failed")


def _get_position_by_ticket(ticket):
    """Get the open position for this ticket"""
    positions = mt5.positions_get(ticket=ticket)
    if positions is None or len(positions) == 0:
        return None
    return positions[0]


def _modify_position(ticket, new_sl, new_tp):
    """Send position modify request to MT5"""
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       new_sl,
        "tp":       new_tp,
    }
    result = mt5.order_send(request)
    if result is None:
        return False
    return result.retcode == mt5.TRADE_RETCODE_DONE


def _check_ml_confidence(last_row, state, ml_model):
    """
    Ask ML if it still believes in the current trade direction.
    Returns True if ML confidence > 55%
    """
    try:
        if not ml_model.trained:
            return True  # no model yet — assume confident

        signal_dict = {
            'signal':       'BUY' if state['direction'] == 1 else 'SELL',
            'atr_pct':      float(last_row.get('atr_pct',      0)),
            'rsi':          float(last_row.get('rsi',           50)),
            'close_vs_ema': float(last_row.get('close_vs_ema',  0)),
            'body_pct':     float(last_row.get('body_pct',      0)),
            'hour_utc':     int  (last_row.get('hour_utc',      0)),
            'bull_sweep':   bool (last_row.get('bull_sweep',    False)),
            'bear_sweep':   bool (last_row.get('bear_sweep',    False)),
            'bull_bos':     bool (last_row.get('bull_bos',      False)),
            'bear_bos':     bool (last_row.get('bear_bos',      False)),
        }
        _, conf = ml_model.predict(signal_dict, 0.0)
        return conf >= 0.55

    except Exception as e:
        logger.warning(f"ML confidence check failed: {e}")
        return True
