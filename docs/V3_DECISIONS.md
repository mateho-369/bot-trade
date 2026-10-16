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

## Locked for later phases (do NOT build yet)1. **ai_regime replacement (P4):** no AI may edit live config. Replace the
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

## P1 verdict (2026-10-16, real MT5 data run on Windows @ d585f26)
- **SR_H1 baseline: FAIL.** Real run 2018-06..2026-10, per-bar spread +
  bid/ask model: 1,216 trades, WR 31.7%, PF 0.903, net E[R] -0.0675
  (95% CI -0.146..+0.010), gross +0.0033R, max DD 86R. Duel method
  (fixed 0.30): PF 0.896, E[R] -0.0721R. BUY PF 1.064 (+0.042R);
  SELL PF 0.731 (-0.200R); only 2025 had PF > 1 (1.12).
- The documented 2024-2025 edge (PF 1.15, +0.020R) does NOT survive a
  longer window with realistic costs. **Do not tune SR_H1 to make it
  pass.** It may enter P2 only as one pre-registered candidate among
  others (via the SMC/SR family as specified), with no special status.
- Open diagnostic (into P2 tooling): zero SR trades before 2021 despite
  H1 from 2018-06. From the code, warm-up cannot explain it (H4 EMA50
  ready ~50 H4 bars, H1 ATR/EMA within ~200 H1 bars). The derived H4
  frame starts at the first H1 bar (2018-06-28 00:00 UTC as returned).
  `tools/diagnose_sr.py` prints the real first H4 timestamp, the funnel
  per year and the first signal date on the Windows data.

## Handoff continuation (2026-10-16, current session)
- Owner's handoff explicitly requests archiving the full specification,
  committing it, and pushing the current session branch so the local agent
  can pull it. Current session branch is `arena/ba18f2e6-bot-trade`, starting
  at `6a54fbb` (same tip as fetched `arena/a951958a-bot-trade`). This
  supersedes the earlier session-specific branch name above; `main` is
  untouched. No reset, merge or force-push is needed.
- Full owner spec is archived in `docs/V3_SPEC.md`. The live Windows P2 v1
  run remains frozen: no engine/cost/backtester/plan/runner changes and no
  request to rerun. The checker is a separate read-only audit tool.
- `docs/P2_CHECKER.md` documents evidence limits and inherited v1 semantic
  caveats. They are review findings, NOT approved research changes. A
  checker PASS means runner parity, not independent section-6 certification.
- Do not begin P3, change the plan, or place demo orders before the owner
  supplies the P2 outputs and approves the next phase. No multiprocessing
  feature was added during this handoff.
