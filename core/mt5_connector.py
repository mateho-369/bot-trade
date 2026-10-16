# ============================================================
#   core/mt5_connector.py
#   Handles all MT5 connection, data fetching, order placing
# ============================================================

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz
import logging
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger("MT5Connector")

TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
}

# ── CONNECTION ───────────────────────────────────────────────

def connect():
    if not config.MT5_LOGIN or not config.MT5_PASSWORD or not config.MT5_SERVER:
        logger.error(
            "MT5 credentials missing — set MT5_LOGIN / MT5_PASSWORD / MT5_SERVER "
            "in the .env file (never commit real credentials to the repo)."
        )
        return False
    if not mt5.initialize():
        logger.error(f"MT5 initialize failed: {mt5.last_error()}")
        return False
    login_result = mt5.login(
        login=config.MT5_LOGIN,
        password=config.MT5_PASSWORD,
        server=config.MT5_SERVER
    )
    if not login_result:
        logger.error(f"MT5 login failed: {mt5.last_error()}")
        return False
    info = mt5.account_info()
    # v3 Hard Rule #2: refuse non-demo accounts unless the owner set
    # ALLOW_LIVE=true. Only enforced for a real (string) server name so
    # mocked terminals in offline tests are unaffected.
    _server = getattr(info, "server", "") if info is not None else ""
    if isinstance(_server, str) and _server.strip():
        _gate = check_live_start(_server,
                                 bool(getattr(config, "ALLOW_LIVE", False)))
        if not _gate.allowed:
            logger.error(f"MT5 start refused: {_gate.reason}")
            mt5.shutdown()
            return False
    logger.info(f"MT5 Connected | Account: {info.login} | "
                f"Balance: {info.balance} {info.currency} | "
                f"Server: {info.server}")
    return True

def disconnect():
    mt5.shutdown()
    logger.info("MT5 disconnected")

def is_connected():
    return mt5.terminal_info() is not None

# ── DATA FETCHING ────────────────────────────────────────────

def get_candles(symbol, timeframe_str, count=500):
    """
    Fetch OHLCV candles from MT5.
    Skips bar 0 (forming bar) — same as Pine confirmed bar logic.
    count=500 for live scanning, use get_candles_history() for backtest.
    """
    tf    = TF_MAP.get(timeframe_str, mt5.TIMEFRAME_M1)
    # pos=1 skips the live forming bar, count+1 to get enough confirmed bars
    rates = mt5.copy_rates_from_pos(symbol, tf, 1, count)

    if rates is None or len(rates) == 0:
        logger.warning(f"No candles for {symbol} {timeframe_str}: {mt5.last_error()}")
        return None

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    df = df[['open','high','low','close','tick_volume']].copy()
    df.rename(columns={'tick_volume': 'volume'}, inplace=True)
    return df


def get_candles_history(symbol, timeframe_str, days=30):
    """
    Fetch a large historical window for backtesting and ML training.
    days=30 → ~43,200 M1 bars → plenty of trades for ML
    """
    tf    = TF_MAP.get(timeframe_str, mt5.TIMEFRAME_M1)
    utc   = pytz.utc
    end   = datetime.now(utc)
    start = end - timedelta(days=days)

    rates = mt5.copy_rates_range(symbol, tf, start, end)

    if rates is None or len(rates) == 0:
        logger.warning(f"No history for {symbol}: {mt5.last_error()}")
        # Fallback: try copy_rates_from_pos with large count
        logger.info(f"Trying fallback fetch for {symbol}...")
        rates = mt5.copy_rates_from_pos(symbol, tf, 1, 20000)
        if rates is None or len(rates) == 0:
            return None

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    df = df[['open','high','low','close','tick_volume']].copy()
    df.rename(columns={'tick_volume': 'volume'}, inplace=True)

    logger.info(f"[{symbol}] Fetched {len(df)} bars "
                f"({df.index[0].date()} → {df.index[-1].date()})")
    return df


def get_symbol_info(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        logger.warning(f"Symbol info not found: {symbol}")
    return info


def get_spread_percent(symbol):
    """Spread as % of price — auto-scales for all symbols"""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or tick.bid == 0:
        return 999.0
    return (tick.ask - tick.bid) / tick.bid * 100.0


def get_account_info():
    return mt5.account_info()

# NOTE: close_position / modify_position are defined ONCE, near the end of
# this file (after get_closed_trades). Earlier duplicates were removed —
# Python silently keeps only the LAST definition, which caused a
# signature mismatch (AI CLOSE calls failed with TypeError).

# ── ORDER EXECUTION ──────────────────────────────────────────

def place_buy(symbol, lot, sl, tp, comment="BOSS BUY"):
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if tick is None or info is None:
        return None, "Symbol info error"

    price = tick.ask
    sl    = round(sl, info.digits)
    tp    = round(tp, info.digits)

    # Validate min stop distance
    min_stop = info.trade_stops_level * info.point
    if (tp - price) < min_stop or (price - sl) < min_stop:
        return None, f"Stop too close (min_stop={min_stop:.5f})"

    # Snapshot existing tickets so we can identify the position this order
    # just created (picking max(p.time) is ambiguous when two positions —
    # e.g. main + flash hedge — open in the same second).
    prior = _ticket_set(symbol)

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       float(lot),
        "type":         mt5.ORDER_TYPE_BUY,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "deviation":    50,
        "magic":        505001,
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result is None:
        return None, str(mt5.last_error())
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        # Try IOC filling if FOK fails
        request["type_filling"] = mt5.ORDER_FILLING_IOC
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            return None, f"Retcode:{result.retcode if result else '?'} {result.comment if result else ''}"

    logger.info(f"[{symbol}] BUY | Price:{price} TP:{tp} SL:{sl} Ticket:{result.order}")
    return _resolve_new_ticket(symbol, prior, result), "OK"


def _ticket_set(symbol):
    try:
        return {p.ticket for p in (mt5.positions_get(symbol=symbol) or ())}
    except Exception:
        return set()


def _resolve_new_ticket(symbol, prior, result):
    """Return the POSITION ticket created by a just-sent order.

    Positions are managed by position id (the order id can differ). The
    reliable way to find the new one is to diff the ticket set taken before
    the order — picking max(p.time) is wrong when two positions share the
    same open second (main + hedge)."""
    new = _ticket_set(symbol) - prior
    if len(new) == 1:
        return new.pop()
    try:
        poss = mt5.positions_get(symbol=symbol)
        if poss:
            return max(poss, key=lambda p: p.time).ticket
    except Exception:
        pass
    return getattr(result, "order", None)


def place_sell(symbol, lot, sl, tp, comment="BOSS SELL"):
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if tick is None or info is None:
        return None, "Symbol info error"

    price = tick.bid
    sl    = round(sl, info.digits)
    tp    = round(tp, info.digits)

    min_stop = info.trade_stops_level * info.point
    if (price - tp) < min_stop or (sl - price) < min_stop:
        return None, f"Stop too close (min_stop={min_stop:.5f})"

    prior = _ticket_set(symbol)

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       float(lot),
        "type":         mt5.ORDER_TYPE_SELL,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "deviation":    50,
        "magic":        505001,
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }
    result = mt5.order_send(request)
    if result is None:
        return None, str(mt5.last_error())
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        request["type_filling"] = mt5.ORDER_FILLING_IOC
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            return None, f"Retcode:{result.retcode if result else '?'} {result.comment if result else ''}"

    logger.info(f"[{symbol}] SELL | Price:{price} TP:{tp} SL:{sl} Ticket:{result.order}")
    return _resolve_new_ticket(symbol, prior, result), "OK"


def has_open_position(symbol):
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return False
    return len([p for p in positions if p.magic == 505001]) > 0


def get_open_positions(symbol):
    """Get all open positions for this symbol with the bot's magic number"""
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return []
    return [p for p in positions if p.magic == 505001]

def get_position_by_ticket(ticket):
    """Get a specific position by its ticket number"""
    positions = mt5.positions_get(ticket=ticket)
    if positions is None or len(positions) == 0:
        return None
    return positions[0]


def get_closed_trades(symbol, count=50, days=7):
    """Fetch recent closed trades for the symbol (last `days` days).

    Default window is 7 days for efficiency. Callers that track positions
    which may be held longer than that (wide SL/TP) should widen it —
    main.py falls back to days=30 for positions whose close deal it can't
    find in the default window.
    """
    now   = datetime.now()
    start = now - timedelta(days=days)
    # FIX: the `group=` parameter of history_deals_get() is a BROKER COMPANY
    # name — passing "*XAUUSD*" returned no deals at all, so closed trades were
    # never detected: live win/loss outcomes were never logged, the ML never
    # retrained on real trades, and open-position tracking could drop positions.
    # Fetch by date range and filter the symbol ourselves.
    history = mt5.history_deals_get(start, now)
    if history is None:
        return []

    sym_up = symbol.upper()
    trades = []
    for deal in reversed(history): # Get most recent first
        if deal.magic != 505001:
            continue
        if deal.entry != mt5.DEAL_ENTRY_OUT:
            continue
        deal_sym = (deal.symbol or "").upper()
        if sym_up not in deal_sym and deal_sym not in sym_up:
            continue
        trades.append({
            "ticket":  deal.order,
            "symbol":  deal.symbol,
            "type":    "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL",
            "profit":  deal.profit + deal.swap + deal.commission,
            "volume":  deal.volume,
            "price":   deal.price,
            "time":    datetime.fromtimestamp(deal.time),
            "comment": deal.comment,
            "position_id": deal.position_id
        })
        if len(trades) >= count:
            break
    return trades


def modify_position(ticket, new_sl, new_tp):
    """Modify an open position's SL/TP. Returns True on success."""
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": int(ticket),
        "sl":       round(float(new_sl), 5),
        "tp":       round(float(new_tp), 5),
    }
    result = mt5.order_send(request)
    if result is None:
        logger.warning(f"Modify SLTP {ticket}: {mt5.last_error()}")
        return False
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.warning(f"Modify SLTP {ticket}: retcode {result.retcode} {result.comment}")
        return False
    return True


def close_position(symbol, ticket, comment="BOSS CLOSE"):
    """Close an open position at market (opposite side, same volume).
    Returns (ok, detail)."""
    pos = get_position_by_ticket(ticket)
    if pos is None:
        return False, "position not found"
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False, "no tick"
    if pos.type == mt5.ORDER_TYPE_BUY:
        otype, price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        otype, price = mt5.ORDER_TYPE_BUY, tick.ask
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       float(pos.volume),
        "type":         otype,
        "price":        price,
        "sl":           0.0,
        "tp":           0.0,
        "deviation":    50,
        "magic":        505001,
        "comment":      comment,
        "position":     int(ticket),
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None:
        return False, str(mt5.last_error())
    ok_codes = (mt5.TRADE_RETCODE_DONE,
                getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010))
    if result.retcode not in ok_codes:
        return False, f"retcode {result.retcode} {result.comment}"
    logger.info(f"[{symbol}] Closed #{ticket} @ {price:.2f} ({comment})")
    return True, "OK"
