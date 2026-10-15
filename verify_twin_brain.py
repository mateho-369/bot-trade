# ============================================================
#   verify_twin_brain.py
#   Offline tests for the v2.3 twin-brain scalper (no network, no broker).
#
#   What this proves:
#     1. only BOT_PROFILE=scalper turns the new lanes on; other profiles are
#        byte-for-byte the v2.2 behaviour
#     2. the Analyst's reply contract is strict (anything odd = no brief)
#     3. the AI-vs-bot consensus matrix: AGREE / CONFLICT / NEUTRAL / NO_BRIEF,
#        with stale and price-ran-away briefs never counting
#     4. at entry: AGREE -> traded at once WITHOUT waiting for the AI;
#        CONFLICT -> the bot obeys the AI; no/neutral brief -> the per-signal
#        gate decides exactly as in v2.2; an Analyst crash never costs a trade
#     5. the thesis-flip close is confirmed, newer than the trade, and never
#        double-counts one brief
#     6. the Scalp Guard: bank / give-back / stall / early-cut / max-hold
#     7. $ sizing: the lot makes the market's TP distance worth the target, and
#        a trade that a broker minimum lot or a wide spread would turn into a
#        non-scalp is skipped
#     8. the loop plumbing (refresh -> bus -> accept) works and degrades safely
#
#   Run:  python3 verify_twin_brain.py
# ============================================================

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

sys.modules['MetaTrader5'] = MagicMock()
import MetaTrader5 as mt5          # noqa: E402  (the mock, deliberately)
mt5.ORDER_TYPE_BUY = 0
mt5.ORDER_TYPE_SELL = 1
import numpy as np                 # noqa: E402
import pandas as pd                # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="bot_twin_test_")

import _isolate_env                # noqa: F401,E402  (never read the real .env)
import config                        # noqa: E402
config.DATA_ROOT_PATH = _TMP
config.MODEL_PATH = f"{_TMP}/models/smc_model.pkl"
config.SCALER_PATH = f"{_TMP}/models/smc_scaler.pkl"
config.TRADE_LOG_PATH = f"{_TMP}/data/trade_log.csv"
config.PATTERN_DATA_PATH = f"{_TMP}/data/patterns.json"
config.AI_JOURNAL_PATH = f"{_TMP}/data/ai_decisions.csv"
config.AI_REGIME_STATE_PATH = f"{_TMP}/data/ai_directives.json"
config.LOG_PATH = f"{_TMP}/bot.log"
config.CRASH_LOG_PATH = f"{_TMP}/crash.log"
config.TELEGRAM_ENABLED = False
config.SYMBOLS = ["XAUUSD"]
config.MAX_SPREAD_COST_PCT = 0.0
config.ENTRY_ENGINE = "SR_H1"

import main                          # noqa: E402
from core import ai_brief as ab      # noqa: E402
from core import scalp_guard as sg   # noqa: E402
from core import risk_manager        # noqa: E402
from core.ai_bus import AIBus        # noqa: E402
from core.ai_review import AIReviewResult   # noqa: E402
from core.ai_provider import AIInvalidResponse, AIUnavailable   # noqa: E402


# ── helpers ─────────────────────────────────────────────────
def _load_config_copy(profile, **env):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    spec = importlib.util.spec_from_file_location(
        "config_probe_tb", os.path.join(ROOT, "config.py"))
    mod = importlib.util.module_from_spec(spec)
    envv = {"BOT_PROFILE": profile, **{k: str(v) for k, v in env.items()}}
    with patch.dict(os.environ, envv, clear=True), \
            patch.dict(sys.modules, {"dotenv": fake_dotenv}):
        spec.loader.exec_module(mod)
    return mod


def _brief(bias="BUY", conf=0.8, state="TREND", stand=False, price=2000.0,
           atr=2.0, seq=1, symbol="XAUUSD", code="T"):
    return ab.Brief(symbol=symbol, bias=bias, confidence=conf, state=state,
                    stand_aside=stand, reason_code=code, reason="test brief",
                    price=price, atr=atr, bar=1, seq=seq)


def _accepted(briefer, **kw):
    b = _brief(**kw)
    briefer.accept(b)
    return b


def _verdict(action="APPROVE", conf=0.9, decision_id=1):
    return AIReviewResult(approved=action == "APPROVE", action=action,
                          confidence=conf, reason_code="T", reason="t",
                          model="fake", key_number=1, decision_id=decision_id,
                          source="ai")


def _decision(price=2000.0, atr=2.0, direction="BUY"):
    signal = {"atr": atr, "atr_pct": 0.1, "rsi": 55.0, "close": price,
              "body_pct": 0.05, "close_vs_ema": 0.02, "hour_utc": 10,
              "bull_sweep": False, "bear_sweep": False, "bull_bos": True,
              "bear_bos": False, "signal": direction,
              "last5_candles": [], "pattern_score": 2,
              "dist_to_high_atr": 1.0, "dist_to_low_atr": 1.0,
              "bar_time": pd.Timestamp("2026-01-01 10:00")}
    sl = price - atr * 0.5 if direction == "BUY" else price + atr * 0.5
    return {"engine": "SR_H1", "direction": direction, "n_positions": 1,
            "chosen_tf": "H1", "signal": signal, "sl_price": sl,
            "sl_atr": atr, "rr": 2.0, "score": 1, "summary": "test"}


def _df(n=60, drift=0.3, atr=1.0, start=2000.0):
    close = start + np.arange(n) * drift
    return pd.DataFrame({"open": close - drift / 2, "high": close + 0.4,
                         "low": close - 0.4, "close": close,
                         "atr": np.full(n, atr)})


class _FakeClient:
    name = "openai_compat"
    model = "fake-model"
    last_key_number = 1

    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, 0

    def complete(self, messages, schema):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return json.dumps(self.reply), "fake-model", "openai_compat"


_GOOD = {"bias": "BUY", "confidence": 0.8, "state": "TREND",
         "stand_aside": False, "reason_code": "MOMO", "reason": "bars drive up"}


# ══════════════════════════════════════════════════════════
#   1. profiles
# ══════════════════════════════════════════════════════════
class TestProfiles(unittest.TestCase):
    def test_scalper_turns_the_new_lanes_on(self):
        c = _load_config_copy("scalper")
        for flag in ("AI_BRIEF_ENABLED", "AI_BRIEF_FASTLANE", "AI_BRIEF_FLIP_CLOSE",
                     "SCALP_USD_MODE", "SCALP_GUARD_ENABLED"):
            self.assertTrue(getattr(c, flag), flag)
        self.assertEqual(c.AI_POSITION_INTERVAL, 15.0)
        self.assertEqual(c.MAX_SPREAD_COST_PCT, 30.0,
                         "a $2-5 scalp must not pay an unbounded spread")
        self.assertGreaterEqual(c.AI_WORKER_THREADS, 3)

    def test_other_profiles_are_unchanged(self):
        for profile in ("balanced", "ultra_fast"):
            c = _load_config_copy(profile)
            for flag in ("AI_BRIEF_ENABLED", "AI_BRIEF_FASTLANE",
                         "AI_BRIEF_FLIP_CLOSE", "SCALP_USD_MODE",
                         "SCALP_GUARD_ENABLED"):
                self.assertFalse(getattr(c, flag), f"{profile}:{flag}")
            self.assertEqual(c.AI_WORKER_THREADS, 1, profile)
        self.assertEqual(_load_config_copy("ultra_fast").MAX_SPREAD_COST_PCT, 0.0)
        self.assertEqual(_load_config_copy("balanced").MAX_SPREAD_COST_PCT, 18.0)

    def test_the_shipped_env_template_does_not_override_the_profile(self):
        """A value written in .env beats the profile. v2.2's template set
        ENTRY_ENGINE=SR_H1, which silently turned BOT_PROFILE=scalper into an
        hourly swing bot. Profile-sensitive keys must ship commented out."""
        sensitive = {"ENTRY_ENGINE", "RISK_PERCENT_PER_TRADE", "MAX_POSITIONS",
                     "MAX_SPREAD", "MAX_SPREAD_COST_PCT", "BREAK_EVEN_PROGRESS",
                     "AI_WORKER_THREADS", "AI_ENTRY_MAX_AGE_SECONDS",
                     "AI_ENTRY_MAX_DRIFT_ATR", "AI_REJECT_COOLDOWN_SECONDS",
                     "AI_TIMEOUT_SECONDS", "AI_MAX_RETRIES", "AI_POSITION_VOTES",
                     "AI_ENTRY_APPEAL", "AI_POSITION_INTERVAL"}
        live = {}
        with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    live[k.strip()] = v.strip()
        self.assertEqual(sensitive & set(live), set(),
                         "these would override the chosen profile")
        # and the real thing: scalper + the template's own live values
        env = {k: v.split("#")[0].strip() for k, v in live.items()}
        env["BOT_PROFILE"] = "scalper"
        c = _load_config_copy("scalper", **{k: v for k, v in env.items()
                                            if k != "BOT_PROFILE"})
        self.assertEqual(c.ENTRY_ENGINE, "SMC_MTF")
        self.assertEqual(c.AI_TIMEOUT_SECONDS, 4.0)
        self.assertTrue(c.SCALP_USD_MODE and c.SCALP_GUARD_ENABLED)

    def test_brief_flag_needs_working_plumbing(self):
        c = _load_config_copy("scalper")
        self.assertFalse(c.ai_brief_on(), "no provider/keys -> analyst OFFLINE")
        c = _load_config_copy("scalper", AI_PROVIDER="openai_compat",
                              AI_API_KEYS="k1")
        self.assertTrue(c.ai_brief_on())
        self.assertTrue(any("AI analyst" in l for l in c.ai_status_lines()))


# ══════════════════════════════════════════════════════════
#   2. the Analyst's reply contract
# ══════════════════════════════════════════════════════════
class TestBriefContract(unittest.TestCase):
    def test_a_good_reply_validates(self):
        out = ab.validate_brief(dict(_GOOD))
        self.assertEqual(out["bias"], "BUY")
        self.assertEqual(out["confidence"], 0.8)

    def test_anything_off_contract_is_rejected(self):
        bad = [
            {**_GOOD, "extra": 1},
            {**_GOOD, "bias": "LONG"},
            {**_GOOD, "confidence": 1.2},
            {**_GOOD, "confidence": True},
            {**_GOOD, "confidence": float("nan")},
            {**_GOOD, "state": "WILD"},
            {**_GOOD, "stand_aside": "yes"},
            {**_GOOD, "reason_code": ""},
            {**_GOOD, "reason": "x" * 201},
            {k: v for k, v in _GOOD.items() if k != "bias"},
        ]
        for obj in bad:
            with self.assertRaises(AIInvalidResponse, msg=str(obj)):
                ab.validate_brief(obj)


# ══════════════════════════════════════════════════════════
#   3. consensus: the bot's signal vs the AI's brief
# ══════════════════════════════════════════════════════════
class TestConsensus(unittest.TestCase):
    def setUp(self):
        self.br = ab.MarketBriefer()

    def verdict(self, direction, brief, **kw):
        return ab.consensus(direction, brief, **kw)[0]

    def test_matrix(self):
        b = _accepted(self.br, bias="BUY", conf=0.8)
        self.assertEqual(self.verdict("BUY", b), ab.AGREE)
        self.assertEqual(self.verdict("SELL", b), ab.CONFLICT)
        weak = _accepted(self.br, bias="SELL", conf=0.60, seq=2)   # < veto 0.65
        self.assertEqual(self.verdict("BUY", weak), ab.NEUTRAL,
                         "a weak opposite view does not veto a scalper")
        flat = _accepted(self.br, bias="NEUTRAL", conf=0.9, seq=3)
        self.assertEqual(self.verdict("BUY", flat), ab.NEUTRAL)
        unsure = _accepted(self.br, bias="BUY", conf=0.50, seq=4)  # < agree 0.55
        self.assertEqual(self.verdict("BUY", unsure), ab.NEUTRAL)
        self.assertEqual(self.verdict("BUY", None), ab.NO_BRIEF)

    def test_stand_aside_is_the_ai_saying_do_nothing(self):
        b = _accepted(self.br, bias="BUY", conf=0.9, state="CHAOS", stand=True)
        self.assertEqual(self.verdict("BUY", b), ab.CONFLICT,
                         "even a same-direction signal: the AI says stand aside")
        meh = _accepted(self.br, bias="BUY", conf=0.6, stand=True, seq=2)
        self.assertEqual(self.verdict("BUY", meh), ab.NEUTRAL,
                         "an unsure stand-aside must not count as AGREE either")

    def test_a_stale_brief_never_counts(self):
        b = _accepted(self.br)
        b.ts -= config.AI_BRIEF_TTL_SECONDS + 5
        self.assertEqual(self.verdict("BUY", b), ab.NO_BRIEF)
        self.assertEqual(self.verdict("SELL", b), ab.NO_BRIEF,
                         "an old opinion cannot veto either")

    def test_a_brief_the_price_has_outrun_never_counts(self):
        b = _accepted(self.br, price=2000.0, atr=2.0)
        self.assertEqual(self.verdict("BUY", b, price=2000.5, atr=2.0), ab.AGREE)
        # 0.6 ATR cap = 1.2; price is 3.0 away
        self.assertEqual(self.verdict("BUY", b, price=2003.0, atr=2.0), ab.NO_BRIEF)


class TestBotView(unittest.TestCase):
    def test_leans_with_the_tape(self):
        self.assertEqual(ab.bot_view(_df(drift=0.4), "BUY")["lean"], "BUY")
        self.assertEqual(ab.bot_view(_df(drift=-0.4, start=2100.0), "SELL")["lean"],
                         "SELL")

    def test_no_data_is_neutral_not_an_error(self):
        self.assertEqual(ab.bot_view(None)["lean"], "NEUTRAL")
        self.assertEqual(ab.bot_view(_df(n=5))["lean"], "NEUTRAL")
        bad = _df()
        bad["atr"] = 0.0
        self.assertEqual(ab.bot_view(bad)["lean"], "NEUTRAL")


# ══════════════════════════════════════════════════════════
#   4. thesis flip
# ══════════════════════════════════════════════════════════
class TestFlipTracker(unittest.TestCase):
    def setUp(self):
        self._saved = (config.AI_BRIEF_FLIP_CONF, config.AI_BRIEF_FLIP_CONFIRMS)
        config.AI_BRIEF_FLIP_CONF, config.AI_BRIEF_FLIP_CONFIRMS = 0.70, 2
        self.ft = ab.FlipTracker()
        self.br = ab.MarketBriefer()
        self.opened = time.time() - 30

    def tearDown(self):
        config.AI_BRIEF_FLIP_CONF, config.AI_BRIEF_FLIP_CONFIRMS = self._saved

    def test_needs_two_confident_opposite_briefs(self):
        b1 = _accepted(self.br, bias="SELL", conf=0.8, seq=1)
        self.assertFalse(self.ft.observe(1, 1, b1, self.opened), "one is a wobble")
        b2 = _accepted(self.br, bias="SELL", conf=0.8, seq=2)
        self.assertTrue(self.ft.observe(1, 1, b2, self.opened), "two in a row = flip")

    def test_one_brief_is_never_counted_twice(self):
        b1 = _accepted(self.br, bias="SELL", conf=0.9, seq=1)
        for _ in range(5):               # the loop looks at it every 0.3s
            self.assertFalse(self.ft.observe(1, 1, b1, self.opened))

    def test_a_non_opposing_brief_resets_the_count(self):
        self.ft.observe(1, 1, _accepted(self.br, bias="SELL", conf=0.9, seq=1),
                        self.opened)
        self.ft.observe(1, 1, _accepted(self.br, bias="BUY", conf=0.9, seq=2),
                        self.opened)
        self.assertFalse(self.ft.observe(
            1, 1, _accepted(self.br, bias="SELL", conf=0.9, seq=3), self.opened))

    def test_weak_or_old_opinions_do_not_count(self):
        self.assertFalse(self.ft.observe(
            1, 1, _accepted(self.br, bias="SELL", conf=0.6, seq=1), self.opened))
        self.assertFalse(self.ft.observe(
            1, 1, _accepted(self.br, bias="SELL", conf=0.6, seq=2), self.opened))
        # a brief that arrived BEFORE the trade says nothing about this trade
        old = _accepted(self.br, bias="SELL", conf=0.95, seq=3)
        old.ts = self.opened - 5
        self.assertFalse(self.ft.observe(2, 1, old, self.opened))

    def test_sell_trades_flip_on_buy_briefs(self):
        self.ft.observe(7, -1, _accepted(self.br, bias="BUY", conf=0.9, seq=1),
                        self.opened)
        self.assertTrue(self.ft.observe(
            7, -1, _accepted(self.br, bias="BUY", conf=0.9, seq=2), self.opened))


# ══════════════════════════════════════════════════════════
#   5. the Analyst object
# ══════════════════════════════════════════════════════════
class TestBriefer(unittest.TestCase):
    def setUp(self):
        self.br = ab.MarketBriefer()
        self.br.enabled = True
        self.br.interval = 30.0

    def test_cadence_new_bar_or_open_trade_and_never_faster_than_interval(self):
        self.assertTrue(self.br.due("XAUUSD", 100), "first brief: due at once")
        self.br.mark_submitted("XAUUSD", 100)
        self.assertFalse(self.br.due("XAUUSD", 101), "interval not spent")
        self.br._last_submit["XAUUSD"] = (time.monotonic() - 31, 100)
        self.assertFalse(self.br.due("XAUUSD", 100), "same bar, no trade: idle market")
        self.assertTrue(self.br.due("XAUUSD", 101), "new bar")
        self.assertTrue(self.br.due("XAUUSD", 100, has_positions=True),
                        "open trade: keep watching its thesis")
        self.br.enabled = False
        self.assertFalse(self.br.due("XAUUSD", 101))

    def test_an_older_answer_arriving_late_is_ignored(self):
        self.assertTrue(self.br.accept(_brief(bias="BUY", seq=2)))
        self.assertFalse(self.br.accept(_brief(bias="SELL", seq=1)))
        self.assertEqual(self.br.latest("XAUUSD").bias, "BUY")

    def test_analyze_returns_a_brief_with_the_meta(self):
        self.br._client = _FakeClient(_GOOD)
        out = self.br.analyze({"symbol": "XAUUSD"},
                              {"symbol": "XAUUSD", "price": 2001.5, "atr": 2.0,
                               "bar": 7, "seq": 3})
        self.assertEqual((out.bias, out.seq, out.price, out.bar), ("BUY", 3, 2001.5, 7))

    def test_the_real_http_client_round_trip(self):
        """No fake client: the actual OpenAI-compatible client against a local
        stand-in provider, so prompt serialisation and envelope parsing are
        exercised for real (what the model is sent, and how its reply is read)."""
        import http.server
        import threading
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen["body"] = body
                reply = {"id": "x", "model": "stand-in", "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant",
                                "content": json.dumps({**_GOOD, "bias": "SELL",
                                                       "confidence": 0.7})}}]}
                raw = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with patch.object(config, "AI_BASE_URL", f"http://127.0.0.1:{srv.server_port}"), \
                patch.object(config, "AI_API_KEYS", ["k-test"]):
            self.br._client = None
            df = _df(n=80)
            ctx = self.br.build_context("XAUUSD", df, h1_trend="SELL",
                                        bot=ab.bot_view(df, "SELL"))
            out = self.br.analyze(ctx, {"symbol": "XAUUSD", "price": 2000.0,
                                        "atr": 1.0, "bar": 5, "seq": 1})
        self.assertEqual((out.bias, out.confidence, out.model), ("SELL", 0.7, "stand-in"))
        sent = seen["body"]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user"])
        payload = json.loads(sent[1]["content"].split("SNAPSHOT_JSON:\n")[1]
                             .split("\nSchema:")[0])
        self.assertEqual(payload["symbol"], "XAUUSD")
        self.assertIn("bot_view", payload)
        self.assertEqual(seen["body"]["temperature"], 0)

    def test_a_bad_reply_raises_and_does_not_open_the_circuit(self):
        self.br._client = _FakeClient({**_GOOD, "bias": "MOON"})
        self.br.circuit = ab._Circuit(1, 60)
        for _ in range(3):
            with self.assertRaises(AIInvalidResponse):
                self.br.analyze({}, {"symbol": "XAUUSD"})
        self.assertTrue(self.br.circuit.allow(),
                        "a contract violation is not an outage")

    def test_outages_open_the_circuit_then_fail_fast(self):
        fake = _FakeClient(exc=AIUnavailable("network_or_timeout"))
        self.br._client = fake
        self.br.circuit = ab._Circuit(2, 60)
        for _ in range(2):
            with self.assertRaises(AIUnavailable):
                self.br.analyze({}, {"symbol": "XAUUSD"})
        calls = fake.calls
        with self.assertRaises(AIUnavailable):
            self.br.analyze({}, {"symbol": "XAUUSD"})
        self.assertEqual(fake.calls, calls, "open circuit: no more provider calls")

    def test_context_is_bounded_and_carries_the_bot_view(self):
        df = _df(n=200)
        ctx = self.br.build_context("XAUUSD", df, h1_trend="BUY", spread_pct=0.01,
                                    news_titles=["a"] * 9,
                                    bot=ab.bot_view(df, "BUY"),
                                    performance={"recent_real_trades": {"n": 3}})
        self.assertEqual(len(ctx["bars_m1"]), self.br.n_bars)
        self.assertEqual(len(ctx["bars_m1"][0]), 4)
        self.assertEqual(ctx["bot_view"]["lean"], "BUY")
        self.assertLessEqual(len(ctx["recent_news"]), 5)
        self.assertLess(len(json.dumps(ctx)), 4000, "keep the prompt small and fast")
        blob = json.dumps(ctx).lower()
        for needle in ("password", "token", "api_key", "login"):
            self.assertNotIn(needle, blob)


# ══════════════════════════════════════════════════════════
#   6. the Scalp Guard
# ══════════════════════════════════════════════════════════
class TestScalpGuard(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k) for k in dir(config)
                       if k.startswith("SCALP_")}
        config.SCALP_TARGET_USD = 3.0
        config.SCALP_GUARD_MIN_AGE_SECONDS = 3.0
        config.SCALP_STALL_SECONDS = 75.0
        config.SCALP_STALL_MIN_PEAK_PCT = 0.25
        config.SCALP_GIVEBACK_ARM_PCT = 0.50
        config.SCALP_GIVEBACK_PCT = 0.40
        config.SCALP_EARLY_CUT_PCT = 0.70
        config.SCALP_EARLY_CUT_MIN_SECONDS = 20.0
        config.SCALP_MAX_HOLD_SECONDS = 240.0
        self.now = 1_000_000.0

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def ps(self, age=30.0, **kw):
        p = {"opened_ts": self.now - age, "target_usd": 3.0, "sl_usd": 3.0}
        p.update(kw)
        return p

    def ev(self, ps, floating):
        return sg.evaluate(ps, floating, now=self.now)

    def test_bank_at_the_dollar_target(self):
        d = self.ev(self.ps(), 3.05)
        self.assertEqual((d.action, d.code), ("CLOSE", "SCALP_BANK"))

    def test_nothing_happens_in_the_first_seconds(self):
        self.assertEqual(self.ev(self.ps(age=1.0), 3.5).action, "HOLD",
                         "spread noise in the first 3s must not trigger anything")

    def test_giveback_banks_what_is_left(self):
        p = self.ps()
        self.assertEqual(self.ev(p, 2.0).action, "HOLD")      # peak 2.0
        self.assertEqual(self.ev(p, 1.5).action, "HOLD")      # kept 75%
        d = self.ev(p, 1.1)                                   # kept 55% < 60%
        self.assertEqual((d.action, d.code), ("CLOSE", "SCALP_GIVEBACK"))

    def test_a_small_peak_is_not_a_giveback(self):
        p = self.ps(age=10)
        self.ev(p, 1.0)                                       # peak < 50% of target
        self.assertEqual(self.ev(p, 0.2).action, "HOLD")

    def test_stall_closes_a_trade_that_never_worked(self):
        d = self.ev(self.ps(age=80), 0.2)
        self.assertEqual((d.action, d.code), ("CLOSE", "SCALP_STALL"))
        worked = self.ps(age=80, peak_usd=1.2)                # reached 40% once
        self.assertEqual(self.ev(worked, 0.9).action, "HOLD",
                         "it did work for a moment: the stall rule stands down")

    def test_early_cut_saves_part_of_the_loss(self):
        d = self.ev(self.ps(age=25), -2.2)                    # 73% of a $3 stop
        self.assertEqual((d.action, d.code), ("CLOSE", "SCALP_EARLY_CUT"))
        self.assertEqual(self.ev(self.ps(age=25), -1.5).action, "HOLD")
        self.assertEqual(self.ev(self.ps(age=10), -2.5).action, "HOLD",
                         "too young: give the entry a chance to breathe")
        no_sl = self.ps(age=25)
        no_sl.pop("sl_usd")
        self.assertEqual(self.ev(no_sl, -2.9).action, "HOLD",
                         "an adopted trade with unknown stop value is never guessed at")

    def test_max_hold_ends_every_scalp(self):
        worked = self.ps(age=250, peak_usd=2.0)
        d = self.ev(worked, 1.9)
        self.assertEqual((d.action, d.code), ("CLOSE", "SCALP_MAX_HOLD"))

    def test_an_adopted_trade_starts_its_clock_when_first_seen(self):
        p = {}
        self.assertEqual(self.ev(p, -5.0).action, "HOLD")
        self.assertEqual(p["opened_ts"], self.now)

    def test_guard_only_ever_closes(self):
        d = self.ev(self.ps(), 0.5)
        self.assertIn(d.action, ("HOLD", "CLOSE"))


# ══════════════════════════════════════════════════════════
#   7. $ sizing
# ══════════════════════════════════════════════════════════
def _gold_info(**kw):
    info = MagicMock()
    info.trade_tick_value = 1.0       # $ per tick on 1 lot
    info.trade_tick_size = 0.01       # => $100 per 1.00 price on 1 lot (gold)
    info.volume_min, info.volume_max, info.volume_step = 0.01, 100.0, 0.01
    info.digits = 2
    for k, v in kw.items():
        setattr(info, k, v)
    return info


class TestUsdSizing(unittest.TestCase):
    def setUp(self):
        mt5.symbol_info.return_value = _gold_info()

    def test_lot_makes_the_distance_worth_the_target(self):
        r = risk_manager.calc_lot_for_usd("XAUUSD", 1.0, 3.0)
        self.assertEqual(r["lot"], 0.03)
        self.assertAlmostEqual(r["actual_usd"], 3.0, places=6)
        r = risk_manager.calc_lot_for_usd("XAUUSD", 2.4, 3.0)
        self.assertEqual(r["lot"], 0.01)             # 0.0125 rounds DOWN, never up
        self.assertAlmostEqual(r["actual_usd"], 2.4, places=6)

    def test_never_sizes_above_the_target(self):
        for dist in (0.7, 1.3, 2.9, 4.1):
            r = risk_manager.calc_lot_for_usd("XAUUSD", dist, 3.0)
            if not r["min_clamped"]:
                self.assertLessEqual(r["actual_usd"], 3.0 + 1e-9, dist)

    def test_broker_minimum_and_cap_are_reported(self):
        r = risk_manager.calc_lot_for_usd("XAUUSD", 1.0, 0.5)
        self.assertTrue(r["min_clamped"])
        self.assertEqual(r["lot"], 0.01)
        r = risk_manager.calc_lot_for_usd("XAUUSD", 0.1, 30.0, max_lot=0.5)
        self.assertTrue(r["max_clamped"])
        self.assertEqual(r["lot"], 0.5)

    def test_missing_broker_data_means_fallback_not_a_guess(self):
        mt5.symbol_info.return_value = None
        self.assertIsNone(risk_manager.calc_lot_for_usd("XAUUSD", 1.0, 3.0))
        mt5.symbol_info.return_value = _gold_info(trade_tick_value=0.0)
        self.assertIsNone(risk_manager.calc_lot_for_usd("XAUUSD", 1.0, 3.0))
        mt5.symbol_info.return_value = _gold_info()
        self.assertIsNone(risk_manager.calc_lot_for_usd("XAUUSD", 0.0, 3.0))


# ══════════════════════════════════════════════════════════
#   8. the flow inside main.py
# ══════════════════════════════════════════════════════════
class FlowCase(unittest.TestCase):
    def setUp(self):
        for s in list(config.SYMBOLS):
            main.symbol_state[s] = main.fresh_state()
        self._saved = {k: getattr(config, k) for k in dir(config)
                       if k.startswith(("SCALP_", "AI_BRIEF_"))}
        self._saved["ENTRY_ENGINE"] = config.ENTRY_ENGINE
        config.AI_BRIEF_FASTLANE = True
        config.AI_BRIEF_FLIP_CLOSE = True
        config.AI_BRIEF_FLIP_CONF = 0.70
        config.AI_BRIEF_FLIP_CONFIRMS = 2
        config.SCALP_USD_MODE = False
        config.SCALP_GUARD_ENABLED = False
        main.ai_position_reviewer = None
        main.ai_regime = None
        main.news_watcher = None
        main.ai_reviewer = None
        main.ai_bus = None
        main.flip_tracker = ab.FlipTracker()
        main.scalp_stats.update(guard_closes=0, flip_closes=0)
        self.briefer = ab.MarketBriefer()
        self.briefer.enabled = True
        main.ai_briefer = self.briefer
        self.bar = {"v": 1_000_000}
        self.acc = MagicMock(balance=1000.0, equity=1000.0, profit=0.0)
        for x in [
            patch.object(main, "get_account_info", return_value=self.acc),
            patch.object(main, "get_spread_percent", return_value=0.02),
            patch.object(main, "get_open_positions", return_value=[]),
            patch.object(main, "place_buy", return_value=(9001, "OK")),
            patch.object(main, "place_sell", return_value=(9002, "OK")),
            patch.object(main, "get_symbol_info",
                         return_value=MagicMock(digit=5, bid=2000.0, ask=2000.2)),
            patch.object(main, "_get_cur_bar",
                         side_effect=lambda *a, **k: self.bar["v"]),
            patch.object(main, "get_candles", return_value=None),
            patch.object(main, "recent_performance", return_value={}),
            patch("core.risk_manager.positions_allowed", return_value=3),
            patch("core.risk_manager.calc_lot_size", return_value=0.01),
            patch("core.risk_manager.can_open_new_trade", return_value=(True, "")),
            patch.object(main, "send", return_value=None),
        ]:
            self.enterContext(x)
        mt5.symbol_info.return_value = _gold_info()
        mt5.symbol_info_tick.return_value = MagicMock(ask=2000.2, bid=2000.0)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        if main.ai_bus is not None:
            main.ai_bus.shutdown()
            main.ai_bus = None
        main.ai_briefer = None
        main.ai_reviewer = None

    def gate(self, verdict=None):
        """A per-signal entry gate that records whether it was consulted."""
        r = MagicMock()
        r.enabled = True
        r.build_context.return_value = {"symbol": "XAUUSD"}
        r.snapshot_key.side_effect = lambda ctx: "SNAP"
        r.peek_cached.return_value = None
        r.review.return_value = verdict or _verdict()
        r.journal = MagicMock()
        main.ai_reviewer = r
        return r

    def orders(self):
        return main.place_buy.call_count + main.place_sell.call_count


class TestEntryConsensus(FlowCase):
    def test_agree_trades_at_once_and_never_calls_the_gate(self):
        gate = self.gate()
        _accepted(self.briefer, bias="BUY", conf=0.8)
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.orders(), 1, "AI and bot agree: trade now")
        gate.review.assert_not_called()
        gate.build_context.assert_not_called()
        self.assertEqual(self.briefer.stats["fastlane"], 1)
        self.assertEqual(self.briefer.stats["agree"], 1)

    def test_agree_with_the_fast_lane_off_still_goes_through_the_gate(self):
        config.AI_BRIEF_FASTLANE = False
        gate = self.gate()
        gate.review.side_effect = lambda ctx: _verdict()
        _accepted(self.briefer, bias="BUY", conf=0.8)
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        gate.review.assert_called_once()
        self.assertEqual(self.orders(), 1)

    def test_conflict_means_the_bot_obeys_the_ai(self):
        gate = self.gate()
        _accepted(self.briefer, bias="SELL", conf=0.9)
        main.execute_trade("XAUUSD", _decision(direction="BUY"), self.bar["v"])
        self.assertEqual(self.orders(), 0)
        gate.review.assert_not_called()
        st = main.state_for("XAUUSD")
        self.assertGreater(st["ai_reject_cooldown_until"], time.time(),
                           "no point re-asking about the same candle")
        self.assertEqual(self.briefer.stats["conflict"], 1)

    def test_conflict_also_holds_when_the_entry_gate_is_off(self):
        _accepted(self.briefer, bias="SELL", conf=0.9)
        main.execute_trade("XAUUSD", _decision(direction="BUY"), self.bar["v"])
        self.assertEqual(self.orders(), 0)

    def test_neutral_brief_hands_over_to_the_gate_and_its_reject_is_final(self):
        gate = self.gate(_verdict("REJECT", 0.9))
        _accepted(self.briefer, bias="NEUTRAL", conf=0.9)
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        gate.review.assert_called_once()
        self.assertEqual(self.orders(), 0, "the gate's REJECT is final")
        self.assertEqual(self.briefer.stats["neutral"], 1)

    def test_snapshot_shows_the_gate_what_the_analyst_said(self):
        gate = self.gate()
        gate.build_context.return_value = {"symbol": "XAUUSD"}
        d = _decision()
        d["ai_brief"] = {"bias": "NEUTRAL", "confidence": 0.9}
        d["ai_consensus"] = ab.NEUTRAL
        ctx = main.build_ai_snapshot("XAUUSD", d, main.state_for("XAUUSD"))
        self.assertEqual(ctx["ai_market_brief"]["bias"], "NEUTRAL")
        self.assertEqual(ctx["brief_consensus"], ab.NEUTRAL)

    def test_no_brief_is_exactly_the_v22_flow(self):
        gate = self.gate()
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        gate.build_context.assert_called()           # went to the per-signal gate
        self.assertEqual(self.briefer.stats["no_brief"], 1)

    def test_a_stale_brief_neither_vetoes_nor_fast_lanes(self):
        gate = self.gate()
        b = _accepted(self.briefer, bias="SELL", conf=0.95)
        b.ts -= config.AI_BRIEF_TTL_SECONDS + 10
        main.execute_trade("XAUUSD", _decision(direction="BUY"), self.bar["v"])
        gate.build_context.assert_called()
        self.assertEqual(self.briefer.stats["no_brief"], 1)

    def test_an_analyst_crash_never_costs_the_entry(self):
        _accepted(self.briefer, bias="BUY", conf=0.8)
        with patch.object(main, "consensus", side_effect=RuntimeError("boom")):
            main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.orders(), 1, "degrades to the bot's own signal")

    def test_an_order_path_error_is_not_retried_through_the_gate(self):
        gate = self.gate()
        _accepted(self.briefer, bias="BUY", conf=0.8)
        with patch.object(main, "_execute_trade_inner",
                          side_effect=RuntimeError("broker boom")):
            with self.assertRaises(RuntimeError):
                main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        gate.review.assert_not_called()
        self.assertEqual(self.orders(), 0)

    def test_sell_signals_work_the_same_way(self):
        _accepted(self.briefer, bias="SELL", conf=0.8)
        main.execute_trade("XAUUSD", _decision(direction="SELL"), self.bar["v"])
        self.assertEqual(main.place_sell.call_count, 1)


class TestUsdEntry(FlowCase):
    def setUp(self):
        super().setUp()
        config.SCALP_USD_MODE = True
        config.SCALP_TARGET_USD = 10.0
        config.SCALP_MAX_LOT = 1.0
        config.SCALP_MAX_LOSS_MULT = 2.0
        config.SCALP_MAX_SPREAD_PCT_OF_TARGET = 35.0
        self.briefer.enabled = False                 # isolate the sizing path
        main.ai_briefer = None

    def lot_of_last_order(self):
        return main.place_buy.call_args[0][1]

    def test_lot_is_sized_from_the_markets_own_tp_distance(self):
        # price 2000.2, SL 1999.0 (1.2 away), TP = +2.4  ->  $240 per lot
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.orders(), 1)
        self.assertEqual(self.lot_of_last_order(), 0.04)          # 10/240 = 0.0416
        ps = main.state_for("XAUUSD")["positions"][-1]
        self.assertAlmostEqual(ps["target_usd"], 9.6, places=2)    # what 0.04 pays
        self.assertAlmostEqual(ps["sl_usd"], 4.8, places=2)        # 1.2 * 100 * 0.04
        self.assertIn("opened_ts", ps)

    def test_a_broker_minimum_that_is_too_big_skips_the_trade(self):
        mt5.symbol_info.return_value = _gold_info(volume_min=1.0, volume_step=1.0)
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.orders(), 0, "a $120 stop is not a $10 scalp")

    def test_a_wide_spread_skips_the_trade(self):
        mt5.symbol_info_tick.return_value = MagicMock(ask=2003.2, bid=2000.0)
        main.execute_trade("XAUUSD", _decision(price=2003.2), self.bar["v"])
        self.assertEqual(self.orders(), 0, "$3.2 spread vs a ~$10 target: no")

    def test_no_broker_data_falls_back_to_the_percent_lot(self):
        mt5.symbol_info.return_value = _gold_info(trade_tick_value=0.0)
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.orders(), 1)
        self.assertEqual(self.lot_of_last_order(), 0.01)

    def test_usd_mode_off_keeps_the_v22_lot(self):
        config.SCALP_USD_MODE = False
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.lot_of_last_order(), 0.01)


class TestScalpWatch(FlowCase):
    def setUp(self):
        super().setUp()
        config.SCALP_GUARD_ENABLED = True
        config.SCALP_TARGET_USD = 3.0
        self.state = main.state_for("XAUUSD")
        self.ps = {"ticket": 5001, "direction": 1, "entry": 2000.0,
                   "opened_ts": time.time() - 30, "target_usd": 3.0, "sl_usd": 3.0}
        self.state["positions"].append(self.ps)
        self.pos = MagicMock(type=0, profit=1.0, ticket=5001)
        for x in [patch.object(main, "get_position_by_ticket",
                               side_effect=lambda t: self.pos),
                  patch.object(main, "close_position",
                               return_value=(True, "OK"))]:
            self.enterContext(x)

    def test_guard_banks_the_target(self):
        self.pos.profit = 3.2
        main.scalp_watch("XAUUSD", self.state)
        main.close_position.assert_called_once()
        self.assertEqual(main.close_position.call_args[0][1], 5001)
        self.assertIn("SCALP_BANK", main.close_position.call_args[0][2])
        self.assertEqual(main.scalp_stats["guard_closes"], 1)

    def test_a_healthy_trade_is_left_alone(self):
        main.scalp_watch("XAUUSD", self.state)
        main.close_position.assert_not_called()

    def test_a_refused_close_is_retried_but_not_hammered(self):
        self.pos.profit = 3.5
        main.close_position.return_value = (False, "requote")
        main.scalp_watch("XAUUSD", self.state)
        main.scalp_watch("XAUUSD", self.state)           # 0.3s later in real life
        self.assertEqual(main.close_position.call_count, 1)
        self.ps["close_try_ts"] -= 3.0
        main.scalp_watch("XAUUSD", self.state)
        self.assertEqual(main.close_position.call_count, 2)
        self.assertEqual(main.scalp_stats["guard_closes"], 0,
                         "a close that did not happen is not counted")

    def test_guard_is_off_for_the_flash_hedge_engine(self):
        config.ENTRY_ENGINE = "FLASH"
        self.pos.profit = 9.0
        main.scalp_watch("XAUUSD", self.state)
        main.close_position.assert_not_called()

    def test_analyst_flip_closes_a_trade_after_two_confident_briefs(self):
        config.SCALP_GUARD_ENABLED = False               # prove the flip alone
        _accepted(self.briefer, bias="SELL", conf=0.85, seq=1)
        main.scalp_watch("XAUUSD", self.state)
        main.close_position.assert_not_called()
        _accepted(self.briefer, bias="SELL", conf=0.85, seq=2)
        main.scalp_watch("XAUUSD", self.state)
        main.close_position.assert_called_once()
        self.assertIn("AI_THESIS_FLIP", main.close_position.call_args[0][2])
        self.assertEqual(main.scalp_stats["flip_closes"], 1)
        self.assertEqual(self.briefer.stats["flip_close"], 1)

    def test_flip_ignores_an_opinion_older_than_the_trade(self):
        config.SCALP_GUARD_ENABLED = False
        for seq in (1, 2, 3):
            b = _accepted(self.briefer, bias="SELL", conf=0.95, seq=seq)
            b.ts = self.ps["opened_ts"] - 10
            main.scalp_watch("XAUUSD", self.state)
        main.close_position.assert_not_called()

    def test_flip_off_means_no_flip_close(self):
        config.SCALP_GUARD_ENABLED = False
        config.AI_BRIEF_FLIP_CLOSE = False
        for seq in (1, 2, 3):
            _accepted(self.briefer, bias="SELL", conf=0.95, seq=seq)
            main.scalp_watch("XAUUSD", self.state)
        main.close_position.assert_not_called()


class TestBriefLoop(FlowCase):
    """refresh -> AI bus -> accept, and every way it can fail."""

    def setUp(self):
        super().setUp()
        main.ai_bus = AIBus(workers=1, queue_max=4, result_ttl=30).start()
        self.state = main.state_for("XAUUSD")
        self.state["feature_cache"] = _df(n=80)
        self.state["last_signal_bar"] = 42

    def wait_for(self, predicate, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            main.collect_briefs()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_a_brief_travels_from_submit_to_the_analyst(self):
        self.briefer._client = _FakeClient(_GOOD)
        main.submit_brief_refreshes("XAUUSD", self.state)
        self.assertTrue(self.wait_for(lambda: self.briefer.latest("XAUUSD")))
        b = self.briefer.latest("XAUUSD")
        self.assertEqual((b.bias, b.bar), ("BUY", 42))
        self.assertGreater(b.price, 0)
        self.assertEqual(self.briefer.stats["ok"], 1)

    def test_it_does_not_resubmit_inside_the_interval(self):
        client = _FakeClient(_GOOD)
        self.briefer._client = client
        main.submit_brief_refreshes("XAUUSD", self.state)
        self.assertTrue(self.wait_for(lambda: client.calls >= 1))
        for _ in range(5):
            main.submit_brief_refreshes("XAUUSD", self.state)
        time.sleep(0.2)
        self.assertEqual(client.calls, 1)

    def test_a_provider_outage_is_a_missing_brief_not_a_crash(self):
        self.briefer._client = _FakeClient(exc=AIUnavailable("network_or_timeout"))
        main.submit_brief_refreshes("XAUUSD", self.state)
        self.assertTrue(self.wait_for(lambda: self.briefer.stats["error"] >= 1))
        self.assertIsNone(self.briefer.latest("XAUUSD"))
        # and the entry flow simply behaves as in v2.2
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.orders(), 1)

    def test_no_features_yet_means_no_call(self):
        client = _FakeClient(_GOOD)
        self.briefer._client = client
        self.state["feature_cache"] = None
        main.submit_brief_refreshes("XAUUSD", self.state)
        time.sleep(0.1)
        self.assertEqual(client.calls, 0)

    def test_the_summary_line_reports_the_new_lanes(self):
        _accepted(self.briefer)
        line = main.ai_flow_summary()
        self.assertIn("analyst:ON", line)
        self.assertIn("guard-closes=", line)


class TestGuardianBriefing(FlowCase):
    def test_the_position_prompt_knows_scalp_mode(self):
        from core import ai_position
        self.assertIn("SCALP MODE", ai_position.SYSTEM_POLICY)
        self.assertIn("scalp_guard_will", ai_position.SYSTEM_POLICY)

    def test_the_position_snapshot_carries_scalp_mode(self):
        config.SCALP_GUARD_ENABLED = True
        config.SCALP_TARGET_USD = 3.0
        r = MagicMock()
        r.due.return_value = True
        r.build_context.return_value = {}
        r.review.return_value = MagicMock(action="HOLD", approved=False,
                                          confidence=0.5, model="fake",
                                          key_number=1, reason_code="T")
        main.ai_position_reviewer = r
        state = main.state_for("XAUUSD")
        state["positions"].append({"ticket": 6001, "direction": 1,
                                   "opened_ts": time.time() - 40,
                                   "target_usd": 3.0, "sl_usd": 3.0,
                                   "peak_usd": 1.4})
        _accepted(self.briefer, bias="BUY", conf=0.7)
        pos = MagicMock(type=0, tp=2002.0, price_open=2000.0, price_current=2000.5,
                        sl=1999.0, profit=1.1, time=time.time() - 40)
        with patch.object(main, "get_position_by_ticket", return_value=pos):
            main.submit_position_reviews("XAUUSD", state)
        ctx = r.review.call_args[0][0]
        sm = ctx["scalp_mode"]
        self.assertEqual(sm["target_usd"], 3.0)
        self.assertEqual(sm["peak_usd"], 1.4)
        self.assertGreaterEqual(sm["seconds_open"], 39)
        self.assertEqual(sm["analyst_brief"]["bias"], "BUY")
        self.assertIn("bank_at_usd", sm["scalp_guard_will"])


class TestPreflightScalperNotes(unittest.TestCase):
    """The scalper profile's own numbers must not be reported as mistakes, and the
    $-mode risk line must say what is really at stake on a small account."""

    def _run(self, fn_name, balance=None, **env):
        import io
        import contextlib
        import preflight
        cfg = _load_config_copy(
            "scalper", AI_PROVIDER="openai_compat", AI_API_KEYS="k",
            AI_ENABLED="true", **env)
        preflight._LIVE.clear()
        if balance is not None:
            preflight._LIVE["balance"] = balance
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            getattr(preflight, fn_name)(cfg)
        return buf.getvalue()

    def test_scalper_defaults_raise_no_tightness_or_looseness_warning(self):
        out = self._run("check_ai", AI_ENTRY_MAX_AGE_SECONDS="10.0")
        self.assertNotIn("much tighter", out)
        out = self._run("check_strategy", MAX_SPREAD_COST_PCT="30.0")
        self.assertNotIn("is loose", out)

    def test_a_really_tight_value_is_still_reported(self):
        out = self._run("check_ai", AI_ENTRY_MAX_AGE_SECONDS="1.0")
        self.assertIn("much tighter", out)

    def test_small_account_is_told_it_runs_one_position(self):
        out = self._run("check_strategy", balance=32.27)
        self.assertIn("only 1 position at a time", out)
        self.assertIn("~9% of the $32.27 balance", out)
        self.assertNotIn("stop-outs end the day", out)

    def test_risky_dollar_target_on_a_mid_account_gets_a_warning(self):
        out = self._run("check_strategy", balance=60.0, SCALP_TARGET_USD="10")
        self.assertIn("~50% of the $60.00 balance", out)
        self.assertIn("stop-outs end the day", out)

    def test_big_account_gets_the_info_line_but_no_warning(self):
        out = self._run("check_strategy", balance=5000.0)
        self.assertIn("% of the $5,000.00 balance", out)
        self.assertNotIn("stop-outs end the day", out)

    def test_brief_ttl_shorter_than_the_interval_is_flagged(self):
        out = self._run("check_strategy", AI_BRIEF_INTERVAL_SECONDS="120",
                        AI_BRIEF_TTL_SECONDS="90")
        self.assertIn("would be stale before the next one arrives", out)
        ok = self._run("check_strategy", AI_BRIEF_INTERVAL_SECONDS="120",
                       AI_BRIEF_TTL_SECONDS="150")
        self.assertNotIn("would be stale", ok)
        default = self._run("check_strategy")
        self.assertNotIn("would be stale", default)

    def test_entry_gate_log_line_matches_the_policy(self):
        import inspect
        src = inspect.getsource(main)
        self.assertIn("on silence: trade the bot's own signal", src)


class TestScanM1(unittest.TestCase):
    def _engine(self, scan_m1):
        from core import mtf_engine
        return mtf_engine, patch.object(mtf_engine.config, "SCALP_SCAN_M1", scan_m1,
                                        create=True)

    def test_timeframes_follow_the_switch(self):
        eng, p = self._engine(True)
        with p:
            self.assertEqual(eng.active_timeframes(), ["M1", "M5", "M15"])
        eng, p = self._engine(False)
        with p:
            self.assertEqual(eng.active_timeframes(), ["M5", "M15"])

    def test_profile_defaults(self):
        self.assertTrue(_load_config_copy("scalper").SCALP_SCAN_M1)
        self.assertFalse(_load_config_copy("balanced").SCALP_SCAN_M1)
        self.assertFalse(_load_config_copy("scalper", SCALP_SCAN_M1="false").SCALP_SCAN_M1)

    def _results(self, m1_signal):
        sig = {"signal": m1_signal, "rsi": 40.0, "pattern_score": 2, "atr": 1.0}
        return {"M1": {"signal": sig, "df_feat": None, "weight": 1},
                "M5": {"signal": None, "df_feat": None, "weight": 2},
                "M15": {"signal": None, "df_feat": None, "weight": 3}}

    def test_an_m1_only_signal_becomes_a_decision_when_m1_is_scanned(self):
        eng, p = self._engine(True)
        with p:
            d = eng._make_decision("XAUUSD", self._results("SELL"))
        self.assertIsNotNone(d)
        self.assertEqual((d["direction"], d["chosen_tf"], d["n_positions"]),
                         ("SELL", "M1", 1))

    def test_the_scan_really_asks_the_connector_for_m1_bars(self):
        eng, p = self._engine(True)
        asked = []

        def conn(sym, tf, count=0):
            asked.append(tf)
            return None
        with p:
            eng.scan_all_timeframes("XAUUSD", conn)
        self.assertEqual(asked, ["M1", "M5", "M15"])
        asked.clear()
        eng, p = self._engine(False)
        with p:
            eng.scan_all_timeframes("XAUUSD", conn)
        self.assertEqual(asked, ["M5", "M15"])


class TestAnalystOwnProvider(unittest.TestCase):
    def test_shares_the_main_endpoint_by_default(self):
        cfg = _load_config_copy("scalper", AI_PROVIDER="openai_compat",
                                AI_BASE_URL="https://main.example/v1", AI_API_KEYS="k1,k2")  # secret-scan: allow
        self.assertEqual(cfg.ai_brief_endpoint(), ("https://main.example/v1", ["k1", "k2"]))
        self.assertTrue(cfg.ai_brief_on())

    def test_own_endpoint_needs_both_url_and_keys(self):
        base = dict(AI_PROVIDER="openai_compat", AI_BASE_URL="https://main.example/v1",
                    AI_API_KEYS="k1")
        both = _load_config_copy("scalper", **base, AI_BRIEF_BASE_URL="https://b.example/v1",
                                 AI_BRIEF_API_KEYS="b1, b2")
        self.assertEqual(both.ai_brief_endpoint(), ("https://b.example/v1", ["b1", "b2"]))
        only_url = _load_config_copy("scalper", **base, AI_BRIEF_BASE_URL="https://b.example/v1")
        self.assertEqual(only_url.ai_brief_endpoint()[0], "https://main.example/v1")

    def test_analyst_can_run_when_only_its_own_keys_exist(self):
        cfg = _load_config_copy("scalper", AI_PROVIDER="openai_compat",
                                AI_BRIEF_BASE_URL="https://b.example/v1",
                                AI_BRIEF_API_KEYS="b1")
        self.assertTrue(cfg.ai_brief_on())
        off = _load_config_copy("scalper", AI_PROVIDER="disabled",
                                AI_BRIEF_BASE_URL="https://b.example/v1",
                                AI_BRIEF_API_KEYS="b1")
        self.assertFalse(off.ai_brief_on())

    def test_the_briefer_client_uses_that_endpoint(self):
        cfg = _load_config_copy("scalper", AI_PROVIDER="openai_compat",
                                AI_BASE_URL="https://main.example/v1", AI_API_KEYS="k1",
                                AI_BRIEF_BASE_URL="https://b.example/v1/",
                                AI_BRIEF_API_KEYS="b1")
        with patch.object(ab.config, "ai_brief_endpoint", cfg.ai_brief_endpoint):
            client = ab.MarketBriefer()._get_client()
        self.assertEqual(client.base_url, "https://b.example/v1")

    def test_extra_keys_are_masked_in_logs(self):
        from core import secret_guard
        cfg = types.SimpleNamespace(MT5_PASSWORD="", TELEGRAM_TOKEN="", AI_API_KEYS=[],
                                    AI_BRIEF_API_KEYS=["brief-key-ZZ9911"])  # secret-scan: allow
        before = set(secret_guard._known_values)
        try:
            secret_guard.install_log_redaction(cfg)
            self.assertNotIn("brief-key-ZZ9911", secret_guard.redact("oops brief-key-ZZ9911"))
        finally:
            secret_guard._known_values.clear()
            secret_guard._known_values.update(before)


class TestNewsFeeds(unittest.TestCase):
    URLS = "https://a.example/rss,https://b.example/rss,https://c.example/rss"

    def _watcher(self):
        from core import news
        with patch.object(news.config, "NEWS_RSS_URL", self.URLS, create=True), \
                patch.object(news.config, "NEWS_ENABLED", True, create=True):
            return news, news.NewsWatcher()

    def test_default_has_several_feeds(self):
        cfg = _load_config_copy("balanced")
        urls = [u for u in cfg.NEWS_RSS_URL.split(",") if u.strip()]
        self.assertGreaterEqual(len(urls), 3)
        self.assertTrue(all(u.startswith("https://") for u in urls))

    def test_falls_through_a_blocked_feed_and_remembers_the_good_one(self):
        news, w = self._watcher()
        calls = []

        def one(url):
            calls.append(url)
            if "a.example" in url:
                raise OSError("sinkholed")
            return [(1.0, "Fed holds rates")]
        with patch.object(w, "_fetch_one", side_effect=one):
            self.assertEqual(w._fetch(), [(1.0, "Fed holds rates")])
            self.assertEqual(calls, ["https://a.example/rss", "https://b.example/rss"])
            calls.clear()
            w._fetch()                       # starts from the feed that worked
        self.assertEqual(calls[0], "https://b.example/rss")

    def test_all_feeds_down_raises_so_the_cache_is_served(self):
        news, w = self._watcher()
        with patch.object(w, "_fetch_one", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                w._fetch()
            self.assertEqual(w.refresh_if_due(), [])      # contained, no crash

    def test_investing_com_date_format_is_read_as_utc(self):
        news, w = self._watcher()
        xml = (b"<rss><channel><item><title>Dollar firms</title>"
               b"<pubDate>2026-10-14 20:04:18</pubDate></item></channel></rss>")

        class _R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s, n=-1): return xml
        with patch.object(news.urllib.request, "urlopen", return_value=_R()):
            (ts, title), = w._fetch_one("https://a.example/rss")
        import datetime as _dt
        self.assertEqual(_dt.datetime.fromtimestamp(ts, _dt.timezone.utc),
                         _dt.datetime(2026, 10, 14, 20, 4, 18, tzinfo=_dt.timezone.utc))


class TestFreshStart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fresh_")
        self.old = os.getcwd()
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import fresh_start
        self.fs = fresh_start
        os.chdir(self.tmp)                    # fresh_start chdir'd to ROOT at import
        data = os.path.join("D", "data")
        os.makedirs(data)
        os.makedirs(os.path.join("D", "models"))
        os.makedirs("logs")
        self.files = {
            "logs/bot.log": "x", "bot_forever_log.txt": "x",
            "D/data/risk_state.json": "{}", "D/data/ai_directives.json": "{}",
            "D/data/ai_decisions.csv": "a", "D/data/news_seen.json": "{}",
            "D/models/smc_model.pkl": "m", "D/models/smc_scaler.pkl": "s",
            "D/data/patterns.json": "{}", "D/data/trade_log.csv": "t"}
        for k, v in self.files.items():
            with open(k, "w") as f:
                f.write(v)
        old_time = time.time() - 600
        os.utime("logs/bot.log", (old_time, old_time))
        c = fresh_start.config
        self.patches = [patch.object(c, n, v, create=True) for n, v in {
            "DATA_ROOT_PATH": "D", "LOG_PATH": "logs/bot.log",
            "MODEL_PATH": "D/models/smc_model.pkl", "SCALER_PATH": "D/models/smc_scaler.pkl",
            "PATTERN_DATA_PATH": "D/data/patterns.json",
            "TRADE_LOG_PATH": "D/data/trade_log.csv",
            "AI_JOURNAL_PATH": "D/data/ai_decisions.csv",
            "NEWS_SEEN_PATH": "D/data/news_seen.json",
            "AI_REGIME_STATE_PATH": "D/data/ai_directives.json"}.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *argv):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.fs.main(list(argv))
        return rc, buf.getvalue()

    def test_dry_run_changes_nothing(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        for k in self.files:
            self.assertTrue(os.path.exists(k), k)
        self.assertFalse(os.path.exists("backups"))

    def test_yes_backs_up_then_resets_runtime_data_but_keeps_the_model(self):
        rc, out = self._run("--yes")
        self.assertEqual(rc, 0, out)
        for gone in ("logs/bot.log", "bot_forever_log.txt", "D/data/risk_state.json",
                     "D/data/ai_directives.json", "D/data/ai_decisions.csv",
                     "D/data/news_seen.json"):
            self.assertFalse(os.path.exists(gone), gone)
        for kept in ("D/models/smc_model.pkl", "D/models/smc_scaler.pkl",
                     "D/data/patterns.json", "D/data/trade_log.csv"):
            self.assertTrue(os.path.exists(kept), kept)
        (bk,) = os.listdir("backups")
        self.assertTrue(os.path.exists(os.path.join("backups", bk, "D/data/risk_state.json")))

    def test_all_also_resets_model_and_history(self):
        rc, _ = self._run("--yes", "--all")
        self.assertEqual(rc, 0)
        for k in self.files:
            self.assertFalse(os.path.exists(k), k)

    def test_refuses_while_the_bot_is_writing_its_log(self):
        os.utime("logs/bot.log", None)                # "just written"
        rc, out = self._run("--yes")
        self.assertEqual(rc, 2)
        self.assertIn("RUNNING", out)
        self.assertTrue(os.path.exists("D/data/risk_state.json"))
        rc, _ = self._run("--yes", "--force")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
