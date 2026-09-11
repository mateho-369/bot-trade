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
    return result.order, "OK"


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
    return result.order, "OK"


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


def get_closed_trades(symbol, count=50):
    """Fetch recent closed trades for the symbol"""
    now   = datetime.now()
    # Fetch from last 7 days for efficiency, but filter for the count
    start = now - timedelta(days=7)
    history = mt5.history_deals_get(start, now, group=f"*{symbol}*")
    if history is None:
        return []
    
    trades = []
    for deal in reversed(history): # Get most recent first
        if deal.magic != 505001:
            continue
        if deal.entry != mt5.DEAL_ENTRY_OUT:
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
