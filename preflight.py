"""BOSS SMC BOT — preflight check  (v2.1)

One command that answers: "am I ready to run the bot?"

It checks the things that actually break first runs on a Windows box —
packages, a half-filled .env, missing folders, broker-terminal settings,
risk numbers and whether the AI is really wired up. It NEVER places,
modifies or closes a trade, and it never needs internet access.

    venv\\Scripts\\python.exe preflight.py            # offline checks
    venv\\Scripts\\python.exe preflight.py --connect  # also ping the MT5 terminal

Exit code 0 = ready. 1 = fix the [FIX] lines first.
"""

import argparse
import os
import sys

OK, FIX, WARN, INFO = "[OK ]", "[FIX]", "[!]", "[i]"
PY_MIN = (3, 9)
PY_MAX_SOFT = (3, 12)          # MetaTrader5 / numpy 1.26 wheels stop here

problems = []
warnings = []


_LIVE = {}          # facts learned from the live MT5 check (balance)


def say(tag, msg):
    print(f"  {tag} {msg}")


def need(msg):
    problems.append(msg)
    say(FIX, msg)


def soft(msg):
    warnings.append(msg)
    say(WARN, msg)


# ── 1. interpreter & platform ───────────────────────────────────────────
def check_python():
    print("\n1) Python")
    v = sys.version_info
    say(INFO, f"{v.major}.{v.minor}.{v.micro} · {sys.executable}")
    if (v.major, v.minor) < PY_MIN:
        need(f"Python {PY_MIN[0]}.{PY_MIN[1]}+ required (found {v.major}.{v.minor}).")
    if (v.major, v.minor) > PY_MAX_SOFT:
        soft(f"Python {v.major}.{v.minor} is newer than what requirements.txt pins "
             f"(MetaTrader5/numpy wheels may not exist). 3.10-3.12 is the safe range.")
    if os.name != "nt":
        soft(f"OS is {os.name} — MetaTrader5 only ships for Windows, so the live "
             f"trading loop cannot run here (offline tests still do).")


# ── 2. packages ─────────────────────────────────────────────────────────
def check_packages():
    print("\n2) Packages")
    hard = ["MetaTrader5", "pandas", "numpy", "sklearn", "schedule", "ta"]
    for name in hard + ["dotenv", "telegram", "matplotlib", "colorama", "joblib"]:
        try:
            __import__(name)
            say(OK, name)
        except Exception as e:
            msg = f"{name} not importable ({type(e).__name__}) — run install.bat"
            if name in hard:
                need(msg)
            else:
                soft(msg + " (optional)")


# ── 3. .env / secrets ──────────────────────────────────────────────────
REQUIRED_PLACEHOLDERS = ("your_account_number", "your_password", "your_broker_server")
OPTIONAL_PLACEHOLDERS = ("your_telegram_bot_token", "your_chat_id")


def check_env():
    print("\n3) .env")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        soft(".env not found — the bot runs on the fallback values in config.py. "
             "Copy .env.example to .env to set your account, symbols and AI keys.")
        return
    raw = open(path, encoding="utf-8", errors="ignore").read()
    left = [p for p in REQUIRED_PLACEHOLDERS if p in raw]
    if left:
        need(f".env still has template values: {', '.join(left)} — edit .env")
    optional = [p for p in OPTIONAL_PLACEHOLDERS if p in raw]
    if optional:
        soft(f"Telegram not configured yet ({', '.join(optional)}) — alerts stay off, "
             f"trading is unaffected")
    if not left and not optional:
        say(OK, "no placeholder values left")
    if len(raw) > 4000:
        soft(".env is unusually large; check nothing was pasted into it twice")


def check_broker(config):
    print("\n4) MT5 account")
    if not config.MT5_LOGIN:
        need("MT5_LOGIN is empty/0 — connector calls mt5.login(), so it must be set "
             "in .env (the terminal being logged in is not enough).")
    else:
        say(OK, f"login {config.MT5_LOGIN} @ {config.MT5_SERVER or 'terminal default server'}")
    if not config.MT5_PASSWORD:
        soft("MT5_PASSWORD empty — only fine if you keep the terminal logged in and "
             "never restart it headless")
    server = (config.MT5_SERVER or "").lower()
    if server and not any(k in server for k in ("demo", "trial", "investing")):
        soft(f"server '{config.MT5_SERVER}' does not look like a demo account — "
             f"REAL MONEY. If that is intended, keep LOT_SIZE small and run 2-3 "
             f"months of demo first (see REAL_WORLD_TEST.md).")
    for sym in config.SYMBOLS:
        if not sym.strip():
            need("SYMBOLS contains an empty entry (check the comma list in .env)")
    say(INFO, f"symbols: {', '.join(config.SYMBOLS)}")


def check_connect():
    """Optional: actually talk to the terminal. Nothing is ordered here."""
    print("\n4b) MT5 terminal (--connect)")
    try:
        import MetaTrader5 as mt5
    except Exception as e:
        soft(f"cannot import MetaTrader5 here ({type(e).__name__}) — skipping live check")
        return
    if not mt5.initialize():
        need(f"mt5.initialize() failed: {mt5.last_error()} — is the MT5 terminal "
             f"open, logged in, and 'Allow algorithmic trading' enabled?")
        mt5.shutdown()
        return
    info = mt5.terminal_info()
    say(OK, f"terminal build {getattr(info, 'build', '?')} · connected={getattr(info, 'connected', '?')}")
    acc = mt5.account_info()
    if acc:
        _LIVE["balance"] = float(acc.balance)
        say(INFO, f"balance {acc.balance} {acc.currency} · leverage 1:{acc.leverage} · "
                  f"margin_mode {getattr(acc, 'margin_mode', '?')}")
        if getattr(acc, "limit_orders", 0) and acc.limit_orders <= 0:
            soft("account reports no orders allowed — check the terminal/broker state")
    for sym in config.SYMBOLS:
        si = mt5.symbol_info(sym)
        if si is None:
            need(f"symbol '{sym}' not found on this account — fix SYMBOLS in .env")
            continue
        if not si.visible:
            say(WARN, f"'{sym}' is hidden in Market Watch — bot will try to enable it")
        tick = mt5.symbol_info_tick(sym)
        spread_pts = None if tick is None else round((tick.ask - tick.bid) / si.point)
        say(OK, f"{sym}: {si.digits} digits · spread ~{spread_pts} pts · "
                f"stop level {si.trade_stops_level} pts")
    mt5.shutdown()


# ── 5. trading config sanity (what the research says) ──────────────────
def check_strategy(config):
    print("\n5) Strategy & risk")
    say(INFO, f"profile={config.BOT_PROFILE} · engine={config.ENTRY_ENGINE} · "
              f"timeframe={config.TIMEFRAME}")
    if config.ENTRY_ENGINE == "SMC_MTF" and not config.ULTRA_FAST:
        soft("SMC_MTF is this repo's own measured loser (PF 0.93 after Exness spread). "
             "Use ENTRY_ENGINE=SR_H1 (PF 1.15) unless you are testing on demo.")
    if config.ULTRA_FAST:
        soft(f"BOT_PROFILE={config.BOT_PROFILE} disables RSI/ATR-expansion/blacklist/"
             f"ML-confidence and widens loss limits — demo only.")
    if getattr(config, "SCALPER", False):
        say(INFO, f"scalper AI lane · AI timeout {config.AI_TIMEOUT_SECONDS}s · "
                  f"entry budget {config.ai_entry_budget_seconds():.1f}s · "
                  f"on silence: {config.AI_ENTRY_TIMEOUT_MODE} · "
                  f"position review every {config.AI_POSITION_INTERVAL:.0f}s "
                  f"({config.AI_POSITION_VOTES} vote) · loop {config.ANALYSIS_INTERVAL_SECONDS}s")
    if getattr(config, "SCALPER", False):
        if config.ENTRY_ENGINE != "SMC_MTF":
            soft(f"BOT_PROFILE=scalper but ENTRY_ENGINE={config.ENTRY_ENGINE}: a value in "
                 f".env overrides the profile. {config.ENTRY_ENGINE} is not the fast M1 "
                 f"scalper - delete ENTRY_ENGINE from .env.")
        if getattr(config, "SCALP_SCAN_M1", False):
            say(INFO, "signals from M1 + M5 + M15 closed bars (SCALP_SCAN_M1)")
        else:
            soft("signals come from M5/M15 closed bars only: a handful a day on one "
                 "chart - not a fast scalper. SCALP_SCAN_M1=true adds M1.")
        if getattr(config, "SCALP_USD_MODE", False):
            say(INFO, f"$ scalping · target ${config.SCALP_TARGET_USD:g}/trade · "
                      f"lot cap {config.SCALP_MAX_LOT:g} · skip when spread > "
                      f"{config.SCALP_MAX_SPREAD_PCT_OF_TARGET:.0f}% of target · "
                      f"min lot may skip big-contract symbols (e.g. BTC)")
        if getattr(config, "SCALP_GUARD_ENABLED", False):
            say(INFO, f"scalp guard · stall {config.SCALP_STALL_SECONDS:.0f}s · giveback "
                      f"{config.SCALP_GIVEBACK_PCT:.0%} · early cut "
                      f"{config.SCALP_EARLY_CUT_PCT:.0%} · max hold "
                      f"{config.SCALP_MAX_HOLD_SECONDS:.0f}s")
        if config.AI_WORKER_THREADS < 2:
            soft(f"AI_WORKER_THREADS={config.AI_WORKER_THREADS}: with the Analyst on, "
                 f"briefs and entry reviews share the lane - use 3 (the scalper default; "
                 f"remove AI_WORKER_THREADS from .env).")
        if config.AI_TIMEOUT_SECONDS > 6:
            soft(f"AI_TIMEOUT_SECONDS={config.AI_TIMEOUT_SECONDS:g} is slow for a scalper "
                 f"(default 4); is an old value left in .env?")
    env_file = getattr(config, "ENV_FILE_USED", "")
    if env_file:
        inside = os.path.dirname(os.path.abspath(env_file)) == \
            os.path.dirname(os.path.abspath(config.__file__))
        if inside:
            soft(f"secrets are read from the repo folder ({env_file}). It is git-ignored, "
                 f"but safer outside the repo: move it to ~/.boss-bot/.env")
        else:
            say(OK, f"secrets file is outside the repo: {env_file}")
    if config.MAX_SPREAD_COST_PCT <= 0:
        soft("MAX_SPREAD_COST_PCT=0 → the cost gate is OFF: the bot can take trades whose "
             "spread is a big share of the stop (the #1 documented reason for losing).")
    elif config.MAX_SPREAD_COST_PCT > (35 if getattr(config, "SCALPER", False) else 25):
        soft(f"MAX_SPREAD_COST_PCT={config.MAX_SPREAD_COST_PCT} is loose; 10-20 is the "
             f"useful band (the scalper profile itself uses 30).")
    if getattr(config, "SCALPER", False) and getattr(config, "SCALP_USD_MODE", False):
        # $ scalping sizes the lot from the $ target, so %-risk x positions is not
        # what is at stake here - say what is.
        per_stop = config.SCALP_TARGET_USD            # stop ~ TP distance => ~ target
        worst = per_stop * config.MAX_POSITIONS
        say(INFO, f"$ risk · lot sized from the ${config.SCALP_TARGET_USD:g} target (the %-risk "
                  f"setting is not used) · a stop-out is ~${per_stop:g}, up to "
                  f"{config.MAX_POSITIONS} positions at once = ~${worst:g}")
        bal = _LIVE.get("balance")
        if bal and bal > 0:
            pct = worst / bal * 100.0
            lim = getattr(config, "DAILY_MAX_LOSS_PERCENT", 0) or 0
            say(INFO, f"that is ~{pct:.0f}% of the ${bal:,.2f} balance; daily loss halt = "
                      f"{lim:g}% (~${bal * lim / 100.0:,.2f})")
            if lim and pct >= lim:
                soft(f"all {config.MAX_POSITIONS} positions stopped out = ~{pct:.0f}% of the "
                     f"balance, at or above the {lim:g}% daily halt: on a small account a few "
                     f"stop-outs end the day. Lower SCALP_TARGET_USD (e.g. 1.5) or "
                     f"MAX_POSITIONS.")
    else:
        heat = round(config.RISK_PERCENT_PER_TRADE * config.MAX_POSITIONS, 2)
        say(INFO, f"risk {config.RISK_PERCENT_PER_TRADE}% × {config.MAX_POSITIONS} positions "
                  f"= {heat}% account heat · fixed LOT_SIZE={config.LOT_SIZE}")
        if heat > 3.0:
            soft(f"{heat}% of the account at risk at once is aggressive for a scalper")
    for name in ("DAILY_MAX_LOSS_PERCENT", "MAX_DRAWDOWN_PERCENT", "MIN_FREE_MARGIN_BUFFER",
                 "CONSEC_LOSS_LIMIT"):
        val = getattr(config, name, None)
        if val is None:
            soft(f"{name} missing from config — kill switch may be inactive")
        else:
            say(OK, f"{name} = {val}")
    if getattr(config, "SL_BUFFER_ENABLED", False):
        need("SL_BUFFER_ENABLED=True would WIDEN stops — the one thing this build keeps "
             "out of AI hands. Set it false unless you know why.")
    if getattr(config, "ML_ENABLED", False) and not os.path.exists(config.MODEL_PATH):
        soft("ML enabled but no trained model yet — the bot trains on first run "
             "(expect a few minutes of slower startup, or the gate is skipped).")


# ── 6. AI wiring ───────────────────────────────────────────────────────
def check_ai(config):
    print("\n6) AI ⇄ bot wiring")
    for line in config.ai_status_lines():
        say(INFO, line)
    roles = [("AI_ENABLED", config.ai_entry_on),
             ("AI_POSITION_ENABLED", config.ai_position_on),
             ("AI_REGIME_ENABLED", config.ai_regime_on)]
    wanted = [n for n, f in roles if getattr(config, n, False)]
    live = [n for n, f in roles if f()]
    if wanted and not live:
        need("an AI flag is ON but no role can run: set AI_PROVIDER=openai_compat and "
             "AI_API_KEYS=sk-... in .env (or turn the flags off so the log is honest)")
    elif live:
        say(OK, f"AI roles live: {', '.join(live)} · plumbing="
                f"{'READY' if config.ai_plumbing_ready() else 'MISSING'}")
        if len(getattr(config, "AI_API_KEYS", [])) < 2:
            soft("one AI key only: rotation cannot save you from rate limits — add a "
                 "2nd key in AI_API_KEYS if you run 4 symbols")
    else:
        say(INFO, "AI off — bot trades its deterministic path alone (nothing is broken)")
    scalper = getattr(config, "SCALPER", False)
    # the scalper profile ships its own, deliberately tighter numbers: compare with those
    caps = {"AI_ENTRY_MAX_AGE_SECONDS": 10.0 if scalper else 45.0,
            "AI_RESULT_TTL_SECONDS": 120.0,
            "AI_QUEUE_MAX": 12, "AI_TIMEOUT_SECONDS": 4.0 if scalper else 8.0}
    for k, healthy in caps.items():
        v = float(getattr(config, k, healthy))
        if k.endswith("_SECONDS") and v <= 0:
            need(f"{k}={v} makes the AI lane unusable")
        elif k == "AI_QUEUE_MAX" and v < len(config.SYMBOLS):
            soft(f"AI_QUEUE_MAX={v} < {len(config.SYMBOLS)} symbols: verdicts will be "
                 f"dropped under load")
        elif v < healthy / 3:
            soft(f"{k}={v} is much tighter than the tested default {healthy}")
    j = config.AI_JOURNAL_PATH
    say(INFO, f"journal: {j}")


# ── 7. writable state ──────────────────────────────────────────────────
def check_fs():
    print("\n7) Folders")
    for path in ("logs", os.path.dirname(config.MODEL_PATH),
                 os.path.dirname(config.TRADE_LOG_PATH)):
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".preflight_write")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            say(OK, path)
        except Exception as e:
            need(f"cannot write inside '{path}' ({e}) — run outside Program Files, "
                 f"or fix permissions")


def main():
    ap = argparse.ArgumentParser(description="BOSS SMC BOT setup check")
    ap.add_argument("--connect", action="store_true",
                    help="also ping the MetaTrader 5 terminal (no orders are sent)")
    args = ap.parse_args()

    global config
    print("=" * 66)
    print("  BOSS SMC BOT — preflight  (nothing is traded by this script)")
    print("=" * 66)
    try:
        import config
    except Exception as e:
        print(f"  {FIX} config.py did not import: {e}")
        return 1
    check_python()
    check_packages()
    check_env()
    check_broker(config)
    if args.connect:
        check_connect()
    check_strategy(config)
    check_ai(config)
    check_fs()

    print("\n" + "=" * 66)
    if problems:
        print(f"  NOT READY — {len(problems)} thing(s) to fix:")
        for p in problems:
            print(f"    · {p}")
    else:
        print("  READY — run:  run_bot_forever.bat")
    if warnings:
        print(f"  {len(warnings)} note(s) above marked [!] — read them, they are "
              f"safety/quality, not blockers.")
    print("=" * 66)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
