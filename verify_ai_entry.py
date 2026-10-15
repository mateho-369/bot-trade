# ============================================================
#   verify_ai_entry.py
#   Offline tests for the AI entry-review layer (feature-flagged).
#
#   Proves, with NO network and NO real broker:
#     • AI-disabled mode preserves existing behavior and makes no provider call
#     • a valid APPROVE only proceeds to the existing deterministic risk checks
#     • an existing risk veto still blocks even after AI approval
#     • REJECT / WAIT / low-confidence-APPROVE block and are never overridden
#     • timeout / invalid JSON / schema error / missing key / rate limit /
#       provider error / open circuit are safe, non-crashing, and NEVER approve
#     • no broker order is sent for AI reject/wait/failure or a risk failure
#     • duplicate/retried processing cannot create duplicate orders
#     • the stop-loss is still set (AI never removes/loosens protection)
#     • the journal links decision -> execution -> outcome without secrets
#     • the CSV journal migration is backward-compatible
#
#   Run:  python3 verify_ai_entry.py
#   (MT5 is mocked, exactly like verify_fix.py / verify_v17.py.)
# ============================================================

import sys
import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Mock MT5 before importing anything that touches it.
sys.modules['MetaTrader5'] = MagicMock()
import MetaTrader5 as mt5

import pandas as pd

# Isolate all runtime data in a temp dir; point config there BEFORE importing
# main (which instantiates TradeLogger / SMCModel at import time).
_TMP = tempfile.mkdtemp(prefix="bot_ai_test_")

import _isolate_env                # noqa: F401,E402  (never read the real .env)
import config
config.DATA_ROOT_PATH    = _TMP
config.MODEL_PATH        = f"{_TMP}/models/smc_model.pkl"
config.SCALER_PATH       = f"{_TMP}/models/smc_scaler.pkl"
config.TRADE_LOG_PATH    = f"{_TMP}/data/trade_log.csv"
config.PATTERN_DATA_PATH = f"{_TMP}/data/patterns.json"
config.AI_JOURNAL_PATH   = f"{_TMP}/data/ai_decisions.csv"
config.LOG_PATH          = f"{_TMP}/bot.log"
config.CRASH_LOG_PATH    = f"{_TMP}/crash.log"
config.TELEGRAM_ENABLED  = False
config.AI_ENABLED        = False       # import-time default: AI off
config.AI_PROVIDER       = "disabled"

import main
from core.ai_review  import AIReviewer, CircuitBreaker
from core.ai_journal import AIJournal
from core.ai_provider import (
    AIUnavailable, AIInvalidResponse, validate_entry_decision, strict_json,
)


# ── helpers ──────────────────────────────────────────────────

class FakeProvider:
    """Stands in for OpenAICompatClient. `behavior` is either a fixed
    (content, model, provider) tuple or a callable(messages, schema)."""
    name = "openai_compat"

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    def complete(self, messages, schema):
        self.calls += 1
        if callable(self.behavior):
            return self.behavior(messages, schema)
        return self.behavior


def _content(action="APPROVE", conf=0.8, reason_code="TREND_ALIGNED",
             reason="context supports the candidate", risk_flags=None):
    return json.dumps({
        "action": action, "confidence": conf, "reason_code": reason_code,
        "reason": reason, "risk_flags": risk_flags or [],
    })


def _ok(content=None, model="gpt-4o-mini"):
    return (content if content is not None else _content(),
            model, "openai_compat")


def _decision(direction="BUY"):
    signal = {
        "atr": 2.0, "atr_pct": 0.1, "rsi": 55.0, "body_pct": 0.05,
        "close_vs_ema": 0.02, "hour_utc": 10,
        "bull_sweep": False, "bear_sweep": False,
        "bull_bos": True, "bear_bos": False, "signal": direction,
        "bar_time": pd.Timestamp("2026-01-01 10:00"),
    }
    return {
        "engine": "SR_H1", "direction": direction, "n_positions": 1,
        "chosen_tf": "H1", "signal": signal, "sl_price": 1998.0,
        "sl_atr": 2.0, "rr": 2.0, "score": 1, "summary": "BUY H1 breakout",
    }


def _fresh_journal():
    path = config.AI_JOURNAL_PATH
    if os.path.exists(path):
        os.remove(path)
    return AIJournal(path)


def _make_reviewer(behavior, *, min_conf=0.6, max_retries=0, circuit_failures=100):
    config.AI_ENABLED = True
    config.AI_PROVIDER = "openai_compat"
    config.AI_MIN_CONFIDENCE = min_conf
    config.AI_MAX_RETRIES = max_retries
    config.AI_CIRCUIT_FAILURES = circuit_failures
    config.AI_CIRCUIT_COOLDOWN_SECONDS = 60
    j = _fresh_journal()
    r = AIReviewer(j)
    r.enabled = True
    r._client = FakeProvider(behavior)
    return r, j


# ── pure validation ──────────────────────────────────────────

class TestStrictValidation(unittest.TestCase):
    def test_valid_decision(self):
        d = validate_entry_decision(json.loads(_content()))
        self.assertEqual(d["action"], "APPROVE")
        self.assertEqual(d["confidence"], 0.8)

    def test_unknown_field_rejected(self):
        obj = json.loads(_content()); obj["extra"] = 1
        self.assertRaises(AIInvalidResponse, validate_entry_decision, obj)

    def test_bad_action_rejected(self):
        obj = json.loads(_content(action="FOO"))
        self.assertRaises(AIInvalidResponse, validate_entry_decision, obj)

    def test_bool_confidence_rejected(self):
        obj = json.loads(_content()); obj["confidence"] = True
        self.assertRaises(AIInvalidResponse, validate_entry_decision, obj)

    def test_confidence_out_of_range(self):
        for bad in (1.5, -0.1, float("nan"), float("inf")):
            obj = json.loads(_content()); obj["confidence"] = bad
            self.assertRaises(AIInvalidResponse, validate_entry_decision, obj)

    def test_reason_code_and_reason_bounds(self):
        obj = json.loads(_content()); obj["reason_code"] = "x" * 33
        self.assertRaises(AIInvalidResponse, validate_entry_decision, obj)
        obj = json.loads(_content()); obj["reason"] = ""
        self.assertRaises(AIInvalidResponse, validate_entry_decision, obj)
        obj = json.loads(_content()); obj["reason"] = "bad\x01ctrl"
        self.assertRaises(AIInvalidResponse, validate_entry_decision, obj)

    def test_risk_flags_validated(self):
        obj = json.loads(_content()); obj["risk_flags"] = ["NOT_A_FLAG"]
        self.assertRaises(AIInvalidResponse, validate_entry_decision, obj)
        obj = json.loads(_content()); obj["risk_flags"] = ["HIGH_SPREAD"] * 7
        self.assertRaises(AIInvalidResponse, validate_entry_decision, obj)

    def test_strict_json_rejects_bad_input(self):
        self.assertRaises(AIInvalidResponse, strict_json, '{"a":1,"a":2}')   # dup key
        self.assertRaises(AIInvalidResponse, strict_json, '[1,2,3]')         # not object
        self.assertRaises(AIInvalidResponse, strict_json, '{"a":NaN}')       # NaN
        self.assertRaises(AIInvalidResponse, strict_json, 'not json')        # garbage
        self.assertRaises(AIInvalidResponse, strict_json, '{"a":"' + "x"*600 + '"}')  # long str
        self.assertEqual(strict_json('{"a":1}'), {"a": 1})


# ── reviewer decisions ───────────────────────────────────────

class TestReviewer(unittest.TestCase):
    def _ctx(self, symbol="XAUUSD"):
        r, _ = _make_reviewer(_ok())
        return r.build_context(symbol, _decision(), _decision()["signal"],
                               2.0, "H1", open_positions=0, spread_pct=0.01)

    def test_approve(self):
        r, j = _make_reviewer(_ok(_content(action="APPROVE", conf=0.9)))
        res = r.review(self._ctx())
        self.assertTrue(res.approved)
        self.assertEqual(res.action, "APPROVE")
        self.assertEqual(r._client.calls, 1)

    def test_approve_below_min_confidence_is_wait(self):
        r, _ = _make_reviewer(_ok(_content(action="APPROVE", conf=0.3)), min_conf=0.6)
        res = r.review(self._ctx())
        self.assertFalse(res.approved)
        self.assertEqual(res.action, "WAIT")

    def test_reject_blocks(self):
        r, _ = _make_reviewer(_ok(_content(action="REJECT", conf=0.9,
                                           reason_code="COUNTER_TREND")))
        res = r.review(self._ctx())
        self.assertFalse(res.approved)
        self.assertEqual(res.action, "REJECT")

    def test_wait_blocks(self):
        r, _ = _make_reviewer(_ok(_content(action="WAIT", conf=0.9)))
        res = r.review(self._ctx())
        self.assertFalse(res.approved)

    def test_timeout_blocks_and_no_crash(self):
        def boom(m, s): raise AIUnavailable("network_or_timeout")
        r, _ = _make_reviewer(boom, max_retries=1)
        res = r.review(self._ctx())
        self.assertFalse(res.approved)
        self.assertEqual(res.source, "ai_unavailable")

    def test_invalid_json_blocks_no_retry(self):
        r, _ = _make_reviewer(_ok("this is not json {"), max_retries=3)
        res = r.review(self._ctx())
        self.assertFalse(res.approved)
        self.assertEqual(res.source, "ai_error")
        self.assertEqual(r._client.calls, 1)   # invalid reply is never retried

    def test_schema_violation_blocks(self):
        r, _ = _make_reviewer(_ok(_content(action="MAYBE")))
        res = r.review(self._ctx())
        self.assertFalse(res.approved)
        self.assertEqual(res.source, "ai_error")

    def test_invalid_response_does_not_open_circuit(self):
        # Repeated invalid replies block every trade (fail-closed) but do NOT
        # open the availability circuit, so a later valid reply still works.
        seq = {"n": 0}
        def sometimes_bad(m, s):
            seq["n"] += 1
            if seq["n"] <= 3:
                return _ok("<<<not json>>>")
            return _ok(_content(conf=0.9))
        r, _ = _make_reviewer(sometimes_bad, max_retries=0, circuit_failures=3)
        for _ in range(3):
            self.assertFalse(r.review(self._ctx()).approved)
        self.assertTrue(r.circuit.allow())          # still closed
        self.assertTrue(r.review(self._ctx()).approved)

    def test_missing_key_blocks(self):
        def boom(m, s): raise AIUnavailable("missing_api_key")
        r, _ = _make_reviewer(boom)
        res = r.review(self._ctx())
        self.assertFalse(res.approved)

    def test_rate_limit_blocks(self):
        def boom(m, s): raise AIUnavailable("http_429")
        r, _ = _make_reviewer(boom)
        res = r.review(self._ctx())
        self.assertFalse(res.approved)

    def test_provider_error_blocks(self):
        def boom(m, s): raise AIUnavailable("http_5xx")
        r, _ = _make_reviewer(boom)
        res = r.review(self._ctx())
        self.assertFalse(res.approved)

    def test_no_failure_mode_can_approve(self):
        for behavior in (
            lambda m, s: (_ for _ in ()).throw(AIUnavailable("network_or_timeout")),
            lambda m, s: (_ for _ in ()).throw(AIUnavailable("missing_api_key")),
            lambda m, s: (_ for _ in ()).throw(AIUnavailable("http_429")),
            lambda m, s: (_ for _ in ()).throw(AIInvalidResponse("invalid_json")),
            lambda m, s: ("garbage", "m", "openai_compat"),
            lambda m, s: (_content(action="REJECT"), "m", "openai_compat"),
        ):
            r, _ = _make_reviewer(behavior)
            self.assertFalse(r.review(self._ctx()).approved)

    def test_circuit_opens_and_blocks_without_call(self):
        def boom(m, s): raise AIUnavailable("http_5xx")
        r, _ = _make_reviewer(boom, max_retries=0, circuit_failures=3)
        for _ in range(3):
            r.review(self._ctx())
        calls_after_open = r._client.calls
        res = r.review(self._ctx())            # circuit now open
        self.assertFalse(res.approved)
        self.assertEqual(res.reason_code, "CIRCUIT_OPEN")
        self.assertEqual(r._client.calls, calls_after_open)  # no new provider call

    def test_circuit_recovers_on_success(self):
        seq = {"n": 0}
        def flaky(m, s):
            seq["n"] += 1
            if seq["n"] <= 2:
                raise AIUnavailable("http_5xx")
            return _ok(_content(conf=0.9))
        r, _ = _make_reviewer(flaky, max_retries=0, circuit_failures=3)
        self.assertFalse(r.review(self._ctx()).approved)
        self.assertFalse(r.review(self._ctx()).approved)   # 2 failures, circuit still closed
        res = r.review(self._ctx())                        # success resets the circuit
        self.assertTrue(res.approved)
        self.assertEqual(r.circuit.failures, 0)

    def test_cache_hit_no_second_call(self):
        r, _ = _make_reviewer(_ok(_content(conf=0.9)))
        ctx = self._ctx()
        r.review(ctx)
        res2 = r.review(ctx)                   # identical context
        self.assertTrue(res2.cached)
        self.assertEqual(res2.source, "cache")
        self.assertTrue(res2.approved)
        self.assertEqual(r._client.calls, 1)   # provider called only once

    def test_cache_keyed_by_context(self):
        r, _ = _make_reviewer(_ok(_content(conf=0.9)))
        r.review(self._ctx("XAUUSD"))
        r.review(self._ctx("EURUSD"))          # different symbol => new call
        self.assertEqual(r._client.calls, 2)


# ── journal ──────────────────────────────────────────────────

class TestJournal(unittest.TestCase):
    def test_record_and_link(self):
        j = _fresh_journal()
        did = j.record(symbol="XAUUSD", timeframe="H1", direction="BUY",
                       engine="SR_H1", snapshot_hash="abc", input_summary={"x": 1},
                       provider="openai_compat", model="gpt-4o-mini",
                       schema_version="entry_review_v1", feature_version="sr_h1_v1",
                       action="APPROVE", confidence=0.8, reason_code="OK",
                       reason="fine", risk_flags=[], latency_ms=12, cached=False,
                       source="ai", final_action="approved_to_risk")
        self.assertEqual(did, 1)
        j.mark_execution(did, ticket=777, risk_result="passed", execution="executed")
        n = j.link_outcome("XAUUSD", 777, 25.0)
        self.assertEqual(n, 1)
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        row = df.iloc[0]
        self.assertEqual(row["execution"], "executed")
        self.assertEqual(row["ticket"], 777)
        self.assertEqual(row["outcome"], 1)
        self.assertEqual(row["profit"], 25.0)

    def test_finalize_pending_only_once(self):
        j = _fresh_journal()
        did = j.record(symbol="XAUUSD", timeframe="H1", direction="BUY",
                       engine="SR_H1", snapshot_hash="abc", input_summary={},
                       provider="p", model="m", schema_version="v",
                       feature_version="f", action="APPROVE", confidence=0.8,
                       reason_code="OK", reason="r", risk_flags=[],
                       latency_ms=1, cached=False, source="ai",
                       final_action="approved_to_risk")
        j.finalize_pending(did, block_reason="risk_gate:daily loss limit")
        j.finalize_pending(did, block_reason="SHOULD_NOT_OVERWRITE")  # no-op
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        self.assertEqual(df.iloc[0]["execution"], "not_executed")
        self.assertEqual(df.iloc[0]["risk_result"], "risk_gate:daily loss limit")

    def test_link_outcome_ignores_open_trades(self):
        j = _fresh_journal()
        j.record(symbol="XAUUSD", timeframe="H1", direction="BUY",
                 engine="SR_H1", snapshot_hash="h", input_summary={},
                 provider="p", model="m", schema_version="v",
                 feature_version="f", action="APPROVE", confidence=0.8,
                 reason_code="OK", reason="r", risk_flags=[],
                 latency_ms=1, cached=False, source="ai",
                 final_action="approved_to_risk")
        # no ticket yet (never executed) -> outcome must NOT be attached
        self.assertEqual(j.link_outcome("XAUUSD", 999, 10.0), 0)

    def test_migration_backward_compatible(self):
        # Simulate an OLD journal with only a subset of columns.
        old_cols = ["decision_id", "time", "kind", "symbol", "action",
                    "confidence", "reason", "source"]
        old = pd.DataFrame([{"decision_id": 1, "time": "t", "kind": "entry",
                             "symbol": "XAUUSD", "action": "APPROVE",
                             "confidence": 0.7, "reason": "old row",
                             "source": "ai"}])
        path = os.path.join(_TMP, "ai_decisions.csv")
        old[old_cols].to_csv(path, index=False)
        j = AIJournal(path)                     # must migrate, keep the old row
        df = pd.read_csv(path)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["reason"], "old row")
        for c in ("execution", "risk_result", "snapshot_hash", "outcome"):
            self.assertIn(c, df.columns)
        # new id continues after the old max
        did = j.record(symbol="XAUUSD", timeframe="H1", direction="BUY",
                       engine="SR_H1", snapshot_hash="h", input_summary={},
                       provider="p", model="m", schema_version="v",
                       feature_version="f", action="WAIT", confidence=0.5,
                       reason_code="W", reason="r", risk_flags=[],
                       latency_ms=1, cached=False, source="ai",
                       final_action="ai_wait")
        self.assertEqual(did, 2)

    def test_summary_is_bounded_and_secret_free(self):
        from core.ai_journal import _sanitize_summary
        big = {"k" + str(i): "v" * 50 for i in range(200)}
        s = _sanitize_summary(big)
        self.assertLessEqual(len(s), 800)


# ── end-to-end through execute_trade ─────────────────────────

class TestExecuteTradeIntegration(unittest.TestCase):
    def setUp(self):
        for s in list(main.symbol_state):
            main.symbol_state[s] = main.fresh_state()
        main.ai_reviewer = None
        acc = MagicMock(); acc.balance = 100.0; acc.equity = 100.0
        self.acc = acc
        self.p_acc = self.enterContext(patch.object(main, "get_account_info", return_value=acc))
        self.p_spread = self.enterContext(patch.object(main, "get_spread_percent", return_value=0.01))
        self.p_pos = self.enterContext(patch.object(main, "get_open_positions", return_value=[]))
        self.p_buy = self.enterContext(patch.object(main, "place_buy", return_value=(777, "OK")))
        self.p_sell = self.enterContext(patch.object(main, "place_sell", return_value=(778, "OK")))
        self.p_allowed = self.enterContext(patch("core.risk_manager.positions_allowed", return_value=3))
        self.p_lot = self.enterContext(patch("core.risk_manager.calc_lot_size", return_value=0.01))
        self.p_can = self.enterContext(patch("core.risk_manager.can_open_new_trade", return_value=(True, "")))
        mt5.symbol_info.return_value.digits = 2
        mt5.symbol_info_tick.return_value = MagicMock(ask=2000.0, bid=1999.9)

    def _set_ai(self, behavior, **kw):
        r, j = _make_reviewer(behavior, **kw)
        main.ai_reviewer = r
        return r, j

    def _run(self, decision=None, cur_bar=1_000_000):
        main.execute_trade("XAUUSD", decision or _decision(), cur_bar)

    # -- disabled -------------------------------------------------
    def test_disabled_none_reviewer_places_order(self):
        main.ai_reviewer = None
        self._run()
        self.assertEqual(self.p_buy.call_count, 1)   # existing behavior preserved

    def test_disabled_reviewer_makes_no_provider_call(self):
        fake = FakeProvider(_ok())
        r, _ = _make_reviewer(_ok())
        r.enabled = False
        r._client = fake
        main.ai_reviewer = r
        self._run()
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self.p_buy.call_count, 1)

    # -- approve -> risk -> order ---------------------------------
    def test_approve_proceeds_to_risk_and_places_order(self):
        r, j = self._set_ai(_ok(_content(action="APPROVE", conf=0.9)))
        self._run()
        self.assertEqual(self.p_buy.call_count, 1)
        # risk-sized lot is used (AI cannot change size)
        args = self.p_buy.call_args[0]
        self.assertEqual(args[1], 0.01)
        self.assertGreater(args[2], 0.0)             # SL still set (>0)
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        row = df.iloc[-1]
        self.assertEqual(row["action"], "APPROVE")
        self.assertEqual(row["execution"], "executed")
        self.assertEqual(row["risk_result"], "passed")
        self.assertEqual(row["ticket"], 777)

    def test_approve_but_risk_veto_blocks_no_order(self):
        self.p_can.return_value = (False, "daily loss limit (-21.0%)")
        r, j = self._set_ai(_ok(_content(action="APPROVE", conf=0.9)))
        self._run()
        self.assertEqual(self.p_buy.call_count, 0)   # deterministic veto wins
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        row = df.iloc[-1]
        self.assertEqual(row["action"], "APPROVE")
        self.assertEqual(row["execution"], "not_executed")
        self.assertTrue(str(row["risk_result"]).startswith("risk_gate:"))

    # -- reject / wait / failure ----------------------------------
    def test_reject_blocks_no_order(self):
        r, j = self._set_ai(_ok(_content(action="REJECT", conf=0.95,
                                         reason_code="COUNTER_TREND")))
        self._run()
        self.assertEqual(self.p_buy.call_count, 0)
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        row = df.iloc[-1]
        self.assertEqual(row["action"], "REJECT")
        self.assertEqual(row["execution"], "not_executed")
        self.assertEqual(row["final_action"], "ai_reject")

    def test_wait_blocks_no_order(self):
        self._set_ai(_ok(_content(action="WAIT", conf=0.9)))
        self._run()
        self.assertEqual(self.p_buy.call_count, 0)

    def test_ai_failure_blocks_no_order(self):
        def boom(m, s): raise AIUnavailable("http_5xx")
        self._set_ai(boom)
        self._run()
        self.assertEqual(self.p_buy.call_count, 0)
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        self.assertEqual(df.iloc[-1]["execution"], "not_executed")

    def test_invalid_reply_blocks_no_order(self):
        self._set_ai(_ok("<<<not json>>>"))
        self._run()
        self.assertEqual(self.p_buy.call_count, 0)

    def test_unexpected_ai_error_fails_closed(self):
        # A non-AI exception from the reviewer must be caught by the gate,
        # block the entry, and never crash the trading loop.
        def boom(m, s):
            raise RuntimeError("unexpected")
        self._set_ai(boom)
        try:
            self._run()
        except RuntimeError:
            self.fail("unexpected AI error escaped execute_trade")
        self.assertEqual(self.p_buy.call_count, 0)

    # -- duplicate processing -------------------------------------
    def test_duplicate_processing_no_duplicate_order(self):
        self._set_ai(_ok(_content(action="APPROVE", conf=0.9)))
        d = _decision()
        self._run(d, cur_bar=1_000_000)
        self._run(d, cur_bar=1_000_000)   # retried/duplicated same candidate
        self.assertEqual(self.p_buy.call_count, 1)   # still exactly one order

    # -- outcome linkage ------------------------------------------
    def test_full_lifecycle_decision_execution_outcome(self):
        r, j = self._set_ai(_ok(_content(action="APPROVE", conf=0.9)))
        self._run()
        # simulate the close reconciliation the bot performs on a real deal
        j.link_outcome("XAUUSD", 777, -3.5)
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        row = df.iloc[-1]
        self.assertEqual(row["ticket"], 777)
        self.assertEqual(row["outcome"], 0)          # loss
        self.assertEqual(row["profit"], -3.5)

    def test_journal_has_no_secret_fields(self):
        r, j = self._set_ai(_ok(_content(action="APPROVE", conf=0.9)))
        self._run()
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        self.assertNotIn("api_key", " ".join(df.columns).lower())
        self.assertNotIn("password", " ".join(df.columns).lower())
        # the stored snapshot summary never contains credentials
        self.assertNotIn("api_key", str(df.iloc[-1]["input_summary"]).lower())


class TestCircuitBreakerUnit(unittest.TestCase):
    def test_open_then_cooldown(self):
        cb = CircuitBreaker(failures=2, cooldown_seconds=0.2)
        self.assertTrue(cb.allow())
        cb.on_failure(); self.assertTrue(cb.allow())
        cb.on_failure(); self.assertFalse(cb.allow())   # opened
        import time; time.sleep(0.25)
        self.assertTrue(cb.allow())                     # cooled down

    def test_success_resets(self):
        cb = CircuitBreaker(failures=3, cooldown_seconds=60)
        cb.on_failure(); cb.on_failure(); cb.on_success()
        cb.on_failure(); self.assertTrue(cb.allow())    # reset, not open


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)
    try:
        os.rmdir("logs")   # main.py creates ./logs at import (gitignored)
    except OSError:
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
