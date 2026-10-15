import MetaTrader5 as mt5
ok = mt5.initialize()
print("bare initialize:", ok, mt5.last_error())
if ok:
    lg = mt5.login(414408585, password="369@Maka", server="Exness-MT5Trial6")
    print("login:", lg, mt5.last_error())
    if lg:
        a = mt5.account_info(); s = mt5.symbol_info("XAUUSDm")
        print(f"account ok: {a.login} {a.balance} {a.currency}")
        print(f"XAUUSDm: visible={s.visible if s else None} trade_allowed={s.trade_mode if s else None}")
    mt5.shutdown()
