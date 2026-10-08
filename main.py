# ============================================================
#   main.py  —  BOSS SMC BOT  v1.9.4
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
    get_spread_percent, get_symbol_info,
    place_buy, place_sell,
    get_open_positions, get_position_by_ticket,
    close_position, modify_position,
    get_closed_trades, get_account_info
)
from core.smc_engine     import compute_features, get_latest_signal, calc_atr, calc_rsi
from core.backtester     import run_backtest
from core.trade_manager  import manage_open_trade
from core.mtf_engine     import scan_all_timeframes, h1_trend_direction, h1_gate_ok
from core.sr_engine      import scan_sr
from core                import flash_engine
from core                import risk_manager
from core.ai_review      import AIReviewer
from core.ai_position    import PositionReviewer
from core.ai_regime      import RegimeReviewer
from core.news           import NewsWatcher
from core.ai_journal     import AIJournal
from core.state_logger   import event, signal_log
from ml.model            import SMCModel, TradeLogger
from tg_alerts.alerts    import (send, msg_signal, msg_closed,
                                  msg_bot_start, msg_error, msg_ml_retrain,
                                  msg_blacklist, msg_milestone,
                                  msg_ai_decision, msg_ai_position,
                                  msg_news, msg_regime)

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
        'ai_reject_cooldown_until': 0,  # pause re-reviews after a final AI reject
    }

symbol_state = {sym: fresh_state() for sym in config.SYMBOLS}

ml_model             = SMCModel()
trade_logger         = TradeLogger()
trades_since_retrain = 0
last_retrain_time    = datetime.now()
risk_state           = risk_manager.RiskState()

# Consecutive-loss circuit breaker (2026-10-07): 10 straight losses in the
# FOMC whipsaw showed the daily halt alone reacts too slowly. After
# CONSEC_LOSS_LIMIT losses in a row, all new entries pause for the cooldown.
consec_losses        = 0
entry_pause_until    = 0.0

# AI entry reviewer (feature-flagged). When AI_ENABLED is False the reviewer
# is a no-op: it makes no provider call and writes no journal, so the bot
# behaves exactly as before. See config.AI_* and core/ai_review.py.
ai_reviewer = None
if getattr(config, 'AI_ENABLED', False) and getattr(config, 'AI_PROVIDER', 'disabled') != 'disabled':
    ai_journal  = AIJournal(config.AI_JOURNAL_PATH)
    ai_reviewer = AIReviewer(ai_journal)
    logger.info(f"AI entry review ENABLED (provider={config.AI_PROVIDER}, "
                f"model={config.AI_MODEL}, min_conf={config.AI_MIN_CONFIDENCE})")
else:
    logger.info("AI entry review disabled — signals flow straight to risk checks")

# AI position manager (reviews OPEN trades: HOLD / CLOSE / EXTEND).
# Needs the same AI_ENABLED + AI_PROVIDER flags, plus AI_POSITION_ENABLED.
ai_position_reviewer = None
if (ai_reviewer is not None
        and getattr(config, 'AI_POSITION_ENABLED', False)):
    ai_position_reviewer = PositionReviewer(ai_journal)
    logger.info(f"AI position manager ENABLED (every {config.AI_POSITION_INTERVAL:.0f}s "
                f"per open trade | EXTEND at >= {config.AI_EXTEND_MIN_PROGRESS:.0%} to TP "
                f"| min_conf {config.AI_POSITION_MIN_CONFIDENCE})")

# News watcher — free RSS feed; major headlines go to Telegram, all recent
# headlines feed the AI decision snapshots as untrusted data.
news_watcher = None
if getattr(config, 'NEWS_ENABLED', False):
    news_watcher = NewsWatcher()
    logger.info(f"News feed ENABLED ({config.NEWS_RSS_URL} every "
                f"{config.NEWS_POLL_SECONDS:.0f}s, keyword alerts)")

# AI market adaptation — self-tunes whitelisted settings within hard
# clamped ranges to match the market regime. Runtime-only; restart resets
# to the .env baseline. Never touches loss limits, stops or sessions.
ai_regime = None
if (ai_reviewer is not None
        and getattr(config, 'AI_REGIME_ENABLED', False)):
    ai_regime = RegimeReviewer(ai_journal)
    logger.info(f"AI market adaptation ENABLED (every {config.AI_REGIME_INTERVAL/3600:.1f}h | "
                f"adjustable: {', '.join(config.AI_REGIME_ALLOWED)})")
else:
    logger.info("AI entry review disabled — signals flow straight to risk checks")

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

def adopt_open_positions():
    """Re-adopt positions left open by a previous (crashed/restarted) run.

    Without this, trades opened before a restart would be invisible to the
    bot: no break-even/trailing management, and their close outcome would
    never be logged (step 1 only tracks tickets opened by this process).
    The original atr/tf are unknown after restart — the manager falls back
    to the live ATR (state.get('atr') or live_atr), which is safe.
    """
    import MetaTrader5 as mt5
    adopted_total = 0
    for symbol in config.SYMBOLS:
        state = symbol_state[symbol]
        known = {ps['ticket'] for ps in state['positions']}
        for p in (get_open_positions(symbol) or []):
            if p.ticket in known:
                continue
            direction = 1 if p.type == mt5.ORDER_TYPE_BUY else -1
            state['positions'].append({
                'ticket': p.ticket, 'entry': p.price_open, 'tp': p.tp,
                'sl': p.sl, 'direction': direction, 'atr': 0.0, 'tf': 'H1',
                'volume': p.volume, 'adopted': True})
            known.add(p.ticket)
            adopted_total += 1
            event(f"ADOPT {symbol} {'BUY' if direction == 1 else 'SELL'} #{p.ticket} "
                  f"@ {p.price_open:.2f} SL {p.sl:.2f} TP {p.tp:.2f} "
                  f"(re-adopted after restart)")
    # FLASH: rebuild the pair (main + optional opposite hedge) from adopted
    # positions so the pair stays managed across restarts.
    if getattr(config, 'ENTRY_ENGINE', 'SR_H1') == 'FLASH' and adopted_total:
        for symbol in config.SYMBOLS:
            state = symbol_state[symbol]
            if flash_engine.has_open_pair(state):
                continue
            adopted = [ps for ps in state['positions'] if ps.get('adopted')]
            if not adopted:
                continue
            main = min(adopted, key=lambda ps: ps['ticket'])
            hedge = next((ps for ps in adopted
                          if ps['direction'] != main['direction']), None)
            flash_engine.register_main(state, {
                'ticket': main['ticket'], 'dir': main['direction'],
                'entry': main['entry'], 'sl': main['sl'], 'tp': main['tp'],
                'init_sl': main['sl'], 'lot': main.get('volume', config.LOT_SIZE),
                'atr': 0.0, 'slm': 1.5, 'tpm': 3.0, 'opened_ts': time.time()})
            if hedge is not None:
                flash_engine._flash(state)['pair']['hedge'] = {
                    'ticket': hedge['ticket'], 'dir': hedge['direction'],
                    'entry': hedge['entry'], 'sl': hedge['sl'], 'tp': hedge['tp'],
                    'init_sl': hedge['sl'], 'lot': hedge.get('volume', config.LOT_SIZE),
                    'atr': 0.0, 'slm': 1.5, 'tpm': 3.0, 'opened_ts': time.time()}
            event(f"FLASH ADOPT {symbol}: pair rebuilt "
                  f"({'+hedge' if hedge else 'no hedge'}), hedges_used kept")
    if adopted_total:
        logger.info(f"Re-adopted {adopted_total} open position(s) from previous run")
    return adopted_total

def review_open_positions_ai(symbol, state):
    """AI position manager: periodically review each open position and act on
    an approved CLOSE (market close) or EXTEND (TP → 120% of original distance
    + SL locked at AI_EXTEND_LOCK_PCT of floating profit). Everything else is
    HOLD — the existing break-even/trailing rules keep managing the trade."""
    import MetaTrader5 as mt5
    for ps in list(state['positions']):
        ticket = ps['ticket']
        p = get_position_by_ticket(ticket)
        if p is None or not p.tp:
            continue
        tp_dist = abs(p.tp - p.price_open)
        if tp_dist <= 0:
            continue
        direction = 1 if p.type == mt5.ORDER_TYPE_BUY else -1
        progress = ((p.price_current - p.price_open) if direction == 1
                    else (p.price_open - p.price_current)) / tp_dist
        if not ai_position_reviewer.due(ticket, progress):
            continue

        spread_pct = None
        try:
            spread_pct = get_spread_percent(symbol)
        except Exception:
            pass
        atr = rsi = None
        h1 = get_candles(symbol, "H1", count=120)
        if h1 is not None and len(h1) > 20:
            try:
                atr = float(calc_atr(h1, 14).iloc[-1])
                rsi = float(calc_rsi(h1['close'], 14).iloc[-1])
            except Exception:
                pass

        ctx = ai_position_reviewer.build_context(
            symbol=symbol, direction=direction, entry=p.price_open,
            current_price=p.price_current, sl=p.sl, tp=p.tp,
            floating_usd=p.profit, progress=progress,
            minutes_open=max(0.0, (time.time() - p.time) / 60.0),
            atr=atr, rsi=rsi, spread_pct=spread_pct,
            open_positions=sum(len(symbol_state[s]['positions'])
                               for s in config.SYMBOLS),
            max_positions=config.MAX_POSITIONS)
        if news_watcher is not None:
            ctx["recent_news"] = news_watcher.recent_titles(5)
        ctx["recent_performance"] = recent_performance()
        res = ai_position_reviewer.review(ctx, ticket=ticket)
        ai_position_reviewer.mark_reviewed(ticket)
        dir_str = "BUY" if direction == 1 else "SELL"

        # One log line per review — always shows which models + keys voted
        event(f"AI REVIEW {symbol} {dir_str} #{ticket} → {res.action} "
              f"(conf {res.confidence:.2f} | {res.model} | {res.key_number}) "
              f"— {res.reason_code}")

        if res.action == "CLOSE" and res.approved:
            ok, detail = close_position(symbol, ticket, "BOSS AI CLOSE")
            if ok:
                event(f"AI CLOSE {symbol} {dir_str} #{ticket} — "
                      f"{res.reason_code} | banked {p.profit:+.2f} USD")
                send(msg_ai_position(
                    "CLOSE", symbol, dir_str, res.confidence,
                    res.reason_code, res.reason, ticket,
                    model=res.model, key=res.key_number,
                    detail=f"💵 Banked ~<b>{p.profit:+.2f} USD</b>"))
            else:
                logger.error(f"AI CLOSE order failed for #{ticket}: {detail}")
                try:
                    ai_reviewer.journal.mark_execution(
                        res.decision_id, ticket=ticket,
                        execution="close_failed",
                        final_action="ai_close_failed", risk_result="failed")
                except Exception:
                    pass
            continue

        if res.action == "EXTEND" and res.approved and not ps.get('ai_extended'):
            info = mt5.symbol_info(symbol)
            digits = info.digits if info else 5
            mult = float(getattr(config, 'AI_EXTEND_TP_MULT', 1.2))
            lock = float(getattr(config, 'AI_EXTEND_LOCK_PCT', 0.7))
            if direction == 1:
                new_tp = round(p.price_open + tp_dist * mult, digits)
                giveback = (1.0 - lock) * max(0.0, p.price_current - p.price_open)
                new_sl = round(p.price_current - giveback, digits)
                tp_ok = new_tp > p.tp
                sl_ok = new_sl > p.sl and new_sl < p.price_current
            else:
                new_tp = round(p.price_open - tp_dist * mult, digits)
                giveback = (1.0 - lock) * max(0.0, p.price_open - p.price_current)
                new_sl = round(p.price_current + giveback, digits)
                tp_ok = new_tp < p.tp
                sl_ok = new_sl < p.sl and new_sl > p.price_current
            if tp_ok and sl_ok and modify_position(ticket, new_sl, new_tp):
                ps['ai_extended'] = True
                event(f"AI EXTEND {symbol} {dir_str} #{ticket} — "
                      f"TP {p.tp:.2f}→{new_tp:.2f}, SL locked {new_sl:.2f}")
                send(msg_ai_position(
                    "EXTEND", symbol, dir_str, res.confidence,
                    res.reason_code, res.reason, ticket,
                    model=res.model, key=res.key_number,
                    detail=f"✅ TP → <code>{new_tp:.2f}</code>   ·   "
                           f"🔒 SL locked → <code>{new_sl:.2f}</code>"))
            else:
                logger.warning(f"AI EXTEND not applied for #{ticket} "
                               f"(tp_ok={tp_ok} sl_ok={sl_ok})")


# ── AI performance briefing (the AI's "memory" of live results) ──
# LLMs are stateless between calls, so we hand each decision a compact
# summary of how recent REAL trades and past AI approvals turned out.
_perf_cache = {'t': 0.0, 'data': None}


def recent_performance():
    """Compact live-performance briefing for AI decisions (60s cache)."""
    now = time.time()
    if _perf_cache['data'] is not None and now - _perf_cache['t'] < 60:
        return _perf_cache['data']
    import pandas as pd
    data = {}
    try:
        df = trade_logger.load()
        # Only show last 5 trades to the AI — showing 20 trades worth of old
        # losses makes the AI permanently refuse in "STALE_LOSS_STREAK" mode.
        closed = df.dropna(subset=['outcome']).tail(5) \
            if 'outcome' in df.columns else df.iloc[0:0]
        wins = int((closed['outcome'] > 0).sum()) if len(closed) else 0
        block = {'wins': wins, 'losses': int(len(closed) - wins)}
        if len(closed):
            block['winrate_pct'] = round(100 * wins / len(closed), 1)
            profits = pd.to_numeric(closed.get('profit'), errors='coerce').dropna()
            if len(profits):
                block['avg_profit_usd'] = round(float(profits.mean()), 2)
            block['sequence'] = ''.join('W' if o > 0 else 'L'
                                        for o in closed['outcome'].tail(10))
        data['last_20_real_trades'] = block
    except Exception as e:
        logger.error(f"perf briefing (trade log) failed: {type(e).__name__}")
    try:
        jdf = pd.read_csv(config.AI_JOURNAL_PATH)
        app = jdf[(jdf['kind'] == 'entry') & (jdf['execution'] == 'executed')
                  & (jdf['outcome'].notna())].tail(20)
        w = int((app['outcome'] > 0).sum())
        data['ai_approved_trades'] = {'wins': w, 'losses': int(len(app) - w)}
    except Exception as e:
        logger.error(f"perf briefing (AI journal) failed: {type(e).__name__}")
    _perf_cache['t'] = now
    _perf_cache['data'] = data
    return data


def run_ai_regime_review():
    """AI market adaptation: review the regime, apply clamped setting
    changes, log + alert everything."""
    symbols_stats = {}
    for sym in config.SYMBOLS:
        h1 = get_candles(sym, "H1", count=120)
        if h1 is None or len(h1) < 30:
            continue
        try:
            atr = float(calc_atr(h1, 14).iloc[-1])
            close = float(h1['close'].iloc[-1])
            if close > 0 and atr == atr and atr > 0:
                symbols_stats[sym] = {"atr_pct": round(atr / close * 100, 3),
                                      "last_close": round(close, 5)}
        except Exception:
            continue
    acc = get_account_info()
    regime, summary, applied = ai_regime.run(
        symbols_stats, acc,
        news_watcher.recent_titles(5) if news_watcher else [],
        perf=recent_performance())
    event(f"AI REGIME → {regime} | {summary}")
    if applied:
        for p, old, new, why in applied:
            event(f"AI ADJUST {p}: {old} → {new} ({why})")
        send(msg_regime(regime, summary, applied))


def process_symbol_hf(symbol):
    """High-frequency processing for a single symbol"""
    global trades_since_retrain, last_retrain_time
    state = symbol_state[symbol]
    import MetaTrader5 as mt5

    # 1. Update Closed Trades & Outcome
    closed_list = get_closed_trades(symbol, count=10)
    deep_list = None   # lazy 30-day fallback (positions held longer than 7 days)
    all_mt5_pos = get_open_positions(symbol)
    tickets_in_mt5 = {p.ticket for p in all_mt5_pos}

    still_open = []
    for pos_state in state['positions']:
        ticket = pos_state['ticket']
        if ticket in tickets_in_mt5:
            still_open.append(pos_state)
            continue
        deal = next((d for d in (closed_list or [])
                     if d.get('position_id') == ticket), None)
        if deal is None:
            # A wide-SL/TP position can stay open beyond the default 7-day
            # deal window — retry once with 30 days before giving up.
            if deep_list is None:
                deep_list = get_closed_trades(symbol, count=50, days=30)
            deal = next((d for d in (deep_list or [])
                         if d.get('position_id') == ticket), None)
        if deal is None:
            # Never drop a position silently — outcome/ML data matters.
            event(f"WARN {symbol} #{ticket} no longer open and no deal found "
                  f"— outcome NOT logged (check broker history)")
            continue
        profit = deal['profit']
        trade_logger.update_outcome(symbol, ticket, profit)
        # Link the reconciled closed-trade result back to the AI decision(s)
        # that approved this entry (from the bot's OWN deal only).
        if ai_reviewer is not None:
            try:
                ai_reviewer.journal.link_outcome(symbol, ticket, profit)
            except Exception as e:
                logger.error(f"AI journal link_outcome failed: {type(e).__name__}")
        trades_since_retrain += 1
        # Consecutive-loss circuit breaker — must write to the MODULE-LEVEL
        # globals (the old code used assignment inside a function which Python
        # treated as new local variables, so the breaker never fired).
        global consec_losses, entry_pause_until
        if profit > 0:
            if consec_losses > 0:
                event(f"CIRCUIT-BREAKER reset — win after {consec_losses} loss(es)")
            consec_losses = 0
        else:
            consec_losses += 1
            event(f"LOSS STREAK: {consec_losses} in a row "
                  f"(limit={getattr(config, 'CONSEC_LOSS_LIMIT', 4)})")
            if consec_losses >= int(getattr(config, 'CONSEC_LOSS_LIMIT', 4)):
                pause_min = float(getattr(config, 'CONSEC_LOSS_PAUSE_MIN', 60))
                entry_pause_until = time.time() + pause_min * 60
                event(f"CIRCUIT-BREAKER FIRED — {consec_losses} losses in a row; "
                      f"new entries paused {pause_min:.0f} min until "
                      f"{time.strftime('%H:%M', time.localtime(entry_pause_until))}")
                send(msg_error(f"🛑 Circuit breaker: {consec_losses} consecutive "
                               f"losses — all new entries paused {pause_min:.0f} min"))
                consec_losses = 0  # recount after the pause
        stats = trade_logger.get_stats()
        event(f"CLOSE {symbol} #{ticket} → {'WIN' if profit>0 else 'LOSS'} "
              f"{profit:+.2f} | WR {stats['winrate']:.0f}% ({stats['wins']}W-{stats['losses']}L)")
        from core.state_logger import trade_log as _tl
        _tl.forget(ticket)  # stop tracking this ticket's progress line
        send(msg_closed(symbol, "BUY" if pos_state['direction'] == 1 else "SELL",
                       pos_state['entry'], deal['price'], profit, profit > 0, stats))
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
    engine = getattr(config, 'ENTRY_ENGINE', 'SR_H1')
    now_ts = time.time()
    if (now_ts - state['last_mod_time']) >= config.MIN_ORDER_INTERVAL_SECONDS:
        if engine == "FLASH":
            # FLASH owns its SL changes (profit lock / hedge / flatten)
            if flash_engine.has_open_pair(state):
                flash_engine.manage(symbol, state, risk_state)
                state['last_mod_time'] = time.time()
        elif state['feature_cache'] is not None:
            for pos_state in state['positions']:
                manage_open_trade(symbol, pos_state, state['feature_cache'], ml_model)
                state['last_mod_time'] = time.time() # Update after mod attempt

    # 4b. AI position manager — HOLD / CLOSE / EXTEND on open trades
    # (own cadence; a failure here must never break the trading loop)
    if ai_position_reviewer is not None and state['positions']:
        try:
            review_open_positions_ai(symbol, state)
        except Exception as e:
            logger.error(f"AI position review error ({type(e).__name__}): {e}")

    # 5. New Entry Scan — at most ONCE per new bar of the engine's TF
    #    (whether or not a trade is placed), so blocked entries don't cause a
    #    per-second scan/log storm.
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
        elif engine == "FLASH":
            # Original MTF high-freq flow (no session filter, no H1 gate) —
            # one pair (main + optional hedge) at a time.
            if _in_session() and not flash_engine.has_open_pair(state):
                decision = flash_engine.decide(symbol, get_candles, state)
                if decision:
                    event(f"FLASH SIGNAL {symbol} {decision['direction']} "
                          f"TF {decision.get('chosen_tf', '?')} {decision['signal'].get('rsi', 0):.0f}")
                    execute_trade(symbol, decision, cur_bar)
        else:
            if _in_session() and time.time() >= entry_pause_until:
                decision = scan_all_timeframes(symbol, get_candles)
                if decision:
                    # ATR explosion guard: block entries when current volatility
                    # is far above its own norm (news chaos whipsaws scalps).
                    # atr_sma comes from the signal dict (50-bar rolling mean).
                    _sig     = decision.get('signal') or {}
                    _dec_atr = float(_sig.get('atr') or 0)
                    _avg_atr = float(_sig.get('atr_sma') or 0)
                    if (getattr(config, 'ATR_EXPANSION_BLOCK', 0) > 0
                            and _dec_atr > 0 and _avg_atr > 0
                            and _dec_atr / _avg_atr >= config.ATR_EXPANSION_BLOCK):
                        signal_log.log(f"{symbol}:atrblowout",
                                       f"● {symbol} entry blocked — ATR {_dec_atr:.2f} "
                                       f"is {_dec_atr/_avg_atr:.1f}x its 50-bar average "
                                       f"(news chaos — ATR_EXPANSION_BLOCK "
                                       f"{config.ATR_EXPANSION_BLOCK}x)")
                    else:
                        # v1.9 H1 trend gate — block entries against the H1 EMA trend
                        h1_dir = h1_trend_direction(get_candles(symbol, "H1", count=300))
                        decision['h1_trend'] = h1_dir  # for the AI snapshot
                        if h1_gate_ok(decision['direction'], h1_dir):
                            if time.time() < state.get('ai_reject_cooldown_until', 0):
                                signal_log.log(f"{symbol}:aicooldown",
                                               f"● {symbol} AI-reject cooldown "
                                               f"— re-review paused")
                            else:
                                execute_trade(symbol, decision, cur_bar)
                        else:
                            signal_log.log(f"{symbol}:h1",
                                           f"● HOLD {symbol} {decision['direction']} — against H1 trend")

def _refresh_consumed(state):
    """SR engine: keep per-day consumed-level sets, drop old days."""
    today = datetime.now(timezone.utc).date().toordinal()
    if state.get('consumed_day') != today:
        state['consumed'] = {d: s for d, s in state.get('consumed', {}).items()
                             if today - d <= 2}
        state['consumed_day'] = today


def execute_trade(symbol, decision, cur_bar):
    """AI entry-review gate in FRONT of the existing deterministic path.

    The strategy has already produced `decision`. When the AI reviewer is
    enabled it reviews a sanitized decision-time snapshot and can only VETO:
    a valid APPROVE lets the candidate CONTINUE to the unchanged deterministic
    risk checks / order path in _execute_trade_inner; REJECT / WAIT and any AI
    failure block it (fail-closed). The AI never places/cancels orders, sizes,
    or moves stops. When disabled this is a transparent pass-through.
    """
    state = symbol_state[symbol]
    signal = decision['signal']
    atr = signal['atr']
    chosen_tf = decision.get('chosen_tf', 'M1')
    direction = decision['direction']

    # Normalize engine-specific fields so the AI snapshot is complete for
    # every engine: MTF/scalping decisions carry multipliers instead of
    # absolute SL / reward-risk values.
    if decision.get('engine') is None:
        decision['engine'] = 'SMC_MTF'
    if decision.get('h1_trend') is None:
        try:
            decision['h1_trend'] = h1_trend_direction(
                get_candles(symbol, "H1", count=300))
        except Exception:
            pass
    if decision.get('sl_atr') is None and decision.get('sl_mult') is not None:
        decision['sl_atr'] = decision['sl_mult']
    if decision.get('rr') is None and decision.get('sl_mult'):
        decision['rr'] = round(decision['tp_mult'] / decision['sl_mult'], 2)
    if decision.get('sl_price') is None and decision.get('sl_mult') is not None:
        # Planned SL price (estimate at current tick; only for AI snapshot).
        # NOTE: intentionally NOT set for SMC_MTF — that engine uses sl_mult
        # path in _execute_trade_inner, not the SR_H1 sl_price path.
        _eng = decision.get('engine', 'SMC_MTF')
        if _eng not in ('SMC_MTF', None):
            try:
                info = get_symbol_info(symbol)
                px = (info.bid if direction == 'BUY' else info.ask) if info else None
                a = float(signal.get('atr') or 0)
                if px and a > 0:
                    dist = float(decision['sl_mult']) * a
                    decision['sl_price'] = (px - dist) if direction == 'BUY' else (px + dist)
            except Exception:
                pass

    review = None
    if ai_reviewer is not None and getattr(ai_reviewer, 'enabled', False):
        try:
            try:
                spread_pct = get_spread_percent(symbol)
            except Exception:
                spread_pct = None
            ctx = ai_reviewer.build_context(symbol, decision, signal, atr,
                                            chosen_tf,
                                            open_positions=len(state['positions']),
                                            spread_pct=spread_pct)
            if news_watcher is not None:
                ctx["recent_news"] = news_watcher.recent_titles(5)
            ctx["recent_performance"] = recent_performance()
            review = ai_reviewer.review(ctx)
        except Exception as e:
            # Any unexpected AI-layer error fails CLOSED: block the candidate,
            # never approve, never crash the loop. Log type only (no raw body).
            logger.error(f"AI review error ({type(e).__name__}); "
                         f"entry blocked (fail-closed)")
            send(msg_error(f"AI review failed ({type(e).__name__}) — "
                           f"entry blocked (fail-closed)"))
            return
        # Telegram: surface every AI decision (APPROVE / REJECT / WAIT /
        # blocked) so the owner sees the AI gate working in real time.
        send(msg_ai_decision(review.action, symbol, direction,
                             review.confidence, review.reason_code,
                             review.reason, review.approved,
                             model=review.model, key=review.key_number,
                             appeal=review.appeal_info))
        appeal = f" | {review.appeal_info}" if review.appeal_info else ""
        if not review.approved:
            event(f"AI {review.action} {symbol} {direction} — {review.reason_code} "
                  f"(conf {review.confidence:.2f} | {review.model} | key#"
                  f"{review.key_number}){appeal}; entry blocked")
            # Cooldown: same setup re-fires every M5 bar — skip re-review for 1 bar only.
            # Was 300s (5 min) which silently killed all signals for 5 min per reject.
            state['ai_reject_cooldown_until'] = time.time() + 60
            return
        event(f"AI APPROVE {symbol} {direction} — {review.reason_code} "
              f"(conf {review.confidence:.2f} | {review.model} | key#"
              f"{review.key_number}){appeal} → risk checks")

    block_reason = {'v': None}
    try:
        _execute_trade_inner(symbol, decision, cur_bar, review, block_reason)
    finally:
        # If the AI approved but the candidate never reached an order, record
        # the deterministic outcome. A no-op if the candidate already executed.
        if review is not None:
            try:
                ai_reviewer.journal.finalize_pending(
                    review.decision_id, block_reason=block_reason['v'])
            except Exception as e:
                logger.error(f"AI journal finalize failed: {type(e).__name__}")


def _execute_trade_inner(symbol, decision, cur_bar, review, block_reason):
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

    # ── RSI chop-zone guard (hard gate — all engines) ─────────
    # The MTF engine already filters at scoring time; this catches SR_H1
    # and FLASH which bypass _make_decision. Checked AFTER direction gates
    # so it never logs for disabled directions.
    if getattr(config, 'RSI_FILTER_ENABLED', True):
        _rsi     = float(signal.get('rsi', 50))
        _pat     = int(signal.get('pattern_score', 0))
        _bull_mn = float(getattr(config, 'RSI_BULL_MIN', 52))
        _bear_mx = float(getattr(config, 'RSI_BEAR_MAX', 48))
        _ovr     = int(getattr(config, 'RSI_CHOP_PATTERN_OVERRIDE', 2))
        _blocked = (
            (direction == 'BUY'  and _rsi < _bull_mn and _pat < _ovr) or
            (direction == 'SELL' and _rsi > _bear_mx and _pat < _ovr)
        )
        if _blocked:
            event(f"RSI-CHOP {symbol} {direction} blocked "
                  f"(RSI {_rsi:.1f}, pattern {_pat}) — waiting for momentum")
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
        event(f"SKIP {symbol} {direction} — spread {spread_pct:.3f}% above "
              f"max {config.MAX_SPREAD}%")
        return

    # ── ML filter + pattern-match alerts (single source of truth) ──
    # The SR_H1 engine is validated WITHOUT the ML gate (duel parity) — the
    # SMC ML model was trained on M1 SMC features, not on H1 breakouts.
    if decision.get('engine') == 'SR_H1':
        ml_ok, ml_conf = True, 1.0
    elif decision.get('engine') == 'FLASH' and not getattr(config, 'FLASH_ML_GATE', False):
        # Backtest parity: the FLASH numbers in core/flash_engine.py were
        # validated without the ML gate (AUC 0.512 ≈ coin flip).
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
        _ml_stats = trade_logger.get_stats()
        if _ml_stats.get('total', 0) >= config.ML_MIN_TRADES:
            event(f"ML-VETO {symbol} {direction} — ML confidence {ml_conf:.0%} "
                  f"below required {config.ML_MIN_CONFIDENCE:.0%}")
            return
        # Bootstrap: the model is still learning (backtest-trained, few real
        # trades). Its veto is advisory until ML_MIN_TRADES real outcomes
        # exist — otherwise no trades would ever close to teach it.
        event(f"ML-ADVISORY {symbol} {direction} — ML conf {ml_conf:.0%} "
              f"(learning {_ml_stats.get('total', 0)}/{config.ML_MIN_TRADES} "
              f"real trades) — allowed, AI decision stands")

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
            # AI adaptive targets ("big fish / small fish"): the market-
            # adaptation review scales TP to the regime (chop -> small,
            # clean trend -> big). SL distance stays untouched.
            _tp_mult = float(decision['tp_mult']) * \
                float(getattr(config, 'TP_SCALE', 1.0))
            tp = round(price + (atr * _tp_mult if direction == 'BUY' else -atr * _tp_mult), digits)
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
            if review is not None:
                block_reason['v'] = f"risk_gate:{reason}"
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
            if decision.get('engine') == 'FLASH' and not flash_engine.has_open_pair(state):
                flash_engine.register_main(state, {
                    'ticket': pos_ticket, 'dir': 1 if direction == 'BUY' else -1,
                    'entry': price, 'sl': sl, 'tp': tp, 'init_sl': sl,
                    'lot': lot, 'atr': atr,
                    'slm': decision['sl_mult'], 'tpm': decision['tp_mult'],
                    'opened_ts': time.time()})
            state['last_trade_bar'] = cur_bar
            trade_logger.log_trade(symbol, signal, price, tp, sl, spread_pct, ticket=pos_ticket)
            if review is not None:
                # AI approved AND the deterministic risk gate passed AND the
                # order filled — link the decision to the real position ticket.
                try:
                    ai_reviewer.journal.mark_execution(
                        review.decision_id, ticket=pos_ticket,
                        risk_result="passed", execution="executed")
                except Exception as e:
                    logger.error(f"AI journal mark_execution failed: {type(e).__name__}")
            _info = mt5.symbol_info(symbol)
            _digits = _info.digits if _info else 5
            event(f"OPEN {symbol} {direction} #{pos_ticket} @ {price:.{_digits}f} "
                  f"| TF {chosen_tf} | lot {lot} | SL {sl:.{_digits}f} "
                  f"TP {tp:.{_digits}f} | ML {ml_conf:.0%}")
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
    logger.info(f"  BOSS SMC BOT  v1.9.4  —  High-Freq Loop")
    logger.info("=" * 57)

    if not connect():
        logger.error("MT5 connection failed.")
        sys.exit(1) # Wrapper will restart

    run_initial_training()
    # Re-adopt any open positions left by a previous (crashed/restarted) run,
    # so they stay managed (break-even/trailing) and their outcome gets logged.
    adopt_open_positions()
    send(msg_bot_start(config.SYMBOLS))

    last_heartbeat = 0.0
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
            # Heartbeat: prove the loop is alive even when nothing happens
            # (logging is change-only, so silence is normal without this).
            if time.time() - last_heartbeat >= 900:  # every 15 minutes
                last_heartbeat = time.time()
                try:
                    sym = config.SYMBOLS[0]
                    tick_info = get_symbol_info(sym)
                    price = f"{tick_info.bid:.2f}" if tick_info else "n/a"
                    n_pos = sum(len(symbol_state[s]['positions'])
                                for s in config.SYMBOLS)
                    logger.info(
                        f"♥ ALIVE | {sym} {price} | equity ${acc.equity:.2f} | "
                        f"floating ${acc.profit:+.2f} | open positions: {n_pos} "
                        f"| AI gate ON | scanning 24/7")
                except Exception:
                    logger.info("♥ ALIVE | scanning 24/7")

            # News: poll the feed; fresh major headlines → log + Telegram
            if news_watcher is not None:
                try:
                    for title in news_watcher.refresh_if_due():
                        event(f"NEWS — {title}")
                        send(msg_news(title))
                except Exception as e:
                    logger.error(f"News refresh error ({type(e).__name__})")

            # AI market adaptation: periodic bounded self-tuning
            if ai_regime is not None and ai_regime.due():
                try:
                    run_ai_regime_review()
                except Exception as e:
                    logger.error(f"AI regime error ({type(e).__name__})")
                    ai_regime.mark_run()  # never hammer on repeated failures

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
