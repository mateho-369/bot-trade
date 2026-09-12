# ============================================================
#   tg_alerts/alerts.py  — Enhanced Telegram Alerts v1.7.2
# ============================================================
import logging
import sys, os
from datetime import datetime
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger("Telegram")

TELEGRAM_OK = False
try:
    import telegram as tg_lib
    from telegram import Bot
    TELEGRAM_OK = True
    logger.info("Telegram library OK")
except Exception as e:
    logger.warning(f"Telegram disabled: {e}")

# Rate limiting for modifications
_last_mod_cache = {}

def send(message: str):
    if not message or not config.TELEGRAM_ENABLED or not TELEGRAM_OK:
        return
    if config.TELEGRAM_TOKEN in ("YOUR_BOT_TOKEN", "", None):
        return
    try:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_send_async(message))
            else:
                loop.run_until_complete(_send_async(message))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_send_async(message))
            loop.close()
    except Exception as e:
        logger.error(f"Telegram send error: {e}")


async def _send_async(message: str):
    try:
        bot = Bot(token=config.TELEGRAM_TOKEN)
        await bot.send_message(
            chat_id=str(config.TELEGRAM_CHAT_ID),
            text=message,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")


def _hr():
    return "━━━━━━━━━━━━━━━━━━━━━━━"


def msg_signal(symbol, direction, entry, tp, sl, atr, rsi, sig_type, ml_conf, lot):
    icon  = "🟢" if direction == "BUY" else "🔴"
    arrow = "▲" if direction == "BUY" else "▼"
    return (
        f"{icon}  <b>BOSS SMC  ·  {direction}</b>\n"
        f"{_hr()}\n"
        f"📊  Pair&nbsp;&nbsp;&nbsp;&nbsp;: <b>{symbol}</b>\n"
        f"{arrow}  Side&nbsp;&nbsp;&nbsp;&nbsp;: <b>{direction}</b>\n"
        f"💰  Entry&nbsp;&nbsp;&nbsp;: <code>{entry:.2f}</code>\n"
        f"✅  TP&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: <code>{tp:.2f}</code>\n"
        f"❌  SL&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: <code>{sl:.2f}</code>\n"
        f"{_hr()}\n"
        f"📈 ATR: {atr:.2f}&nbsp;&nbsp;·&nbsp;&nbsp;📉 RSI: {rsi:.1f}\n"
        f"🔍 {sig_type}&nbsp;&nbsp;·&nbsp;&nbsp;🤖 ML: {ml_conf:.0%}\n"
        f"📦 Lot: {lot}\n"
    )


def msg_closed(symbol, direction, entry, close_price, profit, win, stats):
    icon  = "✅" if win else "❌"
    emoji = "WIN" if win else "LOSS"
    pts = abs(close_price - entry)
    return (
        f"{icon}  <b>{emoji}  ·  {symbol} {direction}</b>\n"
        f"{_hr()}\n"
        f"💰  Entry&nbsp;&nbsp;&nbsp;: <code>{entry:.2f}</code>\n"
        f"🏁  Close&nbsp;&nbsp;&nbsp;: <code>{close_price:.2f}</code>\n"
        f"💵  PnL&nbsp;&nbsp;&nbsp;&nbsp;: <b>{profit:+.2f} USD</b>  ({pts:.2f} pts)\n"
        f"{_hr()}\n"
        f"🏆 Win rate: <b>{stats['winrate']:.1f}%</b>&nbsp;&nbsp;·&nbsp;&nbsp;📊 {stats['wins']}W – {stats['losses']}L\n"
    )


def msg_adjusted(symbol, ticket, old_sl, new_sl, old_tp, new_tp, reason):
    cache_key = f"{ticket}_{new_sl}_{new_tp}"
    now = datetime.now().timestamp()
    if cache_key in _last_mod_cache:
        if now - _last_mod_cache[cache_key] < 10:
            return None
    _last_mod_cache[cache_key] = now

    sl_str = f"{old_sl:.2f} → {new_sl:.2f}" if abs(old_sl - new_sl) > 0.0001 else f"{old_sl:.2f}"
    tp_str = f"{old_tp:.2f} → {new_tp:.2f}" if abs(old_tp - new_tp) > 0.0001 else f"{old_tp:.2f}"

    return (
        f"🔧  <b>Position modified  ·  {symbol}</b>\n"
        f"{_hr()}\n"
        f"🎫 Ticket: <code>{ticket}</code>\n"
        f"❌ SL: <code>{sl_str}</code>\n"
        f"✅ TP: <code>{tp_str}</code>\n"
        f"💡 {reason}\n"
    )

def msg_blacklist(symbol, direction, ml_conf, pattern_type):
    emoji  = "🚫" if pattern_type == "BLACKLIST" else "⭐"
    action = "BLOCKED" if pattern_type == "BLACKLIST" else "BOOSTED"
    return (
        f"{emoji}  <b>PATTERN {pattern_type}</b>\n"
        f"{_hr()}\n"
        f"📊 {symbol}  ·  {direction}\n"
        f"🧠 ML: {ml_conf:.0%}&nbsp;&nbsp;·&nbsp;&nbsp;⚡ {action}\n"
    )

def msg_milestone(m):
    return (
        f"💰  <b>PROFIT MILESTONE — bank it!</b>\n"
        f"{_hr()}\n"
        f"📈 Balance: <b>${m['balance']:.2f}</b>\n"
        f"💵 Profit: <b>+${m['profit']:.2f}</b>  (base ${m['base']})\n"
        f"{_hr()}\n"
        f"🏦 Withdraw: <b>${m['withdraw']}</b>\n"
        f"🔁 Keep in play: ${m['keep']:.2f}\n"
    )


def msg_ml_retrain(n_trades, winrate, n_patterns):
    return (
        f"🤖  <b>ML retrained</b>\n"
        f"{_hr()}\n"
        f"📊 Trades: {n_trades}&nbsp;&nbsp;·&nbsp;&nbsp;🏆 WR: {winrate:.1f}%\n"
        f"🧩 Patterns: {n_patterns} clusters\n"
        f"✅ Saved\n"
    )


def msg_bot_start(symbols):
    return (
        f"🚀  <b>BOSS SMC BOT v1.9.0 — started</b>\n"
        f"{_hr()}\n"
        f"📊 Symbols: <b>{', '.join(symbols)}</b>\n"
        f"⏱ Timeframe: M1  ·  loop 1.0s\n"
        f"🤖 ML: {'ON' if config.ML_ENABLED else 'OFF'}\n"
        f"Scanning... ✅\n"
    )

def msg_error(error_msg):
    return f"⚠️ <b>BOT ERROR</b>\n<code>{error_msg[:200]}</code>"
