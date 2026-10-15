import MetaTrader5 as mt5
ok = mt5.initialize()
print("initialize:", ok, mt5.last_error())
if ok:
    lg = mt5.login(414408585, password="369@Maka", server="Exness-MT5Trial6")
    print("login:", lg, mt5.last_error())
    if lg:
        a = mt5.account_info()
        print(f"account: {a.login} | balance {a.balance} {a.currency} | server {a.server} | trade_allowed {a.trade_allowed}")
    mt5.shutdown()
