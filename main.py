# ============================================================
#   main.py  —  BOSS SMC BOT  v1.9.0
#   Deep Improvements: High-Freq Loop + Continuous Learning
# ============================================================

import time
import logging
import os
import sys
import warnings

# Silence the harmless "could not find physical cores" warning from joblib/loky
# on Windows Store Python. Must be set BELOW the total core count (a value >=
# total makes loky still probe for physical cores) and before parallel code runs.
_cores = os.cpu_count() or 2
os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(max(1, _cores // 2)))
# Belt-and-suspenders: filter the warnings by message text as well.
warnings.filterwarnings("ignore", message=r".*physical cores.*")
warnings.filterwarnings("ignore", message=r".*Trying to unpickle estimator.*")

import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

import config
from core.mt5_connector  import (
    connect, disconnect, is_connected,
    get_candles, get_candles_history,
    get_spread_percent,
    place_buy, place_sell,
    get_open_positions,
    get_closed_trades, get_account_info
)
from core.smc_engine     import compute_features, get_latest_signal
from core.backtester     import run_backtest
from core.trade_manager  import manage_open_trade
from core.mtf_engine     import scan_all_timeframes, h1_trend_direction, h1_gate_ok
from core.sr_engine      import scan_sr
from core                import risk_manager
from core.state_logger   import event, signal_log
from ml.model            import SMCModel, TradeLogger
from tg_alerts.alerts    import (send, msg_signal, msg_closed,
                                  msg_bot_start, msg_error, msg_ml_retrain,
                                  msg_blacklist, msg_milestone)

# ── LOGGING ──────────────────────────────────────────────────
os.makedirs("logs",   exist_ok=True)
os.makedirs(os.path.dirname(config.MODEL_PATH), exist_ok=True)
os.makedirs(os.path.dirname(config.TRADE_LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("BossBot")

# Quiet noisy third-party loggers (httpx prints the full Telegram URL incl. token)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ── STATE PER SYMBOL ─────────────────────────────────────────
def fresh_state():
    return {
        'last_trade_bar':  -999,
        'last_signal_bar': -999,
        'last_scan_bar':   -999,   # last bar we ran an entry scan (placed or not)
        'positions': [],   # list of {ticket, entry, tp, sl, direction}
        'feature_cache':   None,
        'cache_time':      datetime.min,
        'last_mod_time':   0,
        'consumed':        {},   # SR engine: {day_ordinal: set((level, dir))}
        'consumed_day':    -1,
    }

symbol_state = {sym: fresh_state() for sym in config.SYMBOLS}

ml_model             = SMCModel()
trade_logger         = TradeLogger()
trades_since_retrain = 0
last_retrain_time    = datetime.now()
risk_state           = risk_manager.RiskState()

def run_initial_training():
    force = getattr(config, 'FORCE_INITIAL_TRAINING', False)
    if os.path.exists(config.MODEL_PATH) and not force:
        logger.info("ML model already exists, skipping initial training")
        return

    train_days = getattr(config, 'INITIAL_TRAIN_DAYS', 30)
    logger.info(f"Running {'forced ' if force else ''}initial backtest for ML training ({train_days} days)...")
    all_trades = []
    for symbol in config.SYMBOLS:
        df = get_candles_history(symbol, config.TIMEFRAME, days=train_days)
        if df is None or len(df) < 500: continue
        results, trade_log = run_backtest(df, symbol=symbol)
        if 'outcome' in trade_log.columns:
            closed = trade_log.dropna(subset=['outcome'])
            if len(closed) > 0: all_trades.append(closed)

    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
        if ml_model.train(combined):
            logger.info("✓ ML trained and ready!")

def process_symbol_hf(symbol):
    """High-frequency processing for a single symbol"""
    global trades_since_retrain, last_retrain_time
    state = symbol_state[symbol]
    import MetaTrader5 as mt5

    # 1. Update Closed Trades & Outcome
    closed_list = get_closed_trades(symbol, count=10)
    all_mt5_pos = get_open_positions(symbol)
    tickets_in_mt5 = {p.ticket for p in all_mt5_pos}

    still_open = []
    for pos_state in state['positions']:
        ticket = pos_state['ticket']
        if ticket in tickets_in_mt5:
            still_open.append(pos_state)
        else:
            for deal in (closed_list or []):
                if deal.get('position_id') == ticket:
                    profit = deal['profit']
                    trade_logger.update_outcome(symbol, ticket, profit)
                    trades_since_retrain += 1
                    stats = trade_logger.get_stats()
                    event(f"CLOSE {symbol} #{ticket} → {'WIN' if profit>0 else 'LOSS'} "
                          f"{profit:+.2f} | WR {stats['winrate']:.0f}% ({stats['wins']}W-{stats['losses']}L)")
                    from core.state_logger import trade_log as _tl
                    _tl.forget(ticket)  # stop tracking this ticket's progress line
                    send(msg_closed(symbol, "BUY" if pos_state['direction'] == 1 else "SELL",
                                   pos_state['entry'], deal['price'], profit, profit > 0, stats))
                    break
    state['positions'] = still_open

    # 2. Continuous Learning (Rolling Window)
    now = datetime.now()
    if config.ML_ENABLED:
        time_elapsed = (now - last_retrain_time).total_seconds() / 3600
        if (trades_since_retrain >= config.RETRAIN_MIN_NEW_TRADES and time_elapsed >= config.RETRAIN_INTERVAL_HOURS) or \
           (trades_since_retrain >= config.ML_RETRAIN_EVERY):
            log_df = trade_logger.load()
            closed_log = log_df.dropna(subset=['outcome'])
            if len(closed_log) >= config.ML_MIN_TRADES:
                logger.info(f"Retraining ML on rolling window of {min(len(closed_log), config.MAX_TRAINING_TRADES)} trades...")
                ml_model.train(closed_log)
                trades_since_retrain = 0
                last_retrain_time = now
                stats = trade_logger.get_stats()
                n_patterns = len(ml_model.patterns['whitelist']) + len(ml_model.patterns['blacklist'])
                send(msg_ml_retrain(len(closed_log), stats['winrate'], n_patterns))

    # 3. Feature Cache (Every 10s or New Bar)
    cur_bar = _get_cur_bar(symbol)
    if (now - state['cache_time']).total_seconds() > 10 or cur_bar != state['last_signal_bar']:
        df_base = get_candles(symbol, "M1", count=500)
        if df_base is not None:
            state['feature_cache'] = compute_features(df_base)
            state['cache_time'] = now
            state['last_signal_bar'] = cur_bar

    # 4. Smart Trade Manager (Every Loop with Rate Limit)
    if state['feature_cache'] is not None:
        now_ts = time.time()
        if (now_ts - state['last_mod_time']) >= config.MIN_ORDER_INTERVAL_SECONDS:
            for pos_state in state['positions']:
                manage_open_trade(symbol, pos_state, state['feature_cache'], ml_model)
                state['last_mod_time'] = time.time() # Update after mod attempt

    # 5. New Entry Scan — at most ONCE per new bar of the engine's TF
    #    (whether or not a trade is placed), so blocked entries don't cause a
    #    per-second scan/log storm.
    engine = getattr(config, 'ENTRY_ENGINE', 'SR_H1')
    scan_tf = "H1" if engine == "SR_H1" else "M1"
    cur_bar = _get_cur_bar(symbol, scan_tf)
    if cur_bar != state['last_scan_bar'] and len(state['positions']) < config.MAX_POSITIONS:
        state['last_scan_bar'] = cur_bar
        if engine == "SR_H1":
            # ENGINE C (duel winner): session+news filters live inside scan_sr
            _refresh_consumed(state)
            decision = scan_sr(
                get_candles(symbol, "H1", count=320),
                get_candles(symbol, "H4", count=200),
                state['consumed'], symbol)
            if decision:
                event(f"SR SIGNAL {symbol} {decision['direction']} {decision['summary']}")
                execute_trade(symbol, decision, cur_bar)
        else:
            if _in_session():
                decision = scan_all_timeframes(symbol, get_candles)
                if decision:
                    # v1.9 H1 trend gate — block entries against the H1 EMA trend
                    h1_dir = h1_trend_direction(get_candles(symbol, "H1", count=300))
                    if h1_gate_ok(decision['direction'], h1_dir):
                        execute_trade(symbol, decision, cur_bar)
                    else:
                        signal_log.log(f"{symbol}:h1",
                                       f"● HOLD {symbol} {decision['direction']} — against H1 trend")

def _refresh_consumed(state):
    """SR engine: keep per-day consumed-level sets, drop old days."""
    today = datetime.utcnow().date().toordinal()
    if state.get('consumed_day') != today:
        state['consumed'] = {d: s for d, s in state.get('consumed', {}).items()
                             if today - d <= 2}
        state['consumed_day'] = today


def execute_trade(symbol, decision, cur_bar):
    state = symbol_state[symbol]
    import MetaTrader5 as mt5

    direction = decision['direction']
    signal = decision['signal']
    atr = signal['atr']                 # ATR of the timeframe this trade was opened on
    chosen_tf = decision.get('chosen_tf', 'M1')

    # ── Direction switches (now actually enforced) ────────────
    if direction == 'BUY' and not config.ENABLE_BUY:
        return
    if direction == 'SELL' and not config.ENABLE_SELL:
        return

    # FIX: enforce the same COOLDOWN_BARS the backtest used. Previously
    # last_trade_bar was stored but never checked, so live re-entered on the
    # very next bar after a close — live behavior != validated behavior.
    if state['last_trade_bar'] > 0 and \
       (cur_bar - state['last_trade_bar']) < config.COOLDOWN_BARS * 60:
        return

    # ── Volatility-regime guard (skip news-spike conditions) ──
    max_atr_pct = getattr(config, 'MAX_ATR_PCT', 0.0)
    if max_atr_pct > 0 and float(signal.get('atr_pct', 0)) > max_atr_pct:
        event(f"SKIP {symbol} {direction} — volatility too high "
              f"(ATR% {signal.get('atr_pct', 0):.2f} > {max_atr_pct})")
        return

    # ── Risk: cap concurrent positions (fewer on small balances) ──
    acc = get_account_info()
    balance = float(acc.balance) if acc else config.SMALL_BALANCE_THRESHOLD
    max_allowed = risk_manager.positions_allowed(balance)
    n_pos = min(decision['n_positions'], max_allowed - len(state['positions']))
    if n_pos <= 0:
        return

    # ── Spread filter ─────────────────────────────────────────
    spread_pct = get_spread_percent(symbol)
    if spread_pct > config.MAX_SPREAD:
        return

    # ── ML filter + pattern-match alerts (single source of truth) ──
    # The SR_H1 engine is validated WITHOUT the ML gate (duel parity) — the
    # SMC ML model was trained on M1 SMC features, not on H1 breakouts.
    if decision.get('engine') == 'SR_H1':
        ml_ok, ml_conf = True, 1.0
    else:
        ml_ok, ml_conf = ml_model.predict(signal, spread_pct)
    if config.PATTERN_CLUSTERING_ENABLED:
        features = [float(signal.get(col, 0)) for col in ml_model.FEATURE_COLS]
        match = ml_model.match_pattern(features)
        if match == "whitelist":
            send(msg_blacklist(symbol, direction, ml_conf - config.WHITELIST_CONFIDENCE_BOOST, "WHITELIST"))
        elif match == "blacklist" and not ml_ok:
            send(msg_blacklist(symbol, direction, ml_conf, "BLACKLIST"))

    if not ml_ok:
        return

    info = mt5.symbol_info(symbol)
    if info is None:
        event(f"SKIP {symbol} {direction} — symbol info not found (is it in Market Watch?)")
        return
    digits = info.digits
    placed = 0
    price = tp = sl = 0.0
    lot = config.LOT_SIZE

    # FIX: track the real POSITION tickets (MT5 manages positions, not order
    # ids). Snapshot what's open now so we can identify the position we just
    # opened — placing used to store result.order, which positions_get() never
    # matches, so open trades could go unmanaged.
    prior_tickets = {p.ticket for p in get_open_positions(symbol)}

    for i in range(n_pos):
        tick = mt5.symbol_info_tick(symbol)
        price = tick.ask if direction == 'BUY' else tick.bid
        if decision.get('sl_price') is not None:
            # SR_H1 engine (ENGINE C): level-based SL, TP at RR*R
            sl = float(decision['sl_price'])
            sl_dist = abs(price - sl)
            max_sl = getattr(config, 'SR_MAX_SL_ATR', 2.2) * atr
            min_sl = getattr(config, 'SR_MIN_SL_ATR', 0.25) * atr
            if not (min_sl <= sl_dist <= max_sl):
                event(f"SKIP {symbol} {direction} — SL distance {sl_dist:.2f} outside "
                      f"[{min_sl:.2f},{max_sl:.2f}]")
                break
            rr = decision.get('rr', 2.0)
            tp = price + rr * sl_dist if direction == 'BUY' else price - rr * sl_dist
        else:
            tp = round(price + (atr * decision['tp_mult'] if direction == 'BUY' else -atr * decision['tp_mult']), digits)
            sl = round(price - (atr * decision['sl_mult'] if direction == 'BUY' else -atr * decision['sl_mult']), digits)
        sl = round(sl, digits)
        tp = round(tp, digits)

        # Risk-based lot sizing (compounds as balance grows)
        lot = risk_manager.calc_lot_size(symbol, price, sl, balance)

        # Final safety gate — blocks NEW entries on loss-limit/drawdown/margin
        allowed, reason = risk_manager.can_open_new_trade(risk_state, symbol, lot)
        if not allowed:
            # change-only: logs when the block STARTS or the reason changes,
            # not every bar the signal persists
            signal_log.log(f"{symbol}:gate", f"● HOLD {symbol} — {reason}; skipping new entry")
            break

        ticket, err = (place_buy if direction == 'BUY' else place_sell)(symbol, lot, sl, tp, comment=f"BOSS {direction}")
        if ticket:
            # Resolve the actual position ticket (fall back to the order id)
            new_pos = get_open_positions(symbol)
            new_tickets = {p.ticket for p in new_pos} - prior_tickets
            if len(new_tickets) == 1:
                pos_ticket = new_tickets.pop()
            else:
                pos_ticket = ticket
            state['positions'].append({'ticket': pos_ticket, 'entry': price, 'tp': tp, 'sl': sl,
                                       'direction': 1 if direction == 'BUY' else -1,
                                       'atr': atr, 'tf': chosen_tf})
            state['last_trade_bar'] = cur_bar
            trade_logger.log_trade(symbol, signal, price, tp, sl, spread_pct, ticket=pos_ticket)
            event(f"OPEN {symbol} {direction} #{pos_ticket} @ {price:.2f} | TF {chosen_tf} "
                  f"| lot {lot} | SL {sl:.2f} TP {tp:.2f} | ML {ml_conf:.0%}")
            placed += 1
            time.sleep(0.1)
        else:
            event(f"REJECT {symbol} {direction} — {err}")

    if placed > 0:
        send(msg_signal(symbol, direction, price, tp, sl, atr, signal.get('rsi', 0),
                        f"MTF x{placed}", ml_conf, lot * placed))

def _get_cur_bar(symbol, timeframe="M1"):
    import MetaTrader5 as mt5
    tf = {"M1": mt5.TIMEFRAME_M1, "H1": mt5.TIMEFRAME_H1,
          "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}.get(timeframe, mt5.TIMEFRAME_M1)
    rates = mt5.copy_rates_from_pos(symbol, tf, 1, 1)
    return int(rates[0]['time']) if rates is not None and len(rates) > 0 else 0

def _in_session():
    """True if trading is allowed now. Respects SESSION_FILTER (UTC hours)."""
    if not getattr(config, 'SESSION_FILTER', False):
        return True
    hour = datetime.now(timezone.utc).hour
    start, end = config.SESSION_START, config.SESSION_END
    in_window = (start <= hour < end) if start <= end else (hour >= start or hour < end)
    if not in_window:
        signal_log.log("session", f"Outside trading session (UTC {hour}h) — new entries paused")
    return in_window

def main():
    logger.info("=" * 57)
    logger.info(f"  BOSS SMC BOT  v1.9.0  —  High-Freq Loop")
    logger.info("=" * 57)

    if not connect():
        logger.error("MT5 connection failed.")
        sys.exit(1) # Wrapper will restart

    run_initial_training()
    send(msg_bot_start(config.SYMBOLS))

    try:
        while True:
            start_time = time.time()
            
            if not is_connected():
                logger.warning("MT5 lost — reconnecting...")
                if not connect():
                    time.sleep(10)
                    continue

            # Refresh risk state (day PnL, peak equity) and check milestones
            acc = get_account_info()
            risk_state.update(acc)
            milestone = risk_manager.check_withdrawal_milestone(risk_state)
            if milestone:
                event(f"MILESTONE — profit ${milestone['profit']:.2f}; "
                      f"withdraw ${milestone['withdraw']} keep ${milestone['keep']:.2f}")
                send(msg_milestone(milestone))

            for symbol in config.SYMBOLS:
                try:
                    process_symbol_hf(symbol)
                except Exception as e:
                    logger.error(f"Error processing {symbol}: {e}")
            
            # Target Interval
            elapsed = time.time() - start_time
            sleep_time = max(0.1, config.ANALYSIS_INTERVAL_SECONDS - elapsed)
            time.sleep(sleep_time)
            
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"CRITICAL ERROR: {e}")
        with open(config.CRASH_LOG_PATH, "a") as f:
            f.write(f"{datetime.now()}: {e}\n")
        sys.exit(1) # Exit with error for auto-restart

if __name__ == "__main__":
    main()
