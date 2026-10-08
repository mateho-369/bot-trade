# AI Entry Review (optional, feature-flagged)

A read-only **AI risk-reviewer** that sits in front of the bot's existing
deterministic entry path. It reviews a candidate signal produced by the
strategy and may **APPROVE / REJECT / WAIT** it. It is **OFF by default** and,
when off, the bot behaves exactly as before (no provider call, no journal).

## Where it plugs in

```
strategy (scan_sr / flash_engine.decide / scan_all_timeframes)
        │  candidate `decision`
        ▼
main.execute_trade()            ◀── AI gate requested HERE, on a worker thread
        │  AIBus.submit(entry) → the 0.5s loop never waits on HTTP
        │  verdict returns later → freshness re-check (same bar, ≤0.5 ATR,
        │  ≤45s) → APPROVE (conf ≥ AI_MIN_CONFIDENCE)
        ▼
_execute_trade_inner()          ── unchanged deterministic path ──
   ENABLE_BUY/SELL → cooldown → volatility guard → position cap →
   spread → ML filter → per-position {price/TP/SL, calc_lot_size,
   can_open_new_trade RISK GATE, place_buy/sell}
        │  order fills
        ▼
journal links decision → ticket → (on close) reconciled outcome
```

The AI can only **veto**. A valid `APPROVE` merely lets the candidate continue
to the existing risk checks — it is **not** an order. The AI never places or
cancels orders, sizes positions, moves/removes stops, changes a kill switch /
loss limit / drawdown limit, or overrides a deterministic veto.

## Safety guarantees

- **Fail-closed.** `REJECT`, `WAIT`, a low-confidence `APPROVE`, and *any*
  failure (timeout, invalid JSON/schema, missing key, rate limit, provider
  error, open circuit, unexpected error) **block** the entry. None can approve.
- **Risk gates unchanged.** No valid stop or a failed risk gate still means no
  entry, regardless of AI approval.
- **Bounded.** Hard request timeout, bounded retries, a circuit breaker, a
  context-keyed cache, and strict bounded JSON/schema validation. All inline in
  the synchronous loop (no second thread racing the broker).
- **No secrets in prompts/journal.** Only a sanitized decision-time snapshot is
  sent (symbol/timeframe, direction, indicator/regime values, proposed stop
  distance in ATR, spread, current exposure). No credentials, account numbers,
  future bars, or raw provider/exception bodies. External text is treated as
  untrusted data, never instructions.
- **Additive journal.** A CSV next to the existing trade log
  (`SMC_Bot_Data/data/ai_decisions.csv`). Existing databases stay readable; new
  columns are added in place.

## Settings (config.py; secrets via `.env`)

| Setting | Default | Meaning |
|---|---|---|
| `AI_ENABLED` | `false` | Entry gate (role 1). `false` ⇒ identical behavior, no call. |
| `AI_POSITION_ENABLED` / `AI_REGIME_ENABLED` | `true` | Roles 2/3 — **independent of `AI_ENABLED` since v2.1** (in v2.0 they silently required it). |
| `AI_ASYNC` / `AI_WORKER_THREADS` / `AI_QUEUE_MAX` | `true` / `1` / `12` | AI runs off the hot loop; a full queue drops work AND backs the scan off. |
| `AI_ENTRY_MAX_AGE_SECONDS` / `AI_ENTRY_MAX_DRIFT_ATR` | `45` / `0.5` | A verdict may only fire on the bar the AI actually saw. |
| `AI_REJECT_COOLDOWN_SECONDS` | `45` | After a REJECT, stop re-asking every bar (v2.0 hardcoded 300s). |
| `AI_PROVIDER` | `disabled` | `openai_compat` (OpenAI/Groq/Together/local `/v1`) or `disabled`. |
| `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | OpenAI / empty / `gpt-4o-mini` | Endpoint + key (env only) + model. |
| `AI_RESPONSE_FORMAT` | `json_object` | `json_schema` for providers with strict structured output. |
| `AI_TIMEOUT_SECONDS` | `8.0` | Hard per-request timeout. |
| `AI_MAX_RETRIES` | `1` | Bounded retries on availability errors (2 attempts total). |
| `AI_CIRCUIT_FAILURES` / `AI_CIRCUIT_COOLDOWN_SECONDS` | `5` / `120` | Open the circuit after N consecutive availability failures. |
| `AI_MIN_CONFIDENCE` | `0.60` | An `APPROVE` below this is treated as `WAIT`. |
| `AI_JOURNAL_PATH` | `SMC_Bot_Data/data/ai_decisions.csv` | Additive decision journal. |
| `AI_SCHEMA_VERSION` / `AI_FEATURE_VERSION` | `entry_review_v2` / `sr_h1_v2` | Stamped into prompts + journal. |
| `MAX_SPREAD_COST_PCT` | `18.0` | Cost gate: refuse a trade whose spread is >18% of the stop (the reason M1 gold lost — see `REAL_WORLD_TEST.md`). |

**v2.1 changes the direction of this doc's promise — in both ways.** The AI no
longer only vetoes: it receives the bot's *real* management contract (the actual
BE/trailing/tighten numbers, not a description), the live news blackout, and
`ai_lessons()` / `bot_results()` — its own past proposals next to what actually
happened, read back from the journal it used to only write to. Positions get a
bounded advisory channel back (`EXTEND` now survives Rule 4). Full flow, the
17 fixed defects and the diagrams: **`docs/FLOW_AI_BOT.md`**.

## Enable / disable safely

- **Disabled (default):** leave `AI_ENABLED=false`. No provider call, no journal.
- **Enable for a demo/test:** in `.env`, set `AI_ENABLED=true`,
  `AI_PROVIDER=openai_compat`, and point `AI_BASE_URL`/`AI_API_KEY`/`AI_MODEL`
  at any OpenAI-compatible endpoint. See `.env.example`.
  ⚠️ Enabling AI makes approval **required**: while on, a reject/wait or any
  failure blocks new entries (fail-closed). If the key/endpoint is wrong the bot
  stops opening new trades until fixed — set `AI_ENABLED=false` to trade without
  AI. Never weaken a live-account guard to use this.

The offline test suite `verify_ai_entry.py` exercises the AI-enabled path end to
end with a fake provider (no network, no real broker).

## Journal columns (one row per AI-reviewed candidate)

`decision_id, time, kind, symbol, timeframe, direction, engine, snapshot_hash,
input_summary, provider, model, schema_version, feature_version, action,
confidence, reason_code, reason, risk_flags, latency_ms, cached, source,
risk_result, execution, ticket, final_action, outcome, profit`

- `source`: `ai` / `cache` / `ai_error` / `ai_unavailable`.
- `execution`: `pending` → `executed` / `not_executed`.
- `risk_result`: `pending` / `passed` / `risk_gate:<reason>` / `not_reached` / …
- `outcome`/`profit`: filled only from the bot's **own reconciled closed deals**
  (never from an open trade or a guess).

## Intentionally out of scope (future phases)

AI-controlled dynamic risk settings, model-generated signals, AI position
closing, and AI trailing-stop changes are **not** implemented here. A hosted LLM
does not train itself from this journal; filtered lessons could later be fed as
prompt context only, clearly labeled as hypotheses.

## Tests

```
python3 verify_ai_entry.py     # 43 offline tests (MT5 mocked)
```
