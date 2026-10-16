# V3 Decisions Log (owner-approved, noted for later phases)

Recorded 2026-10-16 from the owner's P0 review. These are decisions, not
implementations: each item is built only in its own phase, behind its gate.

## Branch & workflow
- Work on `arena/a951958a-bot-trade`; v3 lives in NEW folders/files, old
  code untouched and working. Small focused commits to this branch are
  allowed. Never push to `main`, never merge, never force-push.
- Corrections to the audit record: commit `6c62410` (P0 audit) is already
  pushed and PR #6 is open -- the audit text saying "not committed" was
  true when written and is superseded by this note.

## Locked for later phases (do NOT build yet)
1. **ai_regime replacement (P4):** no AI may edit live config. Replace the
   v2 directives with a DETERMINISTIC table "Analyst regime -> preset":
   CHAOS => stand aside; RANGE => range engines only; TREND_UP/DOWN =>
   trend engines only; QUIET => wider time-stop or stand aside. The table
   itself must be justified by backtest/shadow results, never guessed.
2. **R2 fix (P4):** no past results, no account balance to any AI role.
   AI-3 Guardian DOES see the open trade's money (floating / target /
   peak / stop in $) plus candles. AI-1 sees market data only.
3. **AI providers (P4):** Groq `qwen/qwen3.8-27b` (entry reviewer +
   guardian) and Gemini `gemini-3.5-flash-lite` (analyst, own
   AI_BRIEF_BASE_URL / AI_BRIEF_API_KEYS). Free-tier budgets matter:
   Gemini flash-lite ~500 requests/day; Groq limits are per-organisation.
   Call budgeting per provider must be measured before enforcement.
4. **R1 ALLOW_LIVE gate:** implemented in P1 (core/execution/live_gate.py,
   wired into mt5_connector.connect() and preflight.py, unit-tested).

## P1 scope actually built (2026-10-16)
- L0: core/data/ (canonical bars/ticks, Parquet, manifest, spread-by-hour,
  synthetic fixture generator), core/costs.py (spread + 0.3x slippage,
  stress x1.5 switch).
- L1 baseline: core/signals/sr_h1.py (faithful port of core/sr_engine.py).
- Backtester: core/backtester_v3.py (next-bar-open entry, SL-first,
  triple barrier, net-of-cost, MFE/MAE, bootstrap CI).
- Tools: tools/download_history.py (Windows/MT5), tools/run_phase.py P1,
  tools/make_fixture.py. Fixture: tests/fixtures/XAUUSDm_M1_synthetic.parquet
  (synthetic, seeded, <1 MB -- pipeline checks only, never a result).
- Tests: tests/ (22 offline tests) + the legacy verify_*.py suite.
