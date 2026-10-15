# ============================================================
#   verify_scalper_flow.py
#   Offline tests for the v2.2 scalper AI lane (no network, no real broker).
#
#   What this proves:
#     1. BOT_PROFILE=scalper turns on the fast settings AND the AI latency
#        policy; balanced / ultra_fast keep the v2.1 fail-closed contract
#     2. a SLOW AI cannot cost the entry: after the budget the bot trades its
#        own signal — once — and the late verdict is journaled as unused
#     3. a REAL REJECT inside the budget is still final (AI keeps its veto)
#     4. "the AI could not answer" (provider down, open circuit, raised job,
#        full queue) is NOT a reject: the bot trades its own signal
#     5. proceed-on-silence still obeys freshness: a candidate whose bar rolled
#        over is dropped, never traded late
#     6. block mode (the default profiles) is unchanged: no verdict = no trade
#     7. Telegram alerts never block the trading loop
#
#   Run:  python3 verify_scalper_flow.py
# ============================================================

import asyncio
import importlib.util
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

sys.modules['MetaTrader5'] = MagicMock()
import MetaTrader5 as mt5          # noqa: E402  (the mock, deliberately)
mt5.ORDER_TYPE_BUY = 0
mt5.ORDER_TYPE_SELL = 1
import pandas as pd                # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="bot_scalper_test_")

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
from core.ai_bus import AIBus        # noqa: E402
from core.ai_review import AIReviewResult   # noqa: E402


# ── helpers ─────────────────────────────────────────────────
_FAKE_TG_TOKEN = "fake-token-for-test"   # secret-scan: allow  (test fixture, not real)


def _verdict(action="APPROVE", conf=0.9, decision_id=1, source="ai",
             approved=None, code="TEST"):
    if approved is None:
        approved = action == "APPROVE" and conf >= 0.6
    return AIReviewResult(approved=approved, action=action, confidence=conf,
                          reason_code=code, reason="test verdict", model="fake",
                          key_number=1, decision_id=decision_id, source=source)


def _unavailable(decision_id=7):
    return _verdict(action="PROVIDER_UNAVAILABLE", conf=0.0,
                    decision_id=decision_id, source="ai_unavailable",
                    approved=False, code="PROVIDER_UNAVAILABLE")


def _decision(price=2000.0, atr=2.0):
    signal = {"atr": atr, "atr_pct": 0.1, "rsi": 55.0, "close": price,
              "body_pct": 0.05, "close_vs_ema": 0.02, "hour_utc": 10,
              "bull_sweep": False, "bear_sweep": False, "bull_bos": True,
              "bear_bos": False, "signal": "BUY",
              "last5_candles": [], "pattern_score": 2,
              "dist_to_high_atr": 1.0, "dist_to_low_atr": 1.0,
              "bar_time": pd.Timestamp("2026-01-01 10:00")}
    return {"engine": "SR_H1", "direction": "BUY", "n_positions": 1,
            "chosen_tf": "H1", "signal": signal, "sl_price": price - atr * 0.5,
            "sl_atr": atr, "rr": 2.0, "score": 1, "summary": "test"}


def _load_config_copy(profile, **env):
    """A hermetic fresh copy of config.py under a different profile: no .env,
    no inherited environment."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    spec = importlib.util.spec_from_file_location(
        "config_probe", os.path.join(ROOT, "config.py"))
    mod = importlib.util.module_from_spec(spec)
    envv = {"BOT_PROFILE": profile, **{k: str(v) for k, v in env.items()}}
    with patch.dict(os.environ, envv, clear=True), \
            patch.dict(sys.modules, {"dotenv": fake_dotenv}):
        spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════
#   1. profiles
# ══════════════════════════════════════════════════════════
class TestScalperProfile(unittest.TestCase):
    def test_scalper_is_fast_and_proceeds_on_silence(self):
        c = _load_config_copy("scalper")
        self.assertTrue(c.SCALPER)
        self.assertTrue(c.ULTRA_FAST, "scalper inherits every ultra_fast switch")
        self.assertEqual(c.ENTRY_ENGINE, "SMC_MTF")
        self.assertEqual(c.ANALYSIS_INTERVAL_SECONDS, 0.3)
        self.assertEqual(c.AI_TIMEOUT_SECONDS, 4.0)
        self.assertEqual(c.AI_MAX_RETRIES, 0)
        self.assertEqual(c.AI_POSITION_INTERVAL, 15.0)
        self.assertEqual(c.AI_POSITION_VOTES, 1)
        self.assertEqual(c.AI_ENTRY_TIMEOUT_MODE, "proceed")
        self.assertTrue(c.ai_entry_proceeds_on_timeout())
        self.assertEqual(c.ai_entry_budget_seconds(), 3.0)
        self.assertLess(c.AI_ENTRY_MAX_AGE_SECONDS, 45.0)
        self.assertLess(c.AI_ENTRY_MAX_DRIFT_ATR, 0.5)

    def test_other_profiles_keep_the_fail_closed_contract(self):
        for profile in ("balanced", "ultra_fast"):
            c = _load_config_copy(profile)
            self.assertFalse(c.SCALPER, profile)
            self.assertEqual(c.AI_ENTRY_TIMEOUT_MODE, "block", profile)
            self.assertFalse(c.ai_entry_proceeds_on_timeout(), profile)
            self.assertEqual(c.ai_entry_budget_seconds(), 0.0, profile)
            self.assertEqual(c.AI_TIMEOUT_SECONDS, 8.0, profile)
            self.assertEqual(c.AI_POSITION_VOTES, 2, profile)
        self.assertEqual(_load_config_copy("ultra_fast")
                         .ANALYSIS_INTERVAL_SECONDS, 0.5)

    def test_budget_can_never_exceed_the_freshness_window(self):
        """A budget >= the max age would drop every late candidate as STALE
        before the timeout policy could act."""
        c = _load_config_copy("scalper", AI_ENTRY_BUDGET_SECONDS=30,
                              AI_ENTRY_MAX_AGE_SECONDS=10)
        self.assertLess(c.ai_entry_budget_seconds(), 10.0)

    def test_env_can_turn_the_policy_on_for_any_profile(self):
        c = _load_config_copy("balanced", AI_ENTRY_TIMEOUT_MODE="proceed")
        self.assertTrue(c.ai_entry_proceeds_on_timeout())
        self.assertEqual(c.ai_entry_budget_seconds(), 3.0)   # 0 -> 3s fallback
        bad = _load_config_copy("scalper", AI_ENTRY_TIMEOUT_MODE="garbage")
        self.assertEqual(bad.AI_ENTRY_TIMEOUT_MODE, "block")


# ══════════════════════════════════════════════════════════
#   2. the entry policy
# ══════════════════════════════════════════════════════════
class PolicyCase(unittest.TestCase):
    MODE = "proceed"

    def setUp(self):
        for s in list(config.SYMBOLS):
            main.symbol_state[s] = main.fresh_state()
        for k in main.ai_entry_stats:
            main.ai_entry_stats[k] = 0
        self._saved = {k: getattr(config, k) for k in (
            "AI_ENTRY_TIMEOUT_MODE", "AI_ENTRY_BUDGET_SECONDS",
            "AI_ENTRY_MAX_AGE_SECONDS", "AI_ENTRY_MAX_DRIFT_ATR")}
        config.AI_ENTRY_TIMEOUT_MODE = self.MODE
        config.AI_ENTRY_BUDGET_SECONDS = 0.3
        config.AI_ENTRY_MAX_AGE_SECONDS = 10.0
        config.AI_ENTRY_MAX_DRIFT_ATR = 0.5
        main.ai_position_reviewer = None
        main.ai_regime = None
        main.news_watcher = None
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
            patch("core.risk_manager.positions_allowed", return_value=3),
            patch("core.risk_manager.calc_lot_size", return_value=0.01),
            patch("core.risk_manager.can_open_new_trade", return_value=(True, "")),
            patch.object(main, "send", return_value=None),
        ]:
            self.enterContext(x)
        mt5.symbol_info.return_value.digits = 2
        mt5.symbol_info_tick.return_value = MagicMock(ask=2000.2, bid=2000.0)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        if main.ai_bus is not None:
            main.ai_bus.shutdown()
            main.ai_bus = None
        main.ai_reviewer = None

    # -- scaffolding --------------------------------------------------
    def wire(self, verdict_fn, queue_max=4):
        r = MagicMock()
        r.enabled = True
        r.build_context.return_value = {"symbol": "XAUUSD"}
        r.snapshot_key.side_effect = lambda ctx: "SNAP"
        r.peek_cached.return_value = None
        r.review.side_effect = verdict_fn
        r.journal = MagicMock()
        main.ai_reviewer = r
        main.ai_bus = AIBus(workers=1, queue_max=queue_max, result_ttl=30).start()
        return r

    def age(self, seconds):
        for c in main.state_for("XAUUSD")['pending_ai'].values():
            c['ts'] -= seconds

    def sweep(self):
        main.sweep_pending_entries("XAUUSD", main.state_for("XAUUSD"))

    def drain(self, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            main.collect_entry_verdicts()
            if not main.state_for("XAUUSD")['pending_ai']:
                return True
            time.sleep(0.01)
        return False

    def orders(self):
        return main.place_buy.call_count


class TestProceedOnSilence(PolicyCase):
    MODE = "proceed"

    def test_slow_ai_cannot_cost_the_entry(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        r = self.wire(lambda ctx: (gate.wait(5), _verdict())[1])
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.orders(), 0, "AI has not answered: nothing yet")
        self.sweep()
        self.assertEqual(self.orders(), 0, "budget not spent yet: still waiting")
        self.age(2.0)                                  # budget (0.3s) is spent
        self.sweep()
        self.assertEqual(self.orders(), 1, "silent AI -> the bot trades its signal")
        self.assertEqual(main.ai_entry_stats['proceeded_timeout'], 1)
        self.sweep()
        self.assertEqual(self.orders(), 1, "and it is a single order, not a retry loop")
        # the AI finally answers — far too late to matter, never a 2nd order
        gate.set()
        calls = []
        end = time.monotonic() + 3.0
        while time.monotonic() < end and 'ai_verdict_late_unused' not in calls:
            main.collect_entry_verdicts()        # the late answer lands here
            calls = [c.kwargs.get('block_reason') for c in
                     r.journal.finalize_pending.call_args_list]
            time.sleep(0.02)
        self.assertEqual(self.orders(), 1, "a late verdict must never trade again")
        self.assertIn('ai_verdict_late_unused', calls,
                      "the journal must say the verdict arrived too late")

    def test_a_real_reject_inside_the_budget_is_final(self):
        self.wire(lambda ctx: _verdict(action="REJECT", conf=0.95))
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertTrue(self.drain())
        self.assertEqual(self.orders(), 0, "the AI keeps its veto when it answers")
        self.sweep()
        self.age(5.0)
        self.sweep()
        self.assertEqual(self.orders(), 0)
        self.assertEqual(sum(main.ai_entry_stats.values()), 0)

    def test_an_approve_inside_the_budget_takes_the_normal_path(self):
        self.wire(lambda ctx: _verdict())
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertTrue(self.drain())
        self.assertEqual(self.orders(), 1)
        self.assertEqual(sum(main.ai_entry_stats.values()), 0,
                         "an answered entry is not a 'bot traded without AI' entry")

    def test_provider_down_is_not_a_reject(self):
        r = self.wire(lambda ctx: _unavailable())
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertTrue(self.drain())
        self.assertEqual(self.orders(), 1, "AI unavailable -> trade the bot's signal")
        self.assertEqual(main.ai_entry_stats['proceeded_ai_error'], 1)
        r.journal.mark_execution.assert_called()
        self.assertEqual(
            r.journal.mark_execution.call_args.kwargs.get('final_action'),
            'ai_unavailable_bot_proceeded')
        self.assertEqual(main.state_for("XAUUSD")['ai_reject_cooldown_until'], 0,
                         "an outage is not a rejection: no cooldown")

    def test_a_job_that_raises_is_not_a_reject(self):
        def boom(ctx):
            raise RuntimeError("provider exploded")
        self.wire(boom)
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertTrue(self.drain())
        self.assertEqual(self.orders(), 1)
        self.assertEqual(main.ai_entry_stats['proceeded_ai_error'], 1)

    def test_a_full_queue_does_not_cost_the_entry(self):
        self.wire(lambda ctx: _verdict())
        with patch.object(main.ai_bus, "submit", return_value=False):
            main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.orders(), 1)
        self.assertEqual(main.ai_entry_stats['proceeded_queue_full'], 1)

    def test_proceeding_without_ai_still_obeys_freshness(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.wire(lambda ctx: (gate.wait(5), _verdict())[1])
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.age(2.0)
        self.bar["v"] += 60                       # the signal bar rolled over
        self.sweep()
        self.assertEqual(self.orders(), 0, "a stale candidate is never traded late")
        self.assertEqual(sum(main.ai_entry_stats.values()), 0)

    def test_price_drift_also_cancels_the_proceed(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.wire(lambda ctx: (gate.wait(5), _verdict())[1])
        main.execute_trade("XAUUSD", _decision(price=2000.0, atr=2.0), self.bar["v"])
        self.age(2.0)
        main.get_symbol_info.return_value = MagicMock(bid=2003.0, ask=2003.2)  # 1.5 ATR
        self.sweep()
        self.assertEqual(self.orders(), 0, "price ran away while the AI was silent")

    def test_status_line_tells_the_truth(self):
        saved = (config.AI_PROVIDER, config.AI_API_KEYS, config.AI_ENABLED)
        config.AI_PROVIDER, config.AI_API_KEYS, config.AI_ENABLED = \
            "openai_compat", ["k"], True
        try:
            lines = " | ".join(config.ai_status_lines())
            self.assertIn("trades its own signal", lines)
        finally:
            config.AI_PROVIDER, config.AI_API_KEYS, config.AI_ENABLED = saved


class TestBlockModeIsUnchanged(PolicyCase):
    MODE = "block"

    def test_no_verdict_no_trade(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.wire(lambda ctx: (gate.wait(5), _verdict())[1])
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.age(5.0)                    # older than the proceed budget
        self.sweep()
        self.assertEqual(self.orders(), 0, "block mode never trades on silence")
        self.assertEqual(len(main.state_for("XAUUSD")['pending_ai']), 1,
                         "and still waits until the max-age window")
        self.age(50.0)
        self.sweep()
        self.assertEqual(self.orders(), 0)
        self.assertEqual(len(main.state_for("XAUUSD")['pending_ai']), 0)

    def test_provider_down_still_blocks(self):
        self.wire(lambda ctx: _unavailable())
        main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertTrue(self.drain())
        self.assertEqual(self.orders(), 0, "fail-closed contract")

    def test_full_queue_still_blocks(self):
        self.wire(lambda ctx: _verdict())
        with patch.object(main.ai_bus, "submit", return_value=False):
            main.execute_trade("XAUUSD", _decision(), self.bar["v"])
        self.assertEqual(self.orders(), 0)


# ══════════════════════════════════════════════════════════
#   3. Telegram never blocks the loop
# ══════════════════════════════════════════════════════════
class TestTelegramNeverBlocks(unittest.TestCase):
    def test_send_returns_instantly_even_when_telegram_hangs(self):
        import tg_alerts.alerts as alerts
        started = threading.Event()

        async def hanging(message):
            started.set()
            await asyncio.sleep(1.0)

        with patch.object(alerts, "TELEGRAM_OK", True), \
                patch.object(alerts.config, "TELEGRAM_ENABLED", True), \
                patch.object(alerts.config, "TELEGRAM_TOKEN", _FAKE_TG_TOKEN), \
                patch.object(alerts, "_send_async", hanging):
            t0 = time.monotonic()
            for i in range(250):                   # more than the queue holds
                alerts.send(f"alert {i}")
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.25,
                            f"250 alerts took {elapsed:.2f}s — the loop would freeze")
            self.assertTrue(started.wait(2.0), "the worker thread must deliver")
            self.assertLessEqual(alerts._alert_q.qsize(), 200,
                                 "the queue must stay bounded")
            while not alerts._alert_q.empty():     # keep the real sender out of it
                try:
                    alerts._alert_q.get_nowait()
                except Exception:
                    break
            time.sleep(1.2)                        # let the in-flight one finish


if __name__ == "__main__":
    unittest.main(verbosity=2)
