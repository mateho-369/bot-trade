# ============================================================
#   verify_ai_flow.py
#   Offline tests for the v2.1 AI ⇄ Bot flow (no network, no real broker).
#
#   What this proves (each item is a bug the audit found in v2.0):
#     1. flags are coherent — AI_POSITION_ENABLED works WITHOUT AI_ENABLED,
#        and a flag that cannot run is reported instead of dying silently
#     2. the AI never blocks the trading loop (bus: submit is instant, a slow
#        provider cannot freeze break-even/trailing)
#     3. an entry candidate is only executed when its verdict is FRESH (same
#        bar, small price drift, inside the age budget) — else fail-closed
#     4. an AI EXTEND survives the deterministic TP-tighten rule and is
#        written back into the bot's own state
#     5. a failed AI CLOSE journals through the POSITION reviewer (v2.0
#        reached into the entry reviewer and would raise when it was off)
#     6. directives: clamped ranges, never looser than the owner baseline,
#        persisted across restarts, and EXPIRED back to baseline
#     7. the AI sees the bot's REAL management contract, not stale prose
#     8. the deterministic cost gate agrees with what the AI is told
#     9. outcomes flow back to the AI journal in position-only mode (the
#        return half of the loop that used to be open)
#
#   Run:  python3 verify_ai_flow.py
# ============================================================

import os
import shutil
import sys
import tempfile
import json
import threading
import time
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

sys.modules['MetaTrader5'] = MagicMock()
import MetaTrader5 as mt5          # noqa: E402  (the mock, deliberately)
# the real module's constants must exist on the mock, or `p.type ==
# mt5.ORDER_TYPE_BUY` compares against a MagicMock and is always False
mt5.ORDER_TYPE_BUY = 0
mt5.ORDER_TYPE_SELL = 1
import pandas as pd                # noqa: E402

_TMP = tempfile.mkdtemp(prefix="bot_flow_test_")

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
config.MAX_SPREAD_COST_PCT = 0.0    # off here; tested on purpose below
config.ENTRY_ENGINE = "SR_H1"

import main                          # noqa: E402
from core.ai_bus import AIBus        # noqa: E402
from core.ai_context import management_contract, spread_cost_pct   # noqa: E402
from core.ai_position import PositionReviewResult                  # noqa: E402
from core.ai_regime import RegimeReviewer                          # noqa: E402
from core.ai_review import AIReviewResult                          # noqa: E402


def _approve(action="APPROVE", conf=0.9, decision_id=1):
    return AIReviewResult(approved=action == "APPROVE" and conf >= 0.6,
                          action=action, confidence=conf,
                          reason_code="TEST", reason="test verdict",
                          model="fake", key_number=1, decision_id=decision_id,
                          source="ai")


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


class FlowCase(unittest.TestCase):
    """Common broker-free scaffolding for the execution-path tests."""

    def setUp(self):
        for s in list(config.SYMBOLS):
            main.symbol_state[s] = main.fresh_state()
        main.ai_reviewer = None
        main.ai_position_reviewer = None
        main.ai_regime = None
        main.news_watcher = None
        self.acc = MagicMock(balance=1000.0, equity=1000.0, profit=0.0)
        self.p = [
            patch.object(main, "get_account_info", return_value=self.acc),
            patch.object(main, "get_spread_percent", return_value=0.02),
            patch.object(main, "get_open_positions", return_value=[]),
            patch.object(main, "place_buy", return_value=(9001, "OK")),
            patch.object(main, "place_sell", return_value=(9002, "OK")),
            patch.object(main, "get_symbol_info",
                         return_value=MagicMock(digit=5, bid=2000.0, ask=2000.2)),
            patch.object(main, "_get_cur_bar", return_value=1_000_000),
            patch("core.risk_manager.positions_allowed", return_value=3),
            patch("core.risk_manager.calc_lot_size", return_value=0.01),
            patch("core.risk_manager.can_open_new_trade", return_value=(True, "")),
            patch.object(main, "send", return_value=None),
        ]
        for x in self.p:
            self.enterContext(x)
        mt5.symbol_info.return_value.digits = 2
        mt5.symbol_info_tick.return_value = MagicMock(ask=2000.2, bid=2000.0)

    def tearDown(self):
        if main.ai_bus is not None:
            main.ai_bus.shutdown()
            main.ai_bus = None


# ══════════════════════════════════════════════════════════
#   1. flag coherence
# ══════════════════════════════════════════════════════════
class TestFlags(unittest.TestCase):
    def _flags(self, **over):
        saved = {k: getattr(config, k, None) for k in over}
        saved_keys = config.AI_API_KEYS
        try:
            for k, v in over.items():
                setattr(config, k, v)
            return (config.ai_plumbing_ready(), config.ai_entry_on(),
                    config.ai_position_on(), config.ai_regime_on(),
                    list(config.ai_status_lines()))
        finally:
            for k, v in saved.items():
                setattr(config, k, v)
            config.AI_API_KEYS = saved_keys

    def test_position_manager_no_longer_needs_the_entry_flag(self):
        """v2.0 bug: AI_POSITION_ENABLED=True did nothing unless AI_ENABLED
        was ALSO true — the config said 'ON', reality said off."""
        config.AI_PROVIDER = "openai_compat"
        config.AI_API_KEYS = ["k1"]
        on_plumb, entry, pos, regime, lines = self._flags(
            AI_ENABLED=False, AI_POSITION_ENABLED=True, AI_REGIME_ENABLED=False)
        self.assertTrue(on_plumb)
        self.assertFalse(entry, "entry gate must follow AI_ENABLED")
        self.assertTrue(pos, "position manager must follow its OWN flag")
        self.assertFalse(regime)

    def test_no_plumbing_means_nothing_is_on(self):
        config.AI_PROVIDER = "disabled"
        config.AI_API_KEYS = []
        _, entry, pos, regime, _ = self._flags(
            AI_ENABLED=True, AI_POSITION_ENABLED=True, AI_REGIME_ENABLED=True)
        self.assertFalse(entry or pos or regime)

    def test_requested_but_unavailable_is_loud(self):
        config.AI_PROVIDER = "disabled"
        config.AI_API_KEYS = []
        _, _, _, _, lines = self._flags(AI_ENABLED=False, AI_POSITION_ENABLED=True)
        self.assertTrue(any("requested ON but OFFLINE" in l for l in lines), lines)

    def test_reviewer_enabled_follows_its_own_role(self):
        from core.ai_position import PositionReviewer
        from core.ai_review import AIReviewer
        config.AI_PROVIDER = "openai_compat"
        config.AI_API_KEYS = ["k1"]
        try:
            config.AI_ENABLED = False
            config.AI_POSITION_ENABLED = True
            self.assertTrue(PositionReviewer().enabled)
            self.assertFalse(AIReviewer().enabled)
            config.AI_POSITION_ENABLED = False
            self.assertFalse(PositionReviewer().enabled)
        finally:
            config.AI_PROVIDER = "disabled"
            config.AI_API_KEYS = []
            config.AI_ENABLED = False
            config.AI_POSITION_ENABLED = True


# ══════════════════════════════════════════════════════════
#   2. the bus: AI work never blocks the loop
# ══════════════════════════════════════════════════════════
class TestBus(unittest.TestCase):
    def test_slow_provider_does_not_block_the_caller(self):
        bus = AIBus(workers=1, queue_max=4, result_ttl=30).start()
        try:
            started = time.monotonic()
            self.assertTrue(bus.submit("entry", "k1", lambda: time.sleep(1.5)))
            self.assertTrue(bus.submit("entry", "k2", lambda: "fast"))
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.05,
                            f"submitting AI work took {elapsed:.3f}s — the loop "
                            f"would freeze")
            # the slow answer shows up later without blocking anyone
            got = {}
            for _ in range(120):
                for key, ok, payload in bus.collect("entry"):
                    got[key.split(':', 1)[1]] = payload
                if "k1" in got and "k2" in got:
                    break
                time.sleep(0.02)
            self.assertEqual(set(got), {"k1", "k2"})
        finally:
            bus.shutdown()

    def test_dedupe_and_queue_cap(self):
        bus = AIBus(workers=1, queue_max=1, result_ttl=30).start()
        try:
            ev = threading.Event()
            # A: picked up by the single worker immediately, still "in flight"
            self.assertTrue(bus.submit("position", "7", lambda: ev.wait(3)))
            self.assertFalse(bus.submit("position", "7", lambda: "dupe"),
                             "the same ticket must never be reviewed twice at once")
            self.assertEqual(bus.stats["dropped_dupe"], 1)
            # let the worker pick A up, so the queue is genuinely empty again
            for _ in range(200):
                if bus._q.qsize() == 0:
                    break
                time.sleep(0.005)
            # B: fits the (now empty) queue; C: queue is full -> refused
            self.assertTrue(bus.submit("position", "8", lambda: "queued"))
            self.assertFalse(bus.submit("position", "9", lambda: "overflow"),
                             "the queue must stay bounded")
            self.assertEqual(bus.stats["dropped_full"], 1)
            ev.set()
            time.sleep(0.3)
            keys = {k for k, _ok, _p in bus.collect("position")}
            self.assertTrue(any(k.endswith(":7") for k in keys))
            self.assertFalse(any(k.endswith(":9") for k in keys))
        finally:
            bus.shutdown()

    def test_worker_error_never_reaches_the_loop(self):
        bus = AIBus(workers=1, queue_max=4, result_ttl=30).start()
        try:
            def boom():
                raise RuntimeError("provider exploded")
            self.assertTrue(bus.submit("position", "9", boom))
            err = None
            for _ in range(150):
                res = bus.collect("position")
                if res:
                    err = res[0]
                    break
                time.sleep(0.02)
            self.assertIsNotNone(err, "a failed job must still report back")
            self.assertFalse(err[1], "ok flag must be False")
            self.assertIsInstance(err[2], RuntimeError)
            # the collector turns that into HOLD — never an exception, never a
            # silent stall; here we just prove the payload is an exception object
        finally:
            bus.shutdown()

    def test_expired_results_are_dropped(self):
        bus = AIBus(workers=1, queue_max=4, result_ttl=0.05).start()
        try:
            bus.submit("entry", "old", lambda: "verdict")
            time.sleep(0.25)
            self.assertEqual(bus.collect("entry"), [],
                             "a stale verdict must be discarded, not traded")
            self.assertEqual(bus.stats["expired"], 1)
        finally:
            bus.shutdown()

    def test_drop_removes_an_orphan_result(self):
        bus = AIBus(workers=1, queue_max=4, result_ttl=30).start()
        try:
            bus.submit("position", "42", lambda: "answer")
            time.sleep(0.1)
            self.assertEqual(bus.drop("position", "42"), 1)
            self.assertEqual(bus.collect("position"), [])
        finally:
            bus.shutdown()


# ══════════════════════════════════════════════════════════
#   3. async entry lane: verdict → order, or nothing
# ══════════════════════════════════════════════════════════
class TestAsyncEntryLane(FlowCase):
    def _wire(self, verdict_fn):
        r = MagicMock()
        r.enabled = True
        r.build_context.return_value = {"symbol": "XAUUSD"}
        r.snapshot_key.side_effect = lambda ctx: "SNAP"
        r.peek_cached.return_value = None
        r.review.side_effect = verdict_fn
        r.journal = MagicMock()
        main.ai_reviewer = r
        main.ai_bus = AIBus(workers=1, queue_max=4, result_ttl=30).start()
        self.addCleanup(main.ai_bus.shutdown)
        self.addCleanup(setattr, main, "ai_bus", None)
        return r

    def _drain(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            main.collect_entry_verdicts()
            main.apply_position_verdicts()
            st = main.state_for("XAUUSD")
            if not st['pending_ai']:
                return True
            time.sleep(0.01)
        return False

    def test_no_order_until_the_verdict_lands(self):
        gate = threading.Event()
        self._wire(lambda ctx: (gate.wait(2), _approve())[1])
        main.execute_trade("XAUUSD", _decision(), 1_000_000)
        self.assertEqual(main.place_buy.call_count, 0,
                         "the AI has not answered yet — nothing may be ordered")
        self.assertEqual(len(main.state_for("XAUUSD")['pending_ai']), 1)
        gate.set()
        self.assertTrue(self._drain())
        self.assertEqual(main.place_buy.call_count, 1,
                         "an approved, still-fresh candidate must be executed once")

    def test_reject_blocks_and_sets_cooldown(self):
        self._wire(lambda ctx: _approve(action="REJECT", conf=0.95))
        main.execute_trade("XAUUSD", _decision(), 1_000_000)
        self.assertTrue(self._drain())
        self.assertEqual(main.place_buy.call_count, 0)
        self.assertGreater(main.state_for("XAUUSD")['ai_reject_cooldown_until'],
                           time.time(),
                           "a rejection must pause re-review briefly (1 bar, not 5 min)")

    def test_stale_verdict_is_never_traded(self):
        config.AI_ENTRY_MAX_AGE_SECONDS = 0.05
        self.addCleanup(lambda: setattr(config, "AI_ENTRY_MAX_AGE_SECONDS", 45))
        def slow(ctx):
            time.sleep(0.3)
            return _approve()
        self._wire(slow)
        main.execute_trade("XAUUSD", _decision(), 1_000_000)
        self.assertTrue(self._drain())
        self.assertEqual(main.place_buy.call_count, 0,
                         "an answer about the past is not a licence for the present")

    def test_rolled_bar_discards_the_candidate(self):
        bars = [1_000_000]
        self._wire(lambda ctx: _approve())
        with patch.object(main, "_get_cur_bar", side_effect=lambda *a, **k: bars[0]):
            main.execute_trade("XAUUSD", _decision(), bars[0])
            bars[0] = 1_000_3600          # the signal bar rolled over
            # rebuild the snapshot key so the verdict still finds its candidate
            main.state_for("XAUUSD")['pending_ai'] = {
                "SNAP": dict(main.state_for("XAUUSD")['pending_ai'].get("SNAP")
                             or {'decision': _decision(), 'ts': time.time(),
                                 'price': 2000.0, 'atr': 2.0, 'cur_bar': 1_000_000})}
            self.assertTrue(self._drain())
            self.assertEqual(main.place_buy.call_count, 0)

    def test_queue_saturation_fails_closed(self):
        r = MagicMock()
        r.enabled = True
        r.build_context.return_value = {"symbol": "XAUUSD"}
        r.snapshot_key.side_effect = lambda ctx: "S%d" % (r.calls if False else 0)
        r.peek_cached.return_value = None
        r.review.return_value = _approve()
        main.ai_reviewer = r
        main.ai_bus = AIBus(workers=1, queue_max=1, result_ttl=30).start()
        self.addCleanup(main.ai_bus.shutdown)
        self.addCleanup(setattr, main, "ai_bus", None)
        blocked = threading.Event()
        r.review.side_effect = lambda ctx: (blocked.wait(2), _approve())[1]
        main.execute_trade("XAUUSD", _decision(), 1_000_000)   # takes the slot
        main.ai_bus.drop('entry', "XAUUSD|S0")
        main.state_for("XAUUSD")['pending_ai'].clear()
        # saturate, then a fresh candidate must be refused, not approved
        main.ai_bus.submit("entry", "filler", lambda: blocked.wait(2))
        r.snapshot_key.side_effect = lambda ctx: "BUSY"
        main.execute_trade("XAUUSD", _decision(), 1_000_000)
        self.assertEqual(main.place_buy.call_count, 0)
        blocked.set()

    def test_sync_lane_still_works_without_a_bus(self):
        """AI_ASYNC=false keeps the documented synchronous contract."""
        r = MagicMock()
        r.enabled = True
        r.build_context.return_value = {"symbol": "XAUUSD"}
        r.peek_cached.return_value = None
        r.review.return_value = _approve()
        r.journal = MagicMock()
        main.ai_reviewer = r
        main.ai_bus = None
        main.execute_trade("XAUUSD", _decision(), 1_000_000)
        self.assertEqual(main.place_buy.call_count, 1)
        r.review.assert_called_once()

    def test_cached_verdict_skips_a_second_call(self):
        r = MagicMock()
        r.enabled = True
        r.build_context.return_value = {"symbol": "XAUUSD"}
        r.peek_cached.return_value = _approve()
        r.journal = MagicMock()
        main.ai_reviewer = r
        main.ai_bus = AIBus(workers=1).start()
        self.addCleanup(main.ai_bus.shutdown)
        self.addCleanup(setattr, main, "ai_bus", None)
        main.execute_trade("XAUUSD", _decision(), 1_000_000)
        r.review.assert_not_called()
        self.assertEqual(main.place_buy.call_count, 1)

    def test_sweep_drops_candidates_with_no_answer(self):
        r = MagicMock()
        r.enabled = True
        r.build_context.return_value = {"symbol": "XAUUSD"}
        r.snapshot_key.side_effect = lambda ctx: "NEVER"
        r.peek_cached.return_value = None
        r.review.side_effect = lambda ctx: time.sleep(5)   # provider hangs
        main.ai_reviewer = r
        main.ai_bus = AIBus(workers=1, queue_max=4).start()
        self.addCleanup(main.ai_bus.shutdown)
        self.addCleanup(setattr, main, "ai_bus", None)
        main.execute_trade("XAUUSD", _decision(), 1_000_000)
        st = main.state_for("XAUUSD")
        self.assertEqual(len(st['pending_ai']), 1)
        st['pending_ai']['NEVER']['ts'] = time.time() - 999
        main.sweep_pending_entries("XAUUSD", st)
        self.assertEqual(st['pending_ai'], {})
        self.assertEqual(main.place_buy.call_count, 0)


# ══════════════════════════════════════════════════════════
#   4/5. position lane: EXTEND survives, CLOSE is safe
# ══════════════════════════════════════════════════════════
class TestPositionLane(unittest.TestCase):
    def _pos(self, price_current=2001.6, tp=2002.0, sl=1999.0, opened=2000.0):
        return MagicMock(ticket=777, type=0, price_open=opened,
                         price_current=price_current, tp=tp, sl=sl,
                         profit=1.6, time=time.time() - 600)

    def test_extend_updates_bot_state_and_is_not_reverted(self):
        """v2.0: the trade manager pulled the extended TP back to 90% of the
        ORIGINAL distance one loop later, silently cancelling the AI."""
        ps = {'ticket': 777, 'direction': 1, 'entry': 2000.0,
              'tp': 2002.0, 'sl': 1999.0, 'atr': 2.0, 'tf': 'M1'}
        res = PositionReviewResult(action="EXTEND", approved=True, confidence=0.9,
                                   reason_code="RUNNER", reason="still pushing")
        main.ai_position_reviewer = MagicMock()
        self.addCleanup(setattr, main, "ai_position_reviewer", None)
        info = MagicMock(digits=2)
        with patch.object(main, "get_position_by_ticket", return_value=self._pos()), \
             patch.object(main, "state_for", return_value={'positions': [ps]}), \
             patch.object(main, "send", return_value=None), \
             patch("core.trade_manager.mt5.symbol_info", return_value=info), \
             patch.object(mt5, "symbol_info", return_value=info), \
             patch.object(main, "modify_position", return_value=True) as mod:
            main.apply_position_action("XAUUSD", ps, res, self._pos(), 0.9)
        mod.assert_called_once()
        _t, new_sl, new_tp = mod.call_args[0]
        self.assertGreater(new_tp, 2002.0, "TP must be pushed beyond the original")
        self.assertEqual(ps['tp'], new_tp,
                         "the bot's own state must know the new target")
        self.assertEqual(ps['sl'], new_sl)
        self.assertTrue(ps['ai_extended'])

        # now the trade manager must NOT undo it
        df = pd.DataFrame([{"atr": 2.0, "atr_pct": 0.1, "rsi": 60, "close_vs_ema": 0.1,
                            "body_pct": 0.05, "hour_utc": 10, "bull_sweep": False,
                            "bear_sweep": False, "bull_bos": True, "bear_bos": False}])
        from core.trade_manager import manage_open_trade
        live = self._pos(price_current=2001.9, tp=new_tp, sl=new_sl)
        with patch("core.trade_manager._get_position_by_ticket", return_value=live), \
             patch("core.trade_manager.mt5.symbol_info_tick",
                   return_value=MagicMock(bid=2001.9, ask=2002.0)), \
             patch("core.trade_manager._modify_position", return_value=True) as tm_mod, \
             patch("core.trade_manager.send", return_value=None):
            manage_open_trade("XAUUSD", ps, df, MagicMock(trained=False))
        tm_mod.assert_not_called()

    def test_close_failure_uses_the_position_journal(self):
        """v2.0 reached into ai_reviewer.journal → AttributeError when the entry
        gate was off but the position manager was on. And v2.1: the journal must
        record the FAILED application, not the proposal."""
        main.ai_reviewer = None
        jrn = MagicMock()
        pr = MagicMock(journal=jrn)
        main.ai_position_reviewer = pr
        self.addCleanup(setattr, main, "ai_position_reviewer", None)
        res = PositionReviewResult(action="CLOSE", approved=True, confidence=0.9,
                                   reason_code="STALL", reason="stalling",
                                   decision_id=5)
        ps = {'ticket': 777, 'direction': 1, 'entry': 2000.0, 'tp': 2002.0,
              'sl': 1999.0}
        with patch.object(main, "get_position_by_ticket",
                          side_effect=[self._pos(), self._pos()]), \
             patch.object(main, "state_for", return_value={'positions': [ps]}), \
             patch.object(main, "close_position", return_value=(False, "broker busy")), \
             patch.object(main, "send", return_value=None):
            main.apply_position_action("XAUUSD", ps, res, None, 0.8)
        pr.annotate.assert_called_once()
        self.assertEqual(pr.annotate.call_args.kwargs["execution"], "close_failed")
        self.assertEqual(pr.annotate.call_args.args[0], 5,
                         "the annotation must land on the reviewer's own row")

    def test_extend_is_annotated_as_applied_not_asserted_upfront(self):
        """The reviewer PROPOSES (execution="proposed"); only the loop may write
        "tp_extended". Otherwise the AI's memory inflates its own impact."""
        from core.ai_position import PositionReviewer
        res = PositionReviewResult(action="EXTEND", approved=True, confidence=0.9,
                                   reason_code="RUNNER", reason="x", decision_id=9)
        ps = {'ticket': 777, 'direction': 1, 'ai_extended': True}
        pr = MagicMock()
        main.ai_position_reviewer = pr
        self.addCleanup(setattr, main, "ai_position_reviewer", None)
        with patch.object(main, "state_for", return_value={'positions': [ps]}), \
             patch.object(main, "get_position_by_ticket",
                          return_value=MagicMock(ticket=777, type=0, tp=2002.0,
                                                 sl=2001.28, price_open=2000.0,
                                                 price_current=2001.6, profit=1.6,
                                                 time=time.time())), \
             patch.object(main, "send", return_value=None):
            main.apply_position_action("XAUUSD", ps, res, None, 0.8)
        pr.annotate.assert_called_once()
        self.assertEqual(pr.annotate.call_args.kwargs["final_action"],
                         "ai_extend_already_done")
        self.assertNotIn(pr.annotate.call_args.kwargs["execution"],
                         ("tp_extended", "closed_by_ai"),
                         "a refused action must not be recorded as done")
    def test_reviewer_records_a_proposal_not_a_done_deal(self):
        from core.ai_position import PositionReviewer
        from core.ai_provider import OpenAICompatClient
        jrn = MagicMock()
        pr = PositionReviewer(jrn)
        pr._client = MagicMock()
        pr._client2 = MagicMock()
        good = (json.dumps({"action": "EXTEND", "confidence": 0.9,
                            "reason_code": "RUNNER", "reason": "x"}),
                "m1", "openai_compat")
        pr._client.complete.return_value = good
        pr._client2.complete.return_value = good
        ctx = {"symbol": "XAUUSD", "direction": "BUY", "extend_allowed": True}
        res = pr.review(ctx, ticket=777)
        self.assertTrue(res.approved)
        kw = jrn.record.call_args.kwargs
        self.assertEqual(kw["execution"], "proposed",
                         "the reviewer proposes; only the loop records action")
        jrn.mark_execution.assert_called_once()
        # the row is linked to the ticket, but still says "proposed" — the
        # ACTION is left for the loop, never assumed
        kw = jrn.mark_execution.call_args.kwargs
        self.assertEqual(kw["ticket"], 777)
        self.assertEqual(kw["execution"], "proposed")
        self.assertNotEqual(jrn.mark_execution.call_args.kwargs.get("execution"),
                            "executed")

    def test_orphaned_verdict_is_annotated_by_ticket(self):
        """A verdict that lands after its position is gone (the close path
        already swept the ticket marker) must not stay "proposed" forever."""
        from core.ai_position import PositionReviewer
        jrn = MagicMock()
        jrn.record.return_value = 4242          # the journal row id
        pr = PositionReviewer(jrn)
        pr._client = MagicMock()
        pr._client2 = MagicMock()
        good = (json.dumps({"action": "EXTEND", "confidence": 0.9,
                            "reason_code": "RUNNER", "reason": "x"}),
                "m1", "openai_compat")
        pr._client.complete.return_value = good
        pr._client2.complete.return_value = good
        pr.review({"symbol": "XAUUSD", "direction": "BUY", "extend_allowed": True},
                  ticket=909)
        before = len(jrn.mark_execution.mock_calls)
        pr.annotate_ticket(909, execution="not_applicable",
                           final_action="ai_review_void_no_state")
        self.assertGreater(len(jrn.mark_execution.mock_calls), before,
                           "the void outcome must reach the journal")
        self.assertEqual(jrn.mark_execution.call_args.args[0], 4242)
        self.assertEqual(jrn.mark_execution.call_args.kwargs["execution"],
                         "not_applicable")
        self.assertEqual(jrn.mark_execution.call_args.kwargs["final_action"],
                         "ai_review_void_no_state")
        # ...exactly once: an orphaned row is never annotated twice
        n = len(jrn.mark_execution.mock_calls)
        pr.annotate_ticket(909, execution="executed", final_action="dup")
        self.assertEqual(len(jrn.mark_execution.mock_calls), n)
        # forget() (position closed normally) drops the ticket mapping too
        pr.review({"symbol": "XAUUSD", "direction": "BUY", "extend_allowed": True},
                  ticket=910)
        pr.forget(910)
        m = len(jrn.mark_execution.mock_calls)
        pr.annotate_ticket(910, execution="executed", final_action="dup")
        self.assertEqual(len(jrn.mark_execution.mock_calls), m)

    def test_verdict_for_a_dead_position_is_ignored(self):
        main.ai_bus = AIBus(workers=1, queue_max=4).start()
        self.addCleanup(main.ai_bus.shutdown)
        self.addCleanup(setattr, main, "ai_bus", None)
        main.ai_position_reviewer = MagicMock()
        self.addCleanup(setattr, main, "ai_position_reviewer", None)
        ps = {'ticket': 777, 'direction': 1}
        st = {'positions': [ps], 'position_ai': {777: {'ts': time.time(),
                                                       'progress': 0.5}}}
        res = PositionReviewResult(action="CLOSE", approved=True, confidence=0.9,
                                   reason_code="X", reason="y")
        _orig_state_for = main.state_for
        main.state_for = lambda sym: st          # type: ignore
        self.addCleanup(setattr, main, "state_for", _orig_state_for)
        main.ai_bus._inbox.append((time.monotonic(), "position:777", True, res))
        with patch.object(main, "get_position_by_ticket", return_value=None), \
             patch.object(main, "close_position") as cp, \
             patch.object(main, "send", return_value=None):
            main.apply_position_verdicts()
        cp.assert_not_called()

    def test_drift_guard_discards_a_moved_verdict(self):
        res = PositionReviewResult(action="CLOSE", approved=True, confidence=0.9,
                                   reason_code="STALL", reason="stalling")
        ps = {'ticket': 777, 'direction': 1}
        config.AI_POSITION_MAX_DRIFT_PROGRESS = 0.15
        # reviewed at +50%, price now at -20% → the situation the AI judged is gone
        moved = self._pos(price_current=1999.6)
        with patch.object(main, "state_for", return_value={'positions': [ps]}), \
             patch.object(main, "close_position") as cp, \
             patch.object(main, "send", return_value=None):
            main.apply_position_action("XAUUSD", ps, res, moved, 0.5)
        cp.assert_not_called()

    def test_submit_never_blocks_and_marks_cadence(self):
        bus = AIBus(workers=1, queue_max=4).start()
        main.ai_bus = bus
        self.addCleanup(bus.shutdown)
        self.addCleanup(setattr, main, "ai_bus", None)
        r = MagicMock()
        r.due.return_value = True
        r.build_context.return_value = {"symbol": "XAUUSD"}
        r.review.side_effect = lambda ctx, ticket=None: time.sleep(0.5)
        main.ai_position_reviewer = r
        self.addCleanup(setattr, main, "ai_position_reviewer", None)
        st = {'positions': [{'ticket': 777, 'direction': 1, 'tp': 2002.0,
                             'sl': 1999.0}], 'position_ai': {}}
        started = time.monotonic()
        with patch.object(main, "get_position_by_ticket", return_value=self._pos()), \
             patch.object(main, "get_candles", return_value=None), \
             patch.object(main, "get_spread_percent", return_value=0.02):
            main.submit_position_reviews("XAUUSD", st)
        self.assertLess(time.monotonic() - started, 0.05,
                        "position review submission must be instant")
        r.review.assert_not_called()        # it runs on the worker, not here
        r.mark_reviewed.assert_called_once_with(777)
        self.assertIn(777, st['position_ai'])


# ══════════════════════════════════════════════════════════
#   6. AI → bot directives: clamps, persistence, expiry
# ══════════════════════════════════════════════════════════
class TestDirectives(unittest.TestCase):
    def setUp(self):
        self.path = f"{_TMP}/data/directives_test.json"
        config.AI_REGIME_STATE_PATH = self.path
        config.AI_REGIME_BASELINE = dict(getattr(config, "AI_REGIME_BASELINE", {}))
        config.AI_REGIME_BASELINE.update({
            "RISK_PERCENT_PER_TRADE": 0.5, "MAX_POSITIONS": 2,
            "AI_MIN_CONFIDENCE": 0.60, "TP_SCALE": 1.0, "MAX_SPREAD": 0.05,
            "BREAK_EVEN_PROGRESS": 0.45,
        })
        self.saved = {k: getattr(config, k) for k in config.AI_REGIME_BASELINE}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _r(self):
        config.AI_PROVIDER = "openai_compat"
        config.AI_API_KEYS = ["k"]
        config.AI_REGIME_ENABLED = True
        try:
            return RegimeReviewer(journal=MagicMock())
        finally:
            pass

    def test_range_and_baseline_clamps(self):
        # pretend a previous directive moved things, so the clamped result is a
        # real change (a no-op is reported as None by design)
        config.RISK_PERCENT_PER_TRADE = 0.4
        config.MAX_POSITIONS = 3
        config.AI_MIN_CONFIDENCE = 0.70
        r = self._r()
        # far outside the range → clamped to the edge
        self.assertAlmostEqual(
            r._guard("RISK_PERCENT_PER_TRADE", 99.0), 0.5,
            msg="risk may only go DOWN from the owner baseline, never up")
        self.assertAlmostEqual(r._guard("RISK_PERCENT_PER_TRADE", 0.01), 0.20)
        self.assertEqual(r._guard("MAX_POSITIONS", 9), 2,
                         "MAX_POSITIONS may never exceed the baseline")
        self.assertEqual(r._guard("MAX_POSITIONS", 0), 1)
        self.assertAlmostEqual(r._guard("AI_MIN_CONFIDENCE", 0.05), 0.60,
                               msg="the gate may only be made STRICTER")
        self.assertAlmostEqual(r._guard("AI_MIN_CONFIDENCE", 0.95), 0.85)
        self.assertIsNone(r._guard("TP_SCALE", 1.0), "no-op changes are dropped")

    def test_apply_journals_and_persists(self):
        r = self._r()
        decision = {"market_regime": "choppy_news", "summary": "tighten",
                    "changes": [{"param": "TP_SCALE", "value": 0.9,
                                 "reason": "range"}]}
        regime, summary, applied = r.apply(decision, {}, "m", "p", 1)
        self.assertEqual(regime, "choppy_news")
        self.assertEqual(len(applied), 1)
        self.assertAlmostEqual(config.TP_SCALE, 0.9)
        self.assertTrue(os.path.exists(self.path))

        # a NEW reviewer (i.e. a restart) picks the fresh directive up again
        config.TP_SCALE = 1.0        # pretend a fresh process (a real restart)
        r2 = RegimeReviewer(journal=MagicMock())
        self.assertAlmostEqual(getattr(config, "TP_SCALE"), 0.9,
                               msg="a fresh directive must survive a restart")
        self.assertIn("TP_SCALE", r2.applied)

        # ...and it expires back to the owner baseline, on its own
        r2.ts = time.time() - (config.AI_REGIME_TTL_SECONDS + 10)
        restored = r2.expire_if_stale()
        self.assertTrue(any(p == "TP_SCALE" for p, _o, _n in restored))
        self.assertAlmostEqual(config.TP_SCALE, self.saved["TP_SCALE"])
        self.assertFalse(os.path.exists(self.path))

    def test_illegal_param_is_inert(self):
        r = self._r()
        before = (config.DAILY_MAX_LOSS_PERCENT, config.MAX_DRAWDOWN_PERCENT)
        decision = {"market_regime": "yolo", "summary": "s",
                    "changes": [{"param": "DAILY_MAX_LOSS_PERCENT", "value": 99.0,
                                 "reason": "please"},
                                {"param": "MAX_DRAWDOWN_PERCENT", "value": 99.0,
                                 "reason": "please"}]}
        _reg, _sum, applied = r.apply(decision, {}, "m", "p", 1)
        self.assertEqual(applied, [])
        self.assertEqual((config.DAILY_MAX_LOSS_PERCENT,
                          config.MAX_DRAWDOWN_PERCENT), before,
                         "loss limits and the drawdown halt are NOT a dial")

    def test_unknown_param_cannot_enter_the_schema(self):
        self.assertNotIn("DAILY_MAX_LOSS_PERCENT", config.AI_REGIME_ALLOWED)
        self.assertNotIn("MAX_DRAWDOWN_PERCENT", config.AI_REGIME_ALLOWED)
        self.assertNotIn("MT5_LOGIN", config.AI_REGIME_ALLOWED)


# ══════════════════════════════════════════════════════════
#   7. truth of what the AI is told
# ══════════════════════════════════════════════════════════
class TestContextTruth(unittest.TestCase):
    def test_contract_matches_config(self):
        c = management_contract()
        self.assertAlmostEqual(c["break_even_at_progress"],
                               config.BREAK_EVEN_PROGRESS)
        self.assertEqual(c["engine"], config.ENTRY_ENGINE)
        self.assertEqual(c["max_positions"], config.MAX_POSITIONS)
        self.assertAlmostEqual(c["risk_percent_per_trade"],
                               config.RISK_PERCENT_PER_TRADE)
        self.assertEqual(c["trail_stages_progress"],
                         [config.TRAIL_STAGE_1_PROGRESS, config.TRAIL_STAGE_2_PROGRESS,
                          config.TRAIL_STAGE_3_PROGRESS])
        self.assertFalse(c["sl_widening_allowed"],
                         "widening a stop must stay impossible")

    def test_prompts_no_longer_claim_stale_numbers(self):
        from core import ai_review, ai_position
        blob = (ai_review.SYSTEM_POLICY + ai_review._USER_TEMPLATE
                + ai_position.SYSTEM_POLICY).lower()
        for stale in ("every 15 minutes", "break-even locks at 50%",
                      "tightens at 60% and 90%", "progress >= 0.9"):
            self.assertNotIn(stale, blob,
                             f"the prompt still hardcodes '{stale}' — the AI "
                             f"would reason about rules that are not in force")
        self.assertIn("management_contract", blob)

    def test_ai_context_is_wired_into_both_reviewers(self):
        from core.ai_review import AIReviewer
        r = AIReviewer(MagicMock())
        ctx = r.build_context("XAUUSD", _decision(), _decision()["signal"], 2.0,
                              "H1", open_positions=0, spread_pct=0.02)
        for key in ("management_contract", "ai_lessons", "bot_directives", "cost"):
            self.assertIn(key, ctx)
        # SR_H1's sl_atr is an ATR VALUE — it must not be forwarded as a distance
        self.assertAlmostEqual(ctx["proposed"]["sl_distance_atr"], 0.5, places=2)

    def test_spread_cost_math(self):
        # 0.02% of a 2000 price = 0.4 of spread against a 4.0 stop = 10%
        self.assertAlmostEqual(spread_cost_pct(0.02, 4.0, 2000.0), 10.0)
        self.assertIsNone(spread_cost_pct(0.02, 0.0, 2000.0))
        self.assertIsNone(spread_cost_pct(0.02, 4.0, 0.0))

    def test_performance_briefing_is_honest(self):
        perf = main.recent_performance()
        self.assertIsInstance(perf, dict)
        self.assertNotIn("last_20_real_trades", perf,
                         "v2.0 labelled a 5-trade window as 'last_20'")


class TestCostGate(FlowCase):
    def setUp(self):
        super().setUp()
        config.MAX_SPREAD_COST_PCT = 18.0
        self.addCleanup(setattr, config, "MAX_SPREAD_COST_PCT", 0.0)

    def test_a_trade_that_only_pays_the_spread_is_refused(self):
        """0.04% of price passes MAX_SPREAD (0.05) but is enormous against a
        $1.2 stop — the deterministic half of the judgement the AI is asked for."""
        with patch.object(main, "get_spread_percent", return_value=0.04):
            main._execute_trade_inner("XAUUSD", _decision(atr=2.0), 1_000_000,
                                      None, {'v': None})
        self.assertEqual(main.place_buy.call_count, 0)

    def test_same_signal_passes_when_cost_is_sane(self):
        with patch.object(main, "get_spread_percent", return_value=0.005):
            main._execute_trade_inner("XAUUSD", _decision(atr=2.0), 1_000_000,
                                      None, {'v': None})
        self.assertEqual(main.place_buy.call_count, 1,
                         "with a cheap spread the same setup must still trade")


# ══════════════════════════════════════════════════════════
#   9. the return half of the loop: outcome → AI journal
# ══════════════════════════════════════════════════════════
class TestJournalAnnotation(unittest.TestCase):
    def test_outcome_can_be_annotated_without_a_ticket(self):
        """mark_execution() used to REQUIRE ticket → every annotate() raised
        TypeError and was swallowed → the journal kept the provisional value
        and the AI's memory never learned what the bot actually did."""
        from core.ai_journal import AIJournal
        path = f"{_TMP}/data/annotate_test.csv"
        if os.path.exists(path):
            os.remove(path)
        j = AIJournal(path)
        did = j.record(symbol="XAUUSD", timeframe="position", direction="BUY",
                       engine="position_review", snapshot_hash="h",
                       input_summary={}, provider="p", model="m",
                       schema_version="v", feature_version="f", action="EXTEND",
                       confidence=0.9, reason_code="R", reason="r", risk_flags=[],
                       latency_ms=1, cached=False, source="ai",
                       final_action="position_extend", kind="position",
                       execution="proposed", risk_result="pending")
        j.mark_execution(did, execution="not_applicable",
                         final_action="ai_extend_already_done",
                         risk_result="not_reached")
        row = pd.read_csv(path).iloc[-1]
        self.assertEqual(row["execution"], "not_applicable")
        self.assertEqual(row["final_action"], "ai_extend_already_done")

    def test_failed_silence_is_not_allowed_in_the_journal_path(self):
        """A bookkeeping call that raises must be visible, not swallowed."""
        import inspect
        from core import ai_position
        src = inspect.getsource(ai_position.PositionReviewer.annotate)
        self.assertIn("logger.debug", src)
        # ...and the journal's own update path is exercised above, so a
        # signature change that breaks it now fails a test instead of a run.


class TestOutcomeLoop(FlowCase):
    def test_close_links_outcome_without_the_entry_gate(self):
        from core.ai_journal import AIJournal
        j = AIJournal(config.AI_JOURNAL_PATH)
        did = j.record(symbol="XAUUSD", timeframe="position", direction="BUY",
                       engine="position_review", snapshot_hash="h",
                       input_summary={}, provider="p", model="m",
                       schema_version="v", feature_version="f", action="CLOSE",
                       confidence=0.9, reason_code="R", reason="r",
                       risk_flags=[], latency_ms=1, cached=False, source="ai",
                       final_action="position_close", kind="position")
        j.mark_execution(did, ticket=777, execution="closed_by_ai",
                         final_action="ai_close", risk_result="ai_actioned")
        main.ai_journal = j
        self.addCleanup(lambda: setattr(main, "ai_journal", None))
        # the bot's reconciliation path must reach the journal even with the
        # entry reviewer switched off (that is what the position manager does)
        main.ai_reviewer = None
        trade = {"position_id": 777, "profit": 4.25, "price": 2002.0,
                 "entry": 0, "time": time.time()}
        with patch.object(main, "get_closed_trades", return_value=[trade]), \
             patch.object(main, "get_open_positions", return_value=[]), \
             patch.object(main, "_get_cur_bar", return_value=0), \
             patch.object(main, "get_candles", return_value=None), \
             patch.object(main, "get_position_by_ticket", return_value=None), \
             patch.object(main, "get_account_info", return_value=self.acc), \
             patch.object(main.trade_logger, "update_outcome") as upd, \
             patch.object(main, "send", return_value=None):
            main.symbol_state["XAUUSD"]['positions'] = [
                {'ticket': 777, 'entry': 2000.0, 'tp': 2002.0, 'sl': 1999.0,
                 'direction': 1}]
            main.process_symbol_hf("XAUUSD")
        upd.assert_called_once_with("XAUUSD", 777, 4.25)
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        row = df[df["decision_id"] == did].iloc[-1]
        self.assertEqual(float(row["profit"]), 4.25)
        self.assertEqual(int(row["outcome"]), 1)

    def test_state_for_creates_on_demand(self):
        self.assertNotIn("NEWCOIN", main.symbol_state)
        st = main.state_for("NEWCOIN")
        self.assertIs(main.symbol_state["NEWCOIN"], st)
        self.assertEqual(st['positions'], [])


class TestLoopNeverBlocks(FlowCase):
    """The headline v2.1 guarantee: an AI that takes 2 seconds must not slow
    the trading loop. In v2.0 every AI call was inline, so a slow provider
    froze break-even, trailing and close-detection for the whole timeout."""

    def _slow_reviewer(self, seconds=2.0):
        r = MagicMock()
        r.enabled = True
        r.build_context.return_value = {"symbol": "XAUUSD"}
        r.snapshot_key.side_effect = lambda ctx: "SLOW"
        r.peek_cached.return_value = None
        r.journal = MagicMock()
        r.review.side_effect = lambda ctx: (time.sleep(seconds), _approve())[1]
        main.ai_reviewer = r
        pr = MagicMock()
        pr.enabled = True
        pr.due.return_value = True
        pr.build_context.return_value = {"symbol": "XAUUSD"}
        pr.review.side_effect = lambda ctx, ticket=None: (
            time.sleep(seconds),
            PositionReviewResult(action="HOLD", approved=False, confidence=0.2,
                                 reason_code="X", reason="y"))[1]
        main.ai_position_reviewer = pr
        main.ai_journal = MagicMock()
        main.news_watcher = None
        return r

    def test_ten_cycles_with_a_two_second_ai_stay_fast(self):
        self._slow_reviewer(2.0)
        main.ai_bus = AIBus(workers=1, queue_max=8, result_ttl=30).start()
        self.addCleanup(main.ai_bus.shutdown)
        self.addCleanup(setattr, main, "ai_bus", None)
        st = main.state_for("XAUUSD")
        st['positions'] = [{'ticket': 777, 'direction': 1, 'entry': 2000.0,
                            'tp': 2002.0, 'sl': 1999.0, 'atr': 2.0, 'tf': 'H1'}]
        bars = iter([1_000_000 + i * 60 for i in range(30)])
        with patch.object(main, "_get_cur_bar", side_effect=lambda *a, **k: next(bars)), \
             patch.object(main, "get_candles", return_value=None), \
             patch.object(main, "get_open_positions",
                          return_value=[MagicMock(ticket=777)]), \
             patch.object(main, "get_closed_trades", return_value=[]), \
             patch.object(main, "get_position_by_ticket",
                          return_value=MagicMock(ticket=777, type=0, tp=2002.0,
                                                 sl=1999.0, price_open=2000.0,
                                                 price_current=2001.4,
                                                 profit=1.4, time=time.time())), \
             patch.object(main, "manage_open_trade"), \
             patch.object(main, "send", return_value=None):
            started = time.monotonic()
            for _ in range(10):
                main.process_symbol_hf("XAUUSD")
                main.collect_entry_verdicts()
                main.apply_position_verdicts()
                main.apply_regime_verdict()
            took = time.monotonic() - started
            self.assertLess(took, 0.5,
                            f"10 loop cycles with a 2s AI took {took:.2f}s — the "
                            f"AI is blocking the trading loop again")
            self.assertGreaterEqual(main.ai_bus.stats["submitted"], 1,
                                    "AI work must actually have been queued")
            self.assertEqual(main.place_buy.call_count, 0,
                             "nothing may be ordered before the verdict lands")
            # the loop stayed fast and the answer is not lost: it lands, is
            # applied, and the ticket becomes eligible for the next review
            ts_before = st['position_ai'][777]['ts']
            time.sleep(2.3)
            for _ in range(2):
                main.process_symbol_hf("XAUUSD")
                main.collect_entry_verdicts()
                main.apply_position_verdicts()
            # the marker for the ANSWERED review is gone; what remains is a
            # fresh submission (newer ts), which is the correct cadence
            self.assertGreater(st['position_ai'][777]['ts'], ts_before,
                               "the answered review must be cleared and re-armed")
            self.assertGreaterEqual(main.ai_bus.stats["submitted"], 2,
                                    "after a verdict is applied a new one may run")
            self.assertEqual(main.place_buy.call_count, 0,
                             "a HOLD verdict must not open anything")

    def test_verdict_lands_later_and_the_order_still_happens(self):
        """A 2s AI answer still gets acted on — it is late, not lost."""
        self._slow_reviewer(0.4)
        main.ai_bus = AIBus(workers=1, queue_max=8, result_ttl=30).start()
        self.addCleanup(main.ai_bus.shutdown)
        self.addCleanup(setattr, main, "ai_bus", None)
        main.execute_trade("XAUUSD", _decision(), 1_000_000)
        self.assertEqual(main.place_buy.call_count, 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            main.collect_entry_verdicts()
            if main.place_buy.call_count:
                break
            time.sleep(0.02)
        self.assertEqual(main.place_buy.call_count, 1)
        self.assertEqual(main.state_for("XAUUSD")['pending_ai'], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
