import MetaTrader5 as mt5, time
for pw in ["369@Maka", "369@Makara"]:
    ok = mt5.initialize(login=414408585, password=pw, server="Exness-MT5Trial6", timeout=60000)
    print(f"pw={pw!r} initialize:", ok, mt5.last_error())
    if ok:
        a = mt5.account_info()
        if a: print(f"CONNECTED: {a.login} | {a.balance} {a.currency} | {a.server} | leverage {a.leverage} | trade_allowed={a.trade_allowed}")
        mt5.shutdown(); break
    mt5.shutdown(); time.sleep(3)
