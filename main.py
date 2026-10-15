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
from core.ai_bus         import AIBus
from core.ai_brief       import (MarketBriefer, FlipTracker, bot_view, consensus,
                                 chart_view, AGREE, CONFLICT, NEUTRAL, NO_BRIEF)
from core                import scalp_guard
from core                import ai_context
from core.news           import NewsWatcher
from core.ai_journal     import AIJournal
from core.state_logger   import event, signal_log
from ml.model            import SMCModel, TradeLogger
from tg_alerts.alerts    import (send, msg_signal, msg_closed,
                                  msg_bot_start, msg_error, msg_ml_retrain,
                                  msg_blacklist, msg_milestone,
                                  msg_ai_decision, msg_ai_position,
                                  msg_news, msg_regime,
                                  msg_consensus, msg_scalp_close)

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

# Defence in depth for the secrets: whatever ends up in a log message — an
# exception that carries the Telegram URL, an Authorization header, a key echoed
# by a provider error — is masked before it reaches the file or the console.
from core.secret_guard import install_log_redaction
install_log_redaction(config)

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
        # ── v2.1 AI lanes (never block the loop, always re-checked on apply) ──
        'pending_ai':   {},   # snapshot_key -> candidate waiting for a verdict
        'position_ai':  {},   # ticket -> what the AI was shown, for the drift check
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

# ═════════════════════════════════════════════════════════════
#   AI roles — three independent reviewers, one transport lane
#   (v2.1: each role is gated by config.ai_*_on(), i.e. its own flag PLUS
#   working plumbing. In v2.0 the position manager required the ENTRY flag,
#   so AI_POSITION_ENABLED=True was silently dead.)
# ═════════════════════════════════════════════════════════════
ai_bus                = None
# How often the AI could not answer an entry and the bot traded its own signal
# (only non-zero under AI_ENTRY_TIMEOUT_MODE=proceed — the scalper policy).
ai_entry_stats        = {'proceeded_timeout': 0, 'proceeded_ai_error': 0,
                         'proceeded_queue_full': 0}
ai_journal            = None
ai_reviewer           = None
ai_position_reviewer  = None
ai_regime             = None
ai_briefer            = None        # v2.3 Analyst: the AI's own chart read
flip_tracker          = FlipTracker()
scalp_stats           = {'guard_closes': 0, 'flip_closes': 0}

# News watcher — free RSS feed; major headlines go to Telegram, and every
# recent headline is handed to the AI snapshots as untrusted DATA (never as
# instructions). The AI cannot query the internet; this is its only news feed.
news_watcher = None
if getattr(config, 'NEWS_ENABLED', False):
    news_watcher = NewsWatcher()
    logger.info(f"News feed ON ({len(news_watcher.urls)} feed(s), first "
                f"{news_watcher.url} every "
                f"{config.NEWS_POLL_SECONDS:.0f}s, keyword alerts)")

if config.ai_entry_on():
    ai_journal  = AIJournal(config.AI_JOURNAL_PATH)
    ai_reviewer = AIReviewer(ai_journal)
    logger.info(f"AI entry gate ON (provider={config.AI_PROVIDER}, "
                f"model={config.AI_MODEL}, min_conf={config.AI_MIN_CONFIDENCE}, "
                + ("on silence: trade the bot's own signal after "
                   f"{config.ai_entry_budget_seconds():.1f}s, a real REJECT is final)"
                   if getattr(config, 'AI_ENTRY_TIMEOUT_MODE', 'block') == 'proceed'
                   else "fail-closed)"))
if config.ai_position_on():
    ai_journal = ai_journal or AIJournal(config.AI_JOURNAL_PATH)
    ai_position_reviewer = PositionReviewer(ai_journal)
    logger.info(f"AI position manager ON (every {config.AI_POSITION_INTERVAL:.0f}s "
                f"per open trade | EXTEND at >= {config.AI_EXTEND_MIN_PROGRESS:.0%} "
                f"to TP | {config.AI_POSITION_VOTES} votes | min_conf "
                f"{config.AI_POSITION_MIN_CONFIDENCE} | fail-safe HOLD)")
if config.ai_regime_on():
    ai_journal = ai_journal or AIJournal(config.AI_JOURNAL_PATH)
    ai_regime = RegimeReviewer(ai_journal)
    logger.info(f"AI directives ON (every {config.AI_REGIME_INTERVAL/3600:.1f}h, "
                f"expire after {config.AI_REGIME_TTL_SECONDS/60:.0f}min | "
                f"adjustable: {', '.join(config.AI_REGIME_ALLOWED)})")
elif getattr(config, 'AI_REGIME_ENABLED', False):
    logger.warning("AI_REGIME_ENABLED=true but the plumbing is missing "
                   "(AI_PROVIDER/AI_API_KEYS) — directives OFF")
if config.ai_brief_on():
    ai_briefer = MarketBriefer()
    logger.info(f"AI analyst ON (brief per symbol every >= "
                f"{config.AI_BRIEF_INTERVAL_SECONDS:.0f}s, valid "
                f"{config.AI_BRIEF_TTL_SECONDS:.0f}s | AGREE>={config.AI_BRIEF_AGREE_CONF} "
                f"CONFLICT>={config.AI_BRIEF_VETO_CONF} | fast lane "
                f"{'ON' if config.AI_BRIEF_FASTLANE else 'off'} | thesis-flip close "
                f"{'ON' if config.AI_BRIEF_FLIP_CLOSE else 'off'})")
elif getattr(config, 'AI_BRIEF_ENABLED', False):
    logger.warning("AI_BRIEF_ENABLED=true but the plumbing is missing "
                   "(AI_PROVIDER/AI_API_KEYS) — analyst OFF")

# The whole point of the bus: an 8s HTTP timeout in the provider must not
# freeze break-even/trailing/reconciliation in a 0.5-1.0s loop.
if (ai_reviewer is not None or ai_position_reviewer is not None
        or ai_regime is not None or ai_briefer is not None):
    if getattr(config, 'AI_ASYNC', True):
        ai_bus = AIBus(workers=getattr(config, 'AI_WORKER_THREADS', 1),
                       queue_max=getattr(config, 'AI_QUEUE_MAX', 12),
                       result_ttl=getattr(config, 'AI_RESULT_TTL_SECONDS', 120.0))
        ai_bus.start()
    else:
        logger.warning("AI_ASYNC=false — AI calls run inline and WILL stall the "
                       "trading loop up to AI_TIMEOUT_SECONDS each")

for _line in config.ai_status_lines():
    logger.info(f"  AI/BOT FLOW | {_line}")


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
        state = state_for(symbol)
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
            state = state_for(symbol)
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

def state_for(symbol):
    """Symbol state, created on demand. v2.0 indexed symbol_state[symbol]
    directly, so any symbol not in config.SYMBOLS at import time raised
    KeyError inside the trading path (and in the offline test suite)."""
    st = symbol_state.get(symbol)
    if st is None:
        st = symbol_state.setdefault(symbol, fresh_state())
    return st


def _live_progress(symbol, p, direction):
    """Signed progress toward TP — the SAME definition the AI is briefed on,
    used both when building the snapshot and when re-checking it on arrival."""
    tp_dist = abs(p.tp - p.price_open)
    if tp_dist <= 0:
        return None
    moved = (p.price_current - p.price_open) if direction == 1 \
        else (p.price_open - p.price_current)
    return moved / tp_dist


def submit_position_reviews(symbol, state):
    """AI position manager, stage 1: snapshot + queue a review. NEVER blocks.

    The verdict is applied in apply_position_verdicts() on a later loop, after
    re-reading the position — so the AI answers about a trade that still exists
    and still looks like what it was shown."""
    import MetaTrader5 as mt5
    if ai_position_reviewer is None:
        return
    pending = state.setdefault('position_ai', {})
    # Orphan guard: a marker whose verdict never arrived (worker died, result
    # expired) must not freeze this ticket's reviews forever.
    stale_after = float(getattr(config, 'AI_RESULT_TTL_SECONDS', 120.0)) * 2
    now0 = time.time()
    for t0, mark in list(pending.items()):
        if now0 - mark.get('ts', 0) > stale_after:
            pending.pop(t0, None)
    for ps in list(state['positions']):
        ticket = ps['ticket']
        if ticket in pending:
            continue                       # a verdict is already on its way
        p = get_position_by_ticket(ticket)
        if p is None:
            continue
        if not p.tp:
            # A position with no TP is the one thing the AI must not "fix" by
            # closing on a whim — but it must never be invisible either.
            signal_log.log(f"{symbol}:notp:{ticket}",
                           f"● WARN {symbol} #{ticket} has no TP — AI position "
                           f"review skipped (set TP manually or let TP/SL rule run)")
            continue
        tp_dist = abs(p.tp - p.price_open)
        if tp_dist <= 0:
            continue
        direction = 1 if p.type == mt5.ORDER_TYPE_BUY else -1
        progress = _live_progress(symbol, p, direction)
        if progress is None:
            continue
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
            open_positions=sum(len(state_for(s)['positions'])
                               for s in config.SYMBOLS),
            max_positions=config.MAX_POSITIONS)
        if news_watcher is not None:
            ctx["recent_news"] = news_watcher.recent_titles(5)
        # The Guardian reads the same M1 chart as the Analyst (market only), so
        # "bank it" and "go bigger" are judged on the candles AND on the dollars
        # (floating_usd / scalp_mode below). atr/rsi above are H1; this is M1.
        _chart = chart_view(state.get('feature_cache'),
                            int(getattr(config, 'AI_POSITION_CHART_BARS', 12)))
        if _chart["bars_m1"]:
            ctx["market_chart"] = _chart
        if (getattr(config, 'SCALP_GUARD_ENABLED', False)
                or getattr(config, 'SCALP_USD_MODE', False)):
            # The Guardian is briefed in scalper terms: dollars, seconds, and
            # what the other two parties (Scalp Guard, Analyst) will do anyway.
            _opened = float(ps.get('opened_ts') or 0)
            _b = ai_briefer.fresh(symbol) if ai_briefer is not None else None
            ctx["scalp_mode"] = {
                "target_usd": ps.get('target_usd', config.SCALP_TARGET_USD),
                "stop_usd": ps.get('sl_usd'),
                "peak_usd": round(float(ps.get('peak_usd', 0.0)), 2),
                "seconds_open": int(time.time() - _opened) if _opened else None,
                "scalp_guard_will": {
                    "bank_at_usd": ps.get('target_usd', config.SCALP_TARGET_USD),
                    "stall_after_s": config.SCALP_STALL_SECONDS,
                    "giveback_pct": config.SCALP_GIVEBACK_PCT,
                    "early_cut_pct_of_stop": config.SCALP_EARLY_CUT_PCT,
                    "max_hold_s": config.SCALP_MAX_HOLD_SECONDS},
                "analyst_brief": _b.short() if _b is not None else None,
            }
        ai_position_reviewer.mark_reviewed(ticket)

        if ai_bus is None:                 # sync fallback (tests, AI_ASYNC=false)
            try:
                res = ai_position_reviewer.review(ctx, ticket=ticket)
            except Exception as e:
                logger.error(f"AI position review error ({type(e).__name__}): {e}")
                continue
            apply_position_action(symbol, ps, res, p, progress)
            continue

        pending[ticket] = {'ts': time.time(), 'progress': progress,
                           'entry': p.price_open, 'tp': p.tp, 'sl': p.sl}
        if not ai_bus.submit('position', str(ticket),
                             lambda c=ctx, t=ticket:
                             ai_position_reviewer.review(c, ticket=t)):
            pending.pop(ticket, None)      # in flight / queue full — try later


def apply_position_action(symbol, ps, res, p, progress_at_review):
    """AI position manager, stage 2: act on a verdict (main thread only).

    CLOSE and EXTEND are the only two actions the AI owns, and each has to
    survive a re-check: the trade may be gone, the price may have run away
    from the snapshot the model judged, and the target may already be next."""
    import MetaTrader5 as mt5
    ticket = ps['ticket']
    state = state_for(symbol)
    dir_str = "BUY" if ps.get('direction', 1) == 1 else "SELL"
    event(f"AI REVIEW {symbol} #{ticket} → {res.action} "
          f"(conf {res.confidence:.2f} | {res.model} | {res.key_number}) "
          f"— {res.reason_code}")
    if res.action == "HOLD" or not res.approved:
        return

    def note(execution, final_action, risk_result="not_reached"):
        """Tell the AI's memory what actually happened to its advice."""
        if ai_position_reviewer is not None:
            ai_position_reviewer.annotate(getattr(res, 'decision_id', 0),
                                          execution=execution,
                                          final_action=final_action,
                                          risk_result=risk_result)

    if p is None:
        p = get_position_by_ticket(ticket)
    if p is None:
        event(f"AI {res.action} skipped {symbol} #{ticket} — position already "
              f"gone (verdict was about a trade that no longer exists)")
        note("not_applicable", "ai_" + res.action.lower() + "_skipped_gone")
        return
    direction = 1 if p.type == mt5.ORDER_TYPE_BUY else -1
    progress_now = _live_progress(symbol, p, direction)
    drift_limit = float(getattr(config, 'AI_POSITION_MAX_DRIFT_PROGRESS', 0.15))
    if progress_now is None or abs(progress_now - progress_at_review) > drift_limit:
        event(f"AI {res.action} DISCARDED {symbol} #{ticket} — price moved "
              f"{(progress_now or 0) - progress_at_review:+.0%} of the way to TP "
              f"since the snapshot (>{drift_limit:.0%}); fail-safe HOLD")
        note("not_applicable", "ai_" + res.action.lower() + "_discarded_stale")
        return

    if res.action == "CLOSE":
        ok, detail = close_position(symbol, ticket, "BOSS AI CLOSE")
        if ok:
            note("closed_by_ai", "ai_close_applied", "ai_actioned")
            event(f"AI CLOSE {symbol} {dir_str} #{ticket} — "
                  f"{res.reason_code} | banked {p.profit:+.2f} USD")
            send(msg_ai_position(
                "CLOSE", symbol, dir_str, res.confidence,
                res.reason_code, res.reason, ticket,
                model=res.model, key=res.key_number,
                detail=f"💵 Banked ~<b>{p.profit:+.2f} USD</b>"))
        else:
            logger.error(f"AI CLOSE order failed for #{ticket}: {detail}")
            # v2.1 fix: this used to reach into ai_reviewer.journal, which is
            # None whenever the position manager runs without the entry gate.
            note("close_failed", "ai_close_failed", "failed")
        return

    if res.action == "EXTEND" and ps.get('ai_extended'):
        note("not_applicable", "ai_extend_already_done")
        event(f"AI EXTEND refused {symbol} #{ticket} — this position was already "
              f"extended once (one extension per position, by design)")
    elif res.action == "EXTEND":
        info = mt5.symbol_info(symbol)
        digits = info.digits if info else 5
        tp_dist = abs(p.tp - p.price_open)
        mult = float(getattr(config, 'AI_EXTEND_TP_MULT', 1.15))
        lock = float(getattr(config, 'AI_EXTEND_LOCK_PCT', 0.80))
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
            # v2.1: write the new geometry BACK into the bot's own state.
            # Without this the trade manager kept computing progress against
            # the OLD tp and its "tighten TP at 80%" rule silently pulled the
            # target back — the AI extension was undone one loop later.
            ps['ai_extended'] = True
            ps['tp'] = new_tp
            ps['sl'] = new_sl
            note("tp_extended", "ai_extend_applied", "ai_actioned")
            ai_context.invalidate()
            event(f"AI EXTEND {symbol} {dir_str} #{ticket} — "
                  f"TP {p.tp:.2f}→{new_tp:.2f}, SL locked {new_sl:.2f}")
            send(msg_ai_position(
                "EXTEND", symbol, dir_str, res.confidence,
                res.reason_code, res.reason, ticket,
                model=res.model, key=res.key_number,
                detail=f"✅ TP → <code>{new_tp:.2f}</code>   ·   "
                       f"🔒 SL locked → <code>{new_sl:.2f}</code>"))
        else:
            note("extend_rejected", "ai_extend_rejected_bounds", "failed")
            logger.warning(f"AI EXTEND not applied for #{ticket} "
                           f"(tp_ok={tp_ok} sl_ok={sl_ok})")


def apply_position_verdicts():
    """Stage 2 for the async lane: match finished verdicts to their tickets."""
    if ai_bus is None:
        return
    for full_key, ok, payload in ai_bus.collect('position'):
        _, _, rest = full_key.partition(':')
        try:
            ticket = int(rest)
        except (TypeError, ValueError):
            continue
        owner = None
        for sym in list(symbol_state):
            st = symbol_state[sym]
            marked = st.get('position_ai', {}).pop(ticket, None)
            if marked is not None:
                ps = next((x for x in st['positions'] if x['ticket'] == ticket), None)
                owner = (sym, ps, marked)
                break
        if owner is None:
            # Marker was swept (its position closed and the close path cleared
            # the ticket) or the verdict outlived its TTL. Still confirm the
            # journal row when we know which ticket it was about.
            if ai_position_reviewer is not None:
                try:
                    ai_position_reviewer.annotate_ticket(
                        ticket, execution="not_applicable",
                        final_action="ai_review_void_no_state",
                        risk_result="not_reached")
                except Exception:
                    pass
            continue
        sym, ps, marked = owner
        if ps is None:
            # Position closed while the AI was thinking. The advice is void —
            # say so in the journal, otherwise the AI keeps a proposal that
            # looks like it is still owed an outcome (v2.1 honesty rule).
            if ai_position_reviewer is not None:
                try:
                    ai_position_reviewer.annotate(
                        getattr(payload, 'decision_id', 0),
                        execution="not_applicable",
                        final_action="ai_review_void_position_gone",
                        risk_result="not_reached")
                except Exception:
                    pass
            continue
        if not ok:
            event(f"AI position review error for {sym} #{ticket} "
                  f"({type(payload).__name__}) — fail-safe HOLD")
            continue
        p = get_position_by_ticket(ticket)
        progress_now = None
        if p is not None:
            direction = 1 if p.type == 0 else -1
            progress_now = _live_progress(sym, p, direction)
        if progress_now is None:
            progress_now = marked.get('progress', 0.0)
        apply_position_action(sym, ps, payload, p, progress_now)



# NOTE (owner rule: "adapt to the market, not to money"): the AI roles are no
# longer briefed with the bot's results or its own track record. The old
# results-briefing helper was removed on purpose; trade profit/outcome is
# still journaled to ai_decisions.csv / trade_log.csv for the owner to review.


# ═════════════════════════════════════════════════════════════
#   v2.3 TWIN-BRAIN SCALPER
#     Analyst  : the AI reads the chart on its own, ahead of time (a "brief")
#     Bot      : its own signal + its own read (bot_view), shared with the AI
#     Guardian : AI position reviewer + the local Scalp Guard watch every trade
# ═════════════════════════════════════════════════════════════
def _h1_trend_cached(symbol, state):
    """H1 trend, refreshed at most once a minute (it moves on the hour)."""
    c = state.get('h1_trend_cache')
    now = time.time()
    if c and now - c[0] < 60:
        return c[1]
    try:
        val = h1_trend_direction(get_candles(symbol, "H1", count=300))
    except Exception:
        val = None
    state['h1_trend_cache'] = (now, val)
    return val


def submit_brief_refreshes(symbol, state):
    """Analyst, stage 1: queue a fresh chart read when one is due. Never blocks.
    The bot's own read (bot_view) travels with it — that is the 'bot shares its
    analysis with the AI' half of the loop."""
    if ai_briefer is None:
        return
    cur_bar = state.get('last_signal_bar')
    if not ai_briefer.due(symbol, cur_bar, has_positions=bool(state['positions'])):
        return
    df = state.get('feature_cache')
    if df is None or len(df) < 30 or 'atr' not in df.columns:
        return
    try:
        h1 = _h1_trend_cached(symbol, state)
        try:
            spread = get_spread_percent(symbol)
        except Exception:
            spread = None
        titles = news_watcher.recent_titles(5) if news_watcher is not None else []
        ctx = ai_briefer.build_context(
            symbol, df, h1_trend=h1, spread_pct=spread, news_titles=titles,
            bot=bot_view(df, h1))
        meta = {'symbol': symbol, 'price': float(df['close'].iloc[-1]),
                'atr': float(df['atr'].iloc[-1]), 'bar': cur_bar}
    except Exception as e:
        logger.error(f"AI brief context error ({type(e).__name__})")
        ai_briefer.mark_submitted(symbol, cur_bar)   # no retry storm
        return
    meta['seq'] = ai_briefer.mark_submitted(symbol, cur_bar)
    if ai_bus is None:                       # sync fallback (tests, AI_ASYNC=false)
        try:
            ai_briefer.accept(ai_briefer.analyze(ctx, meta))
        except Exception as e:
            ai_briefer.stats['error'] += 1
            logger.error(f"AI brief error ({type(e).__name__})")
        return
    # a full queue / an answer still in flight just means: try again after the
    # interval. A missing brief never blocks anything.
    ai_bus.submit('brief', symbol,
                  lambda c=ctx, m=meta: ai_briefer.analyze(c, m))


def collect_briefs():
    """Analyst, stage 2: store finished briefs (main thread only)."""
    if ai_bus is None or ai_briefer is None:
        return
    for full_key, ok, payload in ai_bus.collect('brief'):
        sym = full_key.partition(':')[2]
        if not ok:
            ai_briefer.stats['error'] += 1
            signal_log.log(f"{sym}:brieferr",
                           f"● AI brief {sym} unavailable "
                           f"({type(payload).__name__}) — the bot runs on its own read")
            continue
        if ai_briefer.accept(payload):
            b = payload
            signal_log.log(
                f"{sym}:brief",
                f"● AI BRIEF {sym} {b.bias} | {b.state}"
                + (" | STAND-ASIDE" if b.stand_aside else "")
                + f" — {b.reason_code}")


def _consult_brief(symbol, decision, cur_bar, state):
    """Compare the bot's fresh signal with the Analyst's brief.

    Returns 'block' (the AI says no), 'fastlane' (AI and bot agree: trade now,
    the caller places it) or None (the normal entry gate decides). It does not
    place anything itself."""
    direction = decision['direction']
    signal = decision['signal']
    brief = ai_briefer.latest(symbol)
    try:
        _i = get_symbol_info(symbol)
        px = float(_i.bid) if _i else None
    except Exception:
        px = None
    verdict, why = consensus(direction, brief, price=px,
                             atr=float(signal.get('atr') or 0) or None)
    ai_briefer.stats[verdict.lower()] += 1
    decision['ai_consensus'] = verdict
    if brief is not None:
        decision['ai_brief'] = brief.short()

    if verdict == CONFLICT:
        event(f"AI≠BOT {symbol} {direction} — {why} ({brief.state}); the bot "
              f"obeys the AI, entry blocked")
        state['ai_reject_cooldown_until'] = time.time() + float(
            getattr(config, 'AI_REJECT_COOLDOWN_SECONDS', 20))
        if time.time() - state.get('last_conflict_msg', 0) > 120:
            state['last_conflict_msg'] = time.time()
            send(msg_consensus("CONFLICT", symbol, direction, brief.bias,
                               brief.confidence, brief.state, brief.reason))
        return 'block'

    if verdict == AGREE and getattr(config, 'AI_BRIEF_FASTLANE', False):
        ai_briefer.stats['fastlane'] += 1
        # log only (no Telegram): a scalper opens many trades and the open /
        # close messages already tell the story; a chat group allows ~20
        # messages a minute
        event(f"AI+BOT AGREE {symbol} {direction} — {why} ({brief.state}); "
              f"fast lane, no AI wait")
        return 'fastlane'
    return None


def _scalp_size(symbol, price, sl, tp, tick):
    """$-sized scalp: the lot that makes the TP distance worth SCALP_TARGET_USD.
    Returns (meta, skip_reason). meta None + skip None = broker data missing,
    use the percent-risk lot instead."""
    target = float(config.SCALP_TARGET_USD)
    tp_dist, sl_dist = abs(tp - price), abs(price - sl)
    sz = risk_manager.calc_lot_for_usd(symbol, tp_dist, target,
                                       max_lot=config.SCALP_MAX_LOT)
    if sz is None:
        return None, None
    lot = sz['lot']
    per_unit = sz['per_lot'] / tp_dist
    sl_usd = lot * per_unit * sl_dist
    tp_usd = sz['actual_usd']
    cap_mult = float(config.SCALP_MAX_LOSS_MULT)
    if cap_mult > 0 and sl_usd > cap_mult * target:
        return None, (f"lot {lot:g} is the smallest this broker allows and the "
                      f"stop would be worth ${sl_usd:.2f} against a ${target:g} "
                      f"target (cap {cap_mult:g}x) — too big for a ${target:g} scalp")
    spread_usd = lot * per_unit * abs(float(tick.ask) - float(tick.bid))
    eff_target = min(target, tp_usd)
    spread_cap = float(config.SCALP_MAX_SPREAD_PCT_OF_TARGET)
    if spread_cap > 0 and eff_target > 0 and spread_usd / eff_target * 100 > spread_cap:
        return None, (f"spread costs ${spread_usd:.2f} = "
                      f"{spread_usd / eff_target * 100:.0f}% of the "
                      f"${eff_target:.2f} target (cap {spread_cap:.0f}%) — "
                      f"the market charges too much for this scalp")
    return {'lot': lot, 'target_usd': eff_target, 'sl_usd': sl_usd,
            'tp_usd': tp_usd, 'spread_usd': spread_usd}, None


def _scalp_close(symbol, ps, p, code, detail, kind):
    """Close one trade at market for the Scalp Guard / the Analyst. Main thread
    only. Retries are spaced out so a refusing broker is not hammered."""
    ticket = ps['ticket']
    if time.time() - ps.get('close_try_ts', 0) < 2.0:
        return False
    ps['close_try_ts'] = time.time()
    dir_str = "BUY" if ps.get('direction', 1) == 1 else "SELL"
    pnl = float(getattr(p, 'profit', 0.0) or 0.0)
    ok, why = close_position(symbol, ticket, f"BOSS {code}"[:31])
    if not ok:
        signal_log.log(f"{symbol}:{ticket}:scalpclosefail",
                       f"● WARN {symbol} #{ticket} {code} close refused ({why}) — "
                       f"the stop and TP on file still protect the trade")
        return False
    scalp_stats['guard_closes' if kind == 'guard' else 'flip_closes'] += 1
    if kind != 'guard' and ai_briefer is not None:
        ai_briefer.stats['flip_close'] += 1
    event(f"{code} {symbol} {dir_str} #{ticket} — {detail} | closed ~{pnl:+.2f} USD")
    send(msg_scalp_close(kind, symbol, dir_str, ticket, code, detail, pnl))
    flip_tracker.forget(ticket)
    if ai_bus is not None:
        ai_bus.drop('position', str(ticket))
    return True


def scalp_watch(symbol, state):
    """Every loop, every open trade, no network:
       1. Scalp Guard  - bank the $ target, cut a trade that is not working
       2. Analyst flip - the AI now says the opposite of this trade, twice
    Both only CLOSE; neither moves or removes a stop."""
    guard_on = bool(getattr(config, 'SCALP_GUARD_ENABLED', False)) \
        and config.ENTRY_ENGINE != 'FLASH'
    flip_on = ai_briefer is not None and bool(getattr(config, 'AI_BRIEF_FLIP_CLOSE', False))
    if not (guard_on or flip_on) or not state['positions']:
        return
    import MetaTrader5 as mt5
    brief = ai_briefer.fresh(symbol) if flip_on else None
    for ps in list(state['positions']):
        p = get_position_by_ticket(ps['ticket'])
        if p is None:
            continue
        direction = 1 if p.type == mt5.ORDER_TYPE_BUY else -1
        if guard_on:
            d = scalp_guard.evaluate(ps, float(p.profit))
            if d.action == 'CLOSE':
                _scalp_close(symbol, ps, p, d.code, d.detail, 'guard')
                continue
        else:
            ps.setdefault('opened_ts', time.time())
        if brief is not None and flip_tracker.observe(
                ps['ticket'], direction, brief, ps.get('opened_ts')):
            _scalp_close(
                symbol, ps, p, 'AI_THESIS_FLIP',
                f"Analyst now reads {brief.bias} ({brief.confidence:.0%}, "
                f"{brief.state}) against this trade: {brief.reason_code}",
                'analyst')


def ai_flow_summary():
    """One line of truth about the AI layer — for the heartbeat and the log."""
    parts = ["AI-entry:" + ("ON" if ai_reviewer is not None else "off"),
             "AI-positions:" + ("ON" if ai_position_reviewer is not None else "off"),
             "AI-directives:" + ("ON" if ai_regime is not None else "off")]
    parts.append("queue=%d" % ai_bus.inflight() if ai_bus is not None
                 else "mode=sync")
    pend = sum(len(state_for(s).get('pending_ai') or {}) for s in config.SYMBOLS)
    if pend:
        parts.append("waiting_for_verdict=%d" % pend)
    if ai_reviewer is not None and config.ai_entry_proceeds_on_timeout():
        n_silent = sum(ai_entry_stats.values())
        parts.append("entry-policy=proceed(%.1fs)" %
                     config.ai_entry_budget_seconds())
        if n_silent:
            parts.append("bot-traded-without-AI=%d" % n_silent)
    if ai_briefer is not None:
        s = ai_briefer.stats
        parts.append("analyst:ON briefs=%d err=%d agree=%d(fast=%d) conflict=%d "
                     "neutral=%d none=%d" % (s['ok'], s['error'], s['agree'],
                                             s['fastlane'], s['conflict'],
                                             s['neutral'], s['no_brief']))
    if getattr(config, 'SCALP_GUARD_ENABLED', False) or ai_briefer is not None:
        parts.append("guard-closes=%d flip-closes=%d" % (
            scalp_stats['guard_closes'], scalp_stats['flip_closes']))
    if ai_regime is not None and ai_regime.applied:
        parts.append("directives=" + ",".join(
            "%s=%s" % (k, v) for k, v in list(ai_regime.applied.items())[:4]))
    return " | ".join(parts)


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
                stats = {"atr_pct": round(atr / close * 100, 3),
                         "last_close": round(close, 5)}
                try:   # market data only; simply omitted if the broker will not say
                    stats["spread_pct"] = round(float(get_spread_percent(sym)), 4)
                except Exception:
                    pass
                symbols_stats[sym] = stats
        except Exception:
            continue
    news = news_watcher.recent_titles(5) if news_watcher else []
    ai_regime.mark_run()          # cadence starts now, not when the answer lands
    if ai_bus is None:             # sync fallback (tests / AI_ASYNC=false)
        report_regime(ai_regime.run(symbols_stats, news))
        return
    if not ai_bus.submit('regime', 'review',
                         lambda: ai_regime.evaluate(symbols_stats, news)):
        event("AI directive review skipped — queue busy; keeping current settings")


def report_regime(res):
    """Log + alert the outcome of one adaptation cycle (main thread)."""
    regime, summary, applied = res
    event(f"AI REGIME → {regime} | {summary}")
    if applied:
        for p, old, new, why in applied:
            event(f"AI ADJUST {p}: {old} → {new} ({why})")
        ai_context.invalidate()
        send(msg_regime(regime, summary, applied))


def apply_regime_verdict():
    """Stage 2 of the directive lane: the worker only ANSWERED; the config is
    mutated here, on the main thread, through the same clamps."""
    if ai_bus is None:
        return
    for _key, ok, payload in ai_bus.collect('regime'):
        if not ok or payload is None:
            event(f"AI directive review failed ({type(payload).__name__}) — "
                  f"settings untouched")
            continue
        ctx, decision, model, provider, key_no = payload
        if decision is None:
            event(f"AI directive review skipped: {ai_regime.last_error() or 'no verdict'} "
                  f"— settings untouched")
            continue
        report_regime(ai_regime.apply(decision, ctx, model, provider, key_no))


def process_symbol_hf(symbol):
    """High-frequency processing for a single symbol"""
    global trades_since_retrain, last_retrain_time
    state = state_for(symbol)
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
        # ── THE RETURN PATH: bot → AI ──
        # Link the reconciled closed-trade result back to the AI decision(s)
        # that touched this ticket, from the bot's OWN deal only (never a
        # guess, never an open trade). v2.1 fix: this went through
        # ai_reviewer.journal, so with only the position manager enabled the
        # AI never learned how its own calls turned out — the loop was open.
        jrn = ai_journal or getattr(ai_reviewer, 'journal', None)
        if jrn is not None:
            try:
                jrn.link_outcome(symbol, ticket, profit)
            except Exception as e:
                logger.error(f"AI journal link_outcome failed: {type(e).__name__}")
        # the AI's memory must not show yesterday's numbers for 45s
        ai_context.invalidate()
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
        # Forget the AI bookkeeping for a dead ticket: its cadence marker and
        # any verdict still in flight (applying a late CLOSE to a recycled
        # ticket number would be worse than not applying it).
        state.get('position_ai', {}).pop(ticket, None)
        flip_tracker.forget(ticket)
        if ai_position_reviewer is not None:
            try:
                ai_position_reviewer.forget(ticket)
            except Exception:
                pass
        if ai_bus is not None:
            ai_bus.drop('position', str(ticket))
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

    # 3b. v2.3 Analyst: keep a fresh AI chart read per symbol (never blocks)
    if ai_briefer is not None:
        try:
            submit_brief_refreshes(symbol, state)
        except Exception as e:
            logger.error(f"AI brief submit error ({type(e).__name__})")

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

    # 4b. AI position manager — queue a HOLD / CLOSE / EXTEND review for each
    # open trade that is due. Submitting is cheap and NEVER blocks; the verdict
    # is applied by apply_position_verdicts() once it lands.
    if ai_position_reviewer is not None and state['positions']:
        try:
            submit_position_reviews(symbol, state)
        except Exception as e:
            logger.error(f"AI position review error ({type(e).__name__}): {e}")

    # 4b'. v2.3 Scalp Guard + Analyst thesis-flip: local, no network, every loop
    try:
        scalp_watch(symbol, state)
    except Exception as e:
        logger.error(f"scalp watch error ({type(e).__name__}): {e}")

    # 4c. AI entry candidates waiting for a verdict: drop the ones that will
    # never get an answer (provider dead / queue dropped). Fail-closed.
    try:
        sweep_pending_entries(symbol, state)
    except Exception as e:
        logger.error(f"AI pending-entry sweep error ({type(e).__name__})")

    # 5. New Entry Scan — at most ONCE per new bar of the engine's TF
    #    (whether or not a trade is placed), so blocked entries don't cause a
    #    per-second scan/log storm.
    #
    #    v2.1: the session window, the loss circuit breaker and the AI-reject
    #    cooldown are now checked for EVERY engine. In v2.0 they lived inside
    #    the SMC_MTF branch only — so switching the engine silently switched
    #    off the breaker that exists because of the FOMC whipsaw.
    scan_tf = "H1" if engine == "SR_H1" else "M1"
    cur_bar = _get_cur_bar(symbol, scan_tf)
    if cur_bar != state['last_scan_bar'] and len(state['positions']) < config.MAX_POSITIONS:
        blocked_by = entry_gate_block_reason(symbol, state)
        if blocked_by:
            signal_log.log(f"{symbol}:gate",
                           f"● SKIP {symbol} entry scan — {blocked_by}")
            return
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
            # Original MTF high-freq flow (no H1 gate) — one pair (main +
            # optional hedge) at a time. Session/breaker already checked above.
            if not flash_engine.has_open_pair(state):
                decision = flash_engine.decide(symbol, get_candles, state)
                if decision:
                    event(f"FLASH SIGNAL {symbol} {decision['direction']} "
                          f"TF {decision.get('chosen_tf', '?')} {decision['signal'].get('rsi', 0):.0f}")
                    execute_trade(symbol, decision, cur_bar)
        else:
            # SMC_MTF (fast scalping). Session / circuit breaker / AI cooldown
            # were already checked by the shared gate above.
            if True:
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

def scan_tf_for(decision):
    """The candle the entry scan is gated on — and therefore the bar a pending
    AI verdict must be compared against. SR_H1 decides on H1, the fast engines
    on M1; comparing an H1 bar against an M1 bar would call every verdict
    stale and silently stop the bot from ever trading on an approval."""
    return "H1" if (decision or {}).get('engine') == "SR_H1" else "M1"


def entry_gate_block_reason(symbol, state):
    """Deterministic 'may we look for a new entry at all?' — one answer for
    every engine. Returns a short reason string, or None when open."""
    if not _in_session():
        return "outside trading session"
    if time.time() < entry_pause_until:
        return (f"loss circuit breaker active — "
                f"{(entry_pause_until - time.time()) / 60.0:.0f} min left")
    cd = float(state.get('ai_reject_cooldown_until', 0) or 0)
    if time.time() < cd:
        return f"AI reject cooldown ({(cd - time.time()):.0f}s left)"
    return None


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

    v2.1 — the gate no longer blocks the loop:
      • a verdict already cached for this exact snapshot is applied inline;
      • otherwise the review is queued on the AI bus and this function returns
        immediately. The candidate lives in state['pending_ai'] until
        collect_entry_verdicts() applies it — and only if it is still fresh
        (same bar, price inside AI_ENTRY_MAX_DRIFT_ATR, younger than
        AI_ENTRY_MAX_AGE_SECONDS). No verdict = no trade, still fail-closed.
      • with AI_ASYNC=false (or no bus, e.g. in tests) the review runs inline
        exactly as before, so the documented synchronous contract holds too.
    """
    state = state_for(symbol)
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

    # ── v2.3: the Analyst's own read vs the bot's signal ─────
    # AGREE -> fast lane (no AI wait); CONFLICT -> the bot obeys the AI;
    # NEUTRAL / no brief -> fall through to the per-signal gate below.
    if ai_briefer is not None:
        try:
            act = _consult_brief(symbol, decision, cur_bar, state)
        except Exception as e:
            # the Analyst failing must never cost the entry: degrade to v2.2
            logger.error(f"AI brief consult error ({type(e).__name__})")
            act = None
        if act == 'block':
            return
        if act == 'fastlane':
            # outside the try: an error in the order path must never be
            # mistaken for an Analyst error and re-run through the gate
            handle_entry_verdict(symbol, decision, cur_bar, None, state)
            return

    # ── the AI gate ──────────────────────────────────────────
    review = None
    if ai_reviewer is not None and getattr(ai_reviewer, 'enabled', False):
        try:
            ctx = build_ai_snapshot(symbol, decision, state)
        except Exception as e:
            # Any unexpected AI-layer error fails CLOSED: block the candidate,
            # never approve, never crash the loop. Log type only (no raw body).
            # Scalper policy (AI_ENTRY_TIMEOUT_MODE=proceed): the AI being
            # broken must not stop a fast bot — trade the bot's own signal.
            if config.ai_entry_proceeds_on_timeout():
                ai_entry_stats['proceeded_ai_error'] += 1
                event(f"AI SILENT {symbol} {direction} — snapshot failed "
                      f"({type(e).__name__}); trading the bot's own signal")
                handle_entry_verdict(symbol, decision, cur_bar, None, state)
                return
            logger.error(f"AI review error ({type(e).__name__}); "
                         f"entry blocked (fail-closed)")
            send(msg_error(f"AI review failed ({type(e).__name__}) — "
                           f"entry blocked (fail-closed)"))
            return

        cached = ai_reviewer.peek_cached(ctx)
        if cached is not None:
            # the exact same snapshot was already judged — reuse, no second call
            handle_entry_verdict(symbol, decision, cur_bar, cached, state)
            return

        if ai_bus is not None:
            # ASYNC LANE: queue the review, keep the candidate, return at once.
            # The loop keeps managing open trades; the order is placed (or not)
            # when the verdict lands, after a freshness re-check. A candidate
            # that never gets a verdict is never traded (fail-closed).
            skey = ai_reviewer.snapshot_key(ctx)
            if skey in state['pending_ai']:
                return
            if (config.ai_entry_proceeds_on_timeout()
                    and ai_bus.waiting('entry', f"{symbol}|{skey}")):
                return        # the AI is still thinking about this very snapshot
            try:
                _ti = get_symbol_info(symbol)
                _px = float(_ti.bid) if _ti else 0.0
            except Exception:
                _px = 0.0
            state['pending_ai'][skey] = {
                'decision': decision, 'cur_bar': cur_bar, 'ts': time.time(),
                'price': _px, 'atr': float(signal.get('atr') or 0) or None,
                'scan_tf': scan_tf_for(decision), 'symbol': symbol}
            if not ai_bus.submit('entry', f"{symbol}|{skey}",
                                 lambda c=ctx: ai_reviewer.review(c)):
                state['pending_ai'].pop(skey, None)
                if config.ai_entry_proceeds_on_timeout():
                    # a saturated AI lane is "the AI could not answer" — it
                    # must not cost a fast bot the entry
                    ai_entry_stats['proceeded_queue_full'] += 1
                    event(f"AI queue full {symbol} {direction} — trading the "
                          f"bot's own signal (AI_ENTRY_TIMEOUT_MODE=proceed)")
                    handle_entry_verdict(symbol, decision, cur_bar, None, state)
                    return
                event(f"AI queue full {symbol} {direction} — entry blocked "
                      f"(fail-closed: no verdict = no trade)")
                # back off for a moment rather than hammering a provider that
                # is already behind (this is also what protects rate limits)
                state['ai_reject_cooldown_until'] = time.time() + float(
                    getattr(config, 'AI_QUEUE_BACKOFF_SECONDS', 10))
            return

        try:
            review = ai_reviewer.review(ctx)     # sync fallback (AI_ASYNC=false)
        except Exception as e:
            if config.ai_entry_proceeds_on_timeout():
                ai_entry_stats['proceeded_ai_error'] += 1
                event(f"AI SILENT {symbol} {direction} — review raised "
                      f"({type(e).__name__}); trading the bot's own signal")
                handle_entry_verdict(symbol, decision, cur_bar, None, state)
                return
            logger.error(f"AI review error ({type(e).__name__}); "
                         f"entry blocked (fail-closed)")
            send(msg_error(f"AI review failed ({type(e).__name__}) — "
                           f"entry blocked (fail-closed)"))
            return

    handle_entry_verdict(symbol, decision, cur_bar, review, state)


def build_ai_snapshot(symbol, decision, state):
    """One bounded, sanitized snapshot — shared by the sync and async lanes so
    the model always sees the same shape of data (and the cache key matches)."""
    signal = decision['signal']
    try:
        spread_pct = get_spread_percent(symbol)
    except Exception:
        spread_pct = None
    ctx = ai_reviewer.build_context(
        symbol, decision, signal, signal.get('atr'),
        decision.get('chosen_tf', 'M1'),
        open_positions=len(state['positions']),
        spread_pct=spread_pct)
    if news_watcher is not None:
        ctx["recent_news"] = news_watcher.recent_titles(5)
    if decision.get('ai_brief'):
        # the Analyst's earlier, independent read: the gate sees it too, so the
        # two AI opinions are consistent instead of two strangers
        ctx["ai_market_brief"] = decision['ai_brief']
        ctx["brief_consensus"] = decision.get('ai_consensus')
    return ctx


def handle_entry_verdict(symbol, decision, cur_bar, review, state):
    """One verdict → either the UNCHANGED deterministic path, or a block.

    Alerting, the reject cooldown and journal finalisation live here, so the
    inline (cache / sync) and asynchronous (bus) lanes behave identically —
    the AI has exactly one voice, whichever way it arrived."""
    direction = decision['direction']
    block_reason = {'v': None}
    if (review is not None and not review.approved
            and getattr(review, 'source', '') in ('ai_unavailable', 'ai_error')
            and config.ai_entry_proceeds_on_timeout()):
        # The AI could not ANSWER (provider down, rate limit, open circuit,
        # unparseable reply). That is not a REJECT: under the scalper policy
        # the bot trades its own signal. A genuine REJECT/WAIT never reaches
        # this branch and stays final.
        ai_entry_stats['proceeded_ai_error'] += 1
        event(f"AI SILENT {symbol} {direction} — {review.reason_code}; trading "
              f"the bot's own signal (AI_ENTRY_TIMEOUT_MODE=proceed)")
        try:
            ai_reviewer.journal.mark_execution(
                review.decision_id, risk_result="not_reached",
                execution="not_executed",
                final_action="ai_unavailable_bot_proceeded")
        except Exception as e:
            logger.error(f"AI journal annotate failed: {type(e).__name__}")
        review = None
    if review is not None:
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
            # Cooldown is ONE bar of the engine's timeframe, not five minutes:
            # v2.0's 300s silently killed every fresh signal after a reject.
            state['ai_reject_cooldown_until'] = time.time() + float(
                getattr(config, 'AI_REJECT_COOLDOWN_SECONDS', 45))
            # Nothing more to journal: the reviewer already wrote the row with
            # execution=not_executed / final_action=ai_<action>. And no order:
            # a veto is final, the deterministic path is never reached.
            return
        event(f"AI APPROVE {symbol} {direction} — {review.reason_code} "
              f"(conf {review.confidence:.2f} | {review.model} | key#"
              f"{review.key_number}){appeal} → risk checks")

    try:
        _execute_trade_inner(symbol, decision, cur_bar, review, block_reason)
    finally:
        # If the AI approved but the candidate never reached an order, record
        # the deterministic outcome. A no-op if the candidate already executed.
        _finalize_ai(block_reason['v'],
                     review.decision_id if review is not None else None)


def _finalize_ai(block_reason, decision_id):
    """Journal the deterministic fate of an AI-approved candidate. Never raises
    — a bookkeeping error must not cost a trade or crash the loop."""
    if ai_reviewer is None or not decision_id:
        return
    try:
        ai_reviewer.journal.finalize_pending(decision_id, block_reason=block_reason)
    except Exception as e:
        logger.error(f"AI journal finalize failed: {type(e).__name__}")


def sweep_pending_entries(symbol, state):
    """Drop candidates whose verdict never arrived (queue drop, provider dead,
    result expired). No verdict = no trade — and the journal says why."""
    if not state.get('pending_ai'):
        return
    max_age = float(getattr(config, 'AI_ENTRY_MAX_AGE_SECONDS', 45))
    # Scalper policy: the AI gets a short latency BUDGET. When it is spent and
    # the AI is still silent, the bot trades its own signal (freshness and all
    # deterministic risk gates still apply). Otherwise: no verdict = no trade.
    budget = config.ai_entry_budget_seconds() if \
        config.ai_entry_proceeds_on_timeout() else 0.0
    for skey, cand in list(state['pending_ai'].items()):
        age = time.time() - cand['ts']
        if budget > 0 and age >= budget:
            state['pending_ai'].pop(skey, None)
            _proceed_without_ai(
                symbol, cand, state, 'proceeded_timeout',
                f"no AI answer within {budget:.1f}s")
            continue
        if age <= max_age:
            continue
        state['pending_ai'].pop(skey, None)
        event(f"AI verdict TIMEOUT {symbol} {cand['decision']['direction']} — no "
              f"answer in {max_age:.0f}s; entry dropped (fail-closed)")
        signal_log.log(f"{symbol}:aitimeout",
                       f"● {symbol} AI entry verdict timed out — no trade")


def _stale_reason(sym, cand, max_age, max_drift):
    """Why a waiting candidate may no longer be traded — or None while it is
    still the trade on offer. Returns (journal_tag, event_text). One definition
    shared by the verdict collector and the timeout-proceed path, so 'is this
    still the same trade?' has exactly one answer."""
    age = time.time() - cand['ts']
    if age > max_age:
        return ('ai_verdict_stale',
                f"AI verdict STALE {sym} — {age:.0f}s old (>{max_age:.0f}s); "
                f"entry dropped")
    if _get_cur_bar(sym, cand.get('scan_tf', 'M1')) != cand['cur_bar']:
        return ('ai_verdict_stale_bar',
                f"AI verdict STALE {sym} — the signal bar rolled over while the "
                f"AI thought; entry dropped (a new scan re-arms)")
    if max_drift > 0 and cand.get('atr'):
        try:
            ti = get_symbol_info(sym)
            px = float(ti.bid) if ti else 0.0
        except Exception:
            px = 0.0
        if px and cand['price']:
            drift = abs(px - cand['price']) / cand['atr']
            if drift > max_drift:
                return ('ai_verdict_stale_drift',
                        f"AI verdict STALE {sym} — price moved {drift:.2f} ATR "
                        f"since the snapshot (>{max_drift}); entry dropped")
    return None


def _proceed_without_ai(sym, cand, state, stat, why):
    """The AI could not answer inside the budget: trade the bot's OWN signal,
    but only if the candidate is still fresh (same bar, price inside the drift
    budget). Every deterministic gate in _execute_trade_inner still runs."""
    max_age = float(getattr(config, 'AI_ENTRY_MAX_AGE_SECONDS', 45))
    max_drift = float(getattr(config, 'AI_ENTRY_MAX_DRIFT_ATR', 0.5))
    stale = _stale_reason(sym, cand, max_age, max_drift)
    if stale is not None:
        event(stale[1] + " (AI silent — no trade)")
        return
    ai_entry_stats[stat] += 1
    event(f"AI SILENT {sym} {cand['decision']['direction']} — {why}; trading "
          f"the bot's own signal (AI_ENTRY_TIMEOUT_MODE=proceed)")
    handle_entry_verdict(sym, cand['decision'], cand['cur_bar'], None, state)


def collect_entry_verdicts():
    """Stage 2 of the entry gate. A verdict is applied only if the trade it
    described is still the trade on offer: same bar, price inside the drift
    budget, positions under the cap. Otherwise it is dropped as STALE — an
    AI answer about the past is not a licence for the present."""
    if ai_bus is None:
        return
    max_age = float(getattr(config, 'AI_ENTRY_MAX_AGE_SECONDS', 45))
    max_drift = float(getattr(config, 'AI_ENTRY_MAX_DRIFT_ATR', 0.5))
    for full_key, ok, payload in ai_bus.collect('entry'):
        _, _, rest = full_key.partition(':')
        sym, _, skey = rest.partition('|')
        state = state_for(sym)
        cand = state.get('pending_ai', {}).pop(skey, None)
        if cand is None:
            # Already swept (timeout / bot traded without it) — never act on a
            # ghost, but do not leave its journal row "pending" forever either:
            # the AI's memory must show the verdict arrived too late to matter.
            if ok:
                _finalize_ai('ai_verdict_late_unused',
                             getattr(payload, 'decision_id', 0))
            continue
        if not ok:
            if config.ai_entry_proceeds_on_timeout():
                _proceed_without_ai(
                    sym, cand, state, 'proceeded_ai_error',
                    f"AI job raised ({type(payload).__name__})")
                continue
            logger.error(f"AI entry review raised ({type(payload).__name__}) for "
                         f"{sym} — entry blocked (fail-closed)")
            send(msg_error(f"AI review raised ({type(payload).__name__}) — "
                           f"entry blocked (fail-closed)"))
            continue
        stale = _stale_reason(sym, cand, max_age, max_drift)
        if stale is not None:
            event(stale[1])
            _finalize_ai(stale[0], getattr(payload, 'decision_id', 0))
            continue
        handle_entry_verdict(sym, cand['decision'], cand['cur_bar'], payload, state)


def _execute_trade_inner(symbol, decision, cur_bar, review, block_reason):
    state = state_for(symbol)
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

    # ── Cost gate: spread as a share of the stop distance (v2.1) ──
    # % of PRICE is a blunt tool: 0.05% of a $4,000 gold price is $2 of spread,
    # which is nothing on an H1 swing trade and fatal on an M5 scalp with a $4
    # stop. This measures the number that actually decides whether a scalp can
    # pay — and the AI snapshot carries the SAME value, so the deterministic bot
    # and the reviewer argue from one measurement instead of two.
    cost_cap = float(getattr(config, 'MAX_SPREAD_COST_PCT', 0) or 0)
    if cost_cap > 0:
        _atr_now = float(signal.get('atr') or 0)
        _t = mt5.symbol_info_tick(symbol)
        _px0 = 0.0
        if _t is not None:
            try:
                _px0 = float(_t.ask if direction == 'BUY' else _t.bid)
            except (TypeError, ValueError):
                _px0 = 0.0
        if decision.get('sl_price') is not None and _px0 > 0:
            _stop_dist = abs(_px0 - float(decision['sl_price']))
        elif decision.get('sl_mult') is not None and _atr_now > 0:
            _stop_dist = abs(float(decision['sl_mult'])) * _atr_now
        else:
            _stop_dist = 0.0
        _cost = ai_context.spread_cost_pct(spread_pct, _stop_dist, _px0)
        if _cost is not None and _cost > cost_cap:
            # change-only logging: a wide-spread market would otherwise shout
            # once per bar per symbol
            signal_log.log(f"{symbol}:cost",
                           f"● SKIP {symbol} {direction} — spread is {_cost:.0f}% "
                           f"of the stop distance (cap {cost_cap:.0f}%): the cost "
                           f"is the trade, no entry")
            if review is not None:
                block_reason['v'] = f"spread_cost:{_cost:.0f}pct"
            return
        decision['spread_cost_pct'] = _cost      # visible to the AI + the log

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

        # v2.3 $ scalping: size the lot so the market's own TP distance is worth
        # SCALP_TARGET_USD; refuse a trade the broker's minimum lot / the spread
        # would turn into something other than a small, fast scalp.
        scalp_meta = None
        if getattr(config, 'SCALP_USD_MODE', False):
            scalp_meta, _skip = _scalp_size(symbol, price, sl, tp, tick)
            if _skip:
                signal_log.log(f"{symbol}:scalpsize",
                               f"● SKIP {symbol} {direction} — {_skip}")
                if review is not None:
                    block_reason['v'] = "scalp_size"
                break
            if scalp_meta is not None:
                lot = scalp_meta['lot']

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
            # v2.3: what the Scalp Guard needs to judge this trade later
            _sl_usd = scalp_meta['sl_usd'] if scalp_meta else None
            if _sl_usd is None:
                _pu = risk_manager.usd_per_price_unit(symbol)
                _sl_usd = (abs(price - sl) * _pu * lot) if _pu else None
            scalp_guard.note_open(
                state['positions'][-1],
                target_usd=(scalp_meta['target_usd'] if scalp_meta
                            else float(config.SCALP_TARGET_USD)),
                sl_usd=_sl_usd, lot=lot)
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
            _usd_note = (f" | ${scalp_meta['tp_usd']:.2f} up / "
                         f"${scalp_meta['sl_usd']:.2f} down (spread "
                         f"${scalp_meta['spread_usd']:.2f})") if scalp_meta else ""
            event(f"OPEN {symbol} {direction} #{pos_ticket} @ {price:.{_digits}f} "
                  f"| TF {chosen_tf} | lot {lot} | SL {sl:.{_digits}f} "
                  f"TP {tp:.{_digits}f} | ML {ml_conf:.0%}{_usd_note}")
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
    logger.info(f"  BOSS SMC BOT  v2.3  —  Twin-brain AI ⇄ Bot  [{config.BOT_PROFILE}]")
    logger.info("=" * 57)
    logger.info(f"  engine={config.ENTRY_ENGINE} symbols={len(config.SYMBOLS)} "
                f"loop={config.ANALYSIS_INTERVAL_SECONDS}s "
                f"risk={config.RISK_PERCENT_PER_TRADE}%/trade "
                f"session={config.SESSION_START}-{config.SESSION_END}UTC "
                f"filter={config.SESSION_FILTER}")
    for _l in config.ai_status_lines():
        logger.info(f"  FLOW | {_l}")

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
                    n_pos = sum(len(state_for(s)['positions'])
                                for s in config.SYMBOLS)
                    # v2.1: the heartbeat states what is REALLY running. v2.0
                    # printed "AI gate ON" with the entire AI layer switched off.
                    logger.info(
                        f"♥ ALIVE | {sym} {price} | equity ${acc.equity:.2f} | "
                        f"floating ${acc.profit:+.2f} | open positions: {n_pos} "
                        f"| {ai_flow_summary()} | engine {config.ENTRY_ENGINE} "
                        f"| profile {config.BOT_PROFILE}")
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

            # ── AI ⇄ bot lane: apply finished verdicts (never blocks) ──
            if ai_bus is not None:
                try:
                    collect_briefs()
                    collect_entry_verdicts()
                    apply_position_verdicts()
                    apply_regime_verdict()
                except Exception as e:
                    logger.error(f"AI verdict collection error ({type(e).__name__})")

            # AI directives: an expired directive is debris, not a decision —
            # restore the owner's baseline first, then adapt if it is due.
            if ai_regime is not None:
                try:
                    for param, _old, base in ai_regime.expire_if_stale():
                        event(f"AI DIRECTIVE EXPIRED {param} → baseline {base}")
                    if ai_regime.due():
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
