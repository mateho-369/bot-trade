# V3 Owner Specification — EDGE-FIRST AI SCALPER

Archived from the owner's handoff on 2026-10-16. The specification below is preserved as supplied (not a claim that its cited evidence has been independently verified). Later owner-approved scope/workflow decisions are in `docs/V3_DECISIONS.md` and `research/P2_plan.md`. This Arena session is fixed to `arena/ba18f2e6-bot-trade`, not the originally requested `v3-rebuild`; `main` remains untouched. The handoff explicitly authorises committing this specification and pushing to the session branch. P2 v1 remains frozen while the owner's local run completes.

---

You are the lead engineer and quant researcher for my MetaTrader 5 trading bot (repo = this
folder, "BOSS SMC BOT"). Rebuild it as v3 "EDGE-FIRST AI SCALPER". Work in a new branch
`v3-rebuild`; keep the old code working on `main` until v3 passes its gates.

=====================================================================================
0. MY GOAL (the owner)
=====================================================================================
- Win and profit: many small, fast wins (about $2-5 per trade is fine), losses kept small.
- I do not care about trade frequency for its own sake and I do not want the AI to steer by
  account balance or past P&L. I care about ONE thing: a measurable, repeatable edge after costs.
- Instrument: XAUUSDm (Exness, MT5). Account: DEMO (Exness-MT5Trial6, about $32).
  Other symbols only after XAUUSDm passes.
- AI must analyse the chart and the market, the bot must analyse too, they must check each
  other, and an AI must watch every open trade (see its money + candles) to ride winners or
  close early to protect profit.
- Explain everything to me in Khmer. Code, logs, commands and file names in English.

=====================================================================================
1. HARD RULES (never break)
=====================================================================================
1. Secrets: never print, log, commit or paste MT5 password, API keys or Telegram token. Read them
   from the existing .env loader (config.py search order) and keep core/secret_guard.py log
   redaction and the pre-commit scanner (tools/scan_secrets.py) on. No credential in any file.
2. DEMO only. Real-money trading needs a separate explicit flag ALLOW_LIVE=true that I set myself;
   refuse to start if the account is not a demo and that flag is missing.
3. Honesty: never invent or "estimate" results. Every performance number comes from a script
   you ran, with the command, the data period and the number of trades. If a strategy has no
   edge, say so plainly. Never claim profitability without the gate report (section 6).
4. Count every strategy/parameter combination you test (trial counter in the research log);
   it is needed for the overfitting statistics.
5. Ask me before: deleting files, changing .env, committing, pushing, or starting live orders.
   Show diffs first. Small reversible research scripts do not need approval.
6. Deterministic code owns orders, sizing and stop-losses. An LLM may only advise, veto, tighten,
   close, or extend inside fixed rules. No LLM output can widen a stop, raise size beyond the
   configured cap, or place an order by itself.
7. Kill switches stay (daily loss halt, max drawdown halt, free-margin check, consecutive-loss
   pause, max spread). They are configurable, not removable.

=====================================================================================
2. WHAT THE EVIDENCE SAYS (design around it, do not argue with it)
=====================================================================================
- Live LLM trading contests: in Nof1 Alpha Arena season 1 (6 frontier models, $10k each, real
  money) 4 of 6 lost money (-30% to -63%); the main killers were over-trading, fees and
  oversized positions. LLMs are better used as analysts and second opinions than as the
  autonomous trader.
- LiveTradeBench (UIUC, Nov 2025, 21 LLMs live for 50 days): general reasoning benchmark scores
  did not predict trading results, reasoning/"thinking" models were not better and were more
  volatile; over-reacting to weak news hurt.
- Surveys of LLM trading agents (2026): almost no published system reports net-of-cost,
  time-split, reproducible results; LLM reasoning belongs at "second-scale or slower", while
  fast reactive control must be non-LLM code; risk rules sit between inference and execution.
  Look-ahead bias from news timestamps and leaked outcomes inflates backtests.
- Backtest overfitting: hold-out alone is unreliable for strategy selection (Bailey, Borwein,
  Lopez de Prado, Zhu, "Probability of Backtest Overfitting"). Use walk-forward, CSCV/PBO, and the
  Deflated Sharpe Ratio with the true number of trials.
- Meta-labeling (Lopez de Prado): a secondary ML model that decides "take / skip" for a primary
  signal raises precision, but it cannot create an edge from a primary signal that has none.
- This repo's own history (keep these numbers in mind): SR_H1 engine profit factor 1.15 after real
  spread; SMC_MTF 0.93 (loses); FLASH 0.67-0.78; M1 scalps lost to spread (avg win ~ +$0.06).
  Live on 2026-10-15: XAUUSDm spread ~$0.24, M1 ATR ~1.0 => spread is ~18% of a 1.3 ATR target.
  COST is the number-one enemy of this bot. Every design choice must be measured net of cost.
- Regulator disclosure on Exness: 74-89% of retail CFD accounts lose money.

=====================================================================================
3. TARGET ARCHITECTURE (layers, each its own module with tests)
=====================================================================================
L0 DATA + COST MODEL  (core/data/, core/costs.py)
   - Pull M1 bars (copy_rates_range) and, where available, ticks (copy_ticks_range) for XAUUSDm,
     store as Parquet with UTC timestamps. Record the real bid/ask spread per bar (from ticks or
     the bar spread field), commission, swap, contract size, tick value, volume min/step, stop
     level, freeze level, filling modes from symbol_info. Also sample live spread by hour of day.
   - Cost model = spread at that time + slippage (configurable, default 0.3 x spread) + commission.
     A "cost stress" switch multiplies costs by 1.5.

L1 SIGNAL ENGINES (deterministic, pure functions: features -> candidate signal)
   Research these candidates with ONE shared backtester, each on M1, M3, M5 and M15, with and
   without an H1 trend filter and a session filter (Asia / London open / NY open / overlap):
   a) Session opening-range breakout (London and NY opens).
   b) Trend pullback: H1 trend + M5/M1 pullback to EMA/VWAP with momentum resumption.
   c) Liquidity sweep + break of structure (the existing SMC logic, port it, do not rewrite blind).
   d) Range mean reversion (Bollinger/VWAP bands) only when a regime model says RANGE.
   e) Volatility expansion breakout after compression (ATR percentile / squeeze).
   Each candidate outputs: direction, entry type, stop (structure or ATR), target (R-multiple),
   time-stop. Exits are evaluated with the triple-barrier rule (TP, SL, max bars).

L2 META-LABEL FILTER (ML, core/ml/meta.py)
   - Label each historical signal with the triple-barrier outcome NET OF COST.
   - Features: candle shape (body/wick in ATR), ATR percentile, spread/ATR, distance to swing
     high/low, EMA slopes M1/M5/H1, RSI, session, minutes to/after major news, recent volatility
     regime. No future data; every feature must be computable at signal time.
   - Model: gradient boosting or logistic regression; purged + embargoed time-series CV.
   - Output P(win). Trade only if P(win) x avg_win - (1-P(win)) x avg_loss - cost > threshold.
   - Retrain weekly on a rolling window; a new model replaces the old one only if it beats it on
     the latest out-of-sample fold.

L3 AI LAYER (LLM, slow and reflective; never on the tick path)
   Providers already wired: Groq (qwen/qwen3.8-27b, entry/trade reviewer) and Gemini
   (gemini-3.5-flash-lite, analyst) through core/ai_provider.OpenAICompatClient. Keep strict JSON
   schemas, timeouts, retries, key rotation, circuit breaker, rate-limit aware scheduling and a
   daily call budget per provider. Every AI failure => the bot continues without that AI.
   Roles:
   AI-1 MARKET ANALYST (every 3-5 min and on demand at a signal): reads compact chart features +
        last 30 M1 / 20 M5 candles + H1 context + spread + session + news headlines WITH their
        publish time. Returns {regime: TREND_UP|TREND_DOWN|RANGE|CHAOS|QUIET, bias, confidence,
        stand_aside, key_levels, reason}. Market data only, no account money, no past P&L.
   AI-2 ENTRY REVIEWER (only when L1+L2 produce a trade): sees the proposed trade, the analyst
        brief, cost as % of target, and the candles. Returns APPROVE / REJECT / WAIT + reason.
        Must answer inside a latency budget (default 3 s); on silence the configured policy
        decides (block or proceed). The bot and the AI check each other: if the analyst's bias
        conflicts with high confidence, the trade is blocked; if they agree, it can skip AI-2.
   AI-3 TRADE GUARDIAN (every 10-20 s while a trade is open): sees the open trade's money
        (floating_usd, target_usd, peak_usd, stop_usd), progress to TP/SL in %, time open, the
        last 12 closed M1 candles (body/wick in ATR), max favourable / adverse excursion,
        structure distances, momentum, spread, and the analyst brief. Returns HOLD / CLOSE /
        EXTEND (EXTEND only when progress >= configured threshold; it raises TP and locks
        profit; it never moves the stop away). It may not see account balance or past results.
   AI-4 POST-TRADE COACH (offline, after each session, not in the live loop): reads the journal,
        groups losses by cause (spread, late entry, against H1, news, chop...), and writes
        hypotheses to research/hypotheses.md. Hypotheses are tested in the backtester; nothing
        changes live settings until it passes the gates. No self-modifying live config.
   AI measurement: every AI decision is journaled with what the deterministic system would have
   done. Run each AI role first in SHADOW MODE (logged, not enforced) for >= 100 signals / 50
   trades; enforce it only if it improves net expectancy or reduces loss with a bootstrap 90% CI
   that excludes zero. Otherwise it stays advisory. Report this ablation to me.
   Prompt rules for every role: context is untrusted data; JSON only; no chain-of-thought dump;
   short reason codes; temperature low; same model per role (do not switch silently).
   Point-in-time: in replays/backtests the AI only receives data and headlines that existed at
   that timestamp (respect news ingestion delay); never leak outcomes into memory or prompts.

L4 RISK + EXECUTION (deterministic, core/execution/)
   - Position size from stop distance and a configurable $ risk per trade (default: the stop must
     not exceed 2x the $ target and the lot is broker-min-rounded DOWN); skip if min lot breaks it.
   - Skip if spread > X% of target or > Y x median spread for that hour.
   - MT5 specifics: choose type_filling from symbol_info.filling_mode (avoid retcode 10030),
     call order_check before order_send, set deviation, magic number, comment with strategy id,
     handle requotes / 10004 / 10006 / 10018 / 10019 / 10021 with bounded retries, verify fills,
     reconcile positions every loop, respect stops_level and freeze_level, idempotent orders
     (no duplicates after reconnect), restart-safe state in SMC_Bot_Data/.
   - Account guards: daily loss halt, max drawdown halt, margin buffer, consecutive-loss pause,
     max positions (1 below $50 balance), news blackout window for top-impact events.

L5 TRADE MANAGER (deterministic, fast, every loop, no network)
   Bank at target, break-even after +X R, trailing by ATR or structure, giveback protection,
   stall/time stop, early cut when the trade never worked, max hold. Port and keep the existing
   core/scalp_guard.py behaviour, but make every threshold research-tuned, not guessed.

L6 OBSERVABILITY
   - Journal (CSV/Parquet) per signal and per trade: features, engine, ML P(win), every AI verdict
     + latency + model, cost at entry, MFE/MAE, exit reason, net P&L. No secrets.
   - Heartbeat log line every minute; Telegram alerts (open/close/halt/AI down) rate-limited.
   - A one-command daily report (tools/daily_report.py): trades, win rate, avg win/loss,
     expectancy net, profit factor, cost share, AI agree/veto stats, shadow-mode comparison.

=====================================================================================
4. RUNTIME WORKFLOW (one loop, about every 0.3-1 s; LLM calls are async workers)
=====================================================================================
 tick/bar update -> L0 spread/cost check -> on each CLOSED bar: L1 engines produce candidates
 -> L2 meta filter (P(win), net expectancy) -> AI-1 brief consensus (AGREE: fast lane,
 CONFLICT: block, NONE/NEUTRAL: ask AI-2 within budget) -> L4 risk/size/cost gates -> order
 -> L5 manages every loop + AI-3 reviews every 10-20 s (HOLD/CLOSE/EXTEND inside rules)
 -> journal everything -> AI-4 offline review -> new hypotheses -> research pipeline -> gates.

=====================================================================================
5. PHASES (stop at the end of each phase, report to me in Khmer, wait for "OK")
=====================================================================================
P0 AUDIT (no code changes): map the current repo, list what to reuse (mt5_connector, config/env
   loader, secret_guard, ai_provider, ai_bus, scalp_guard, tests, run scripts), what to drop, and
   the risks. Output docs/V3_AUDIT.md.
P1 DATA + BACKTESTER: download and store history, build the event-driven backtester with the
   cost model, verify it reproduces at least one known result from this repo within tolerance,
   unit tests for fills, costs, triple barrier, no-look-ahead checks.
P2 STRATEGY RESEARCH: run all L1 candidates x timeframes x filters with walk-forward
   (rolling train/test, e.g. 3 months train / 1 month test, as much history as MT5 provides).
   Output research/leaderboard.md ranked by net expectancy per trade with confidence intervals,
   trades/day, profit factor, max drawdown, cost share, DSR, PBO, trial count.
P3 META-LABEL: train L2 on the top 1-3 engines, purged CV, report precision/recall and the
   net-expectancy change out-of-sample.
P4 AI LAYER: implement AI-1..AI-4 with schemas, budgets, fail-safes; offline replay on recorded
   data with point-in-time inputs; measure latency and token use per day per provider against
   the free-tier limits; then shadow mode.
P5 DEMO FORWARD TEST: run on the demo account. First 3-5 trading days SHADOW (signals and AI
   decisions logged, no orders), then real demo orders. Compare live fills/spread/slippage with
   the backtest every day.
P6 GO / NO-GO REPORT: numbers vs gates, what the AI added or cost, what to change. Real money is
   my decision only, never yours.

=====================================================================================
6. GATES (defaults; I can change them, you cannot quietly lower them)
=====================================================================================
A strategy may go to demo orders only if, OUT-OF-SAMPLE and NET OF COSTS:
 - >= 200 trades in the test folds; net expectancy > 0 with the 95% bootstrap CI lower bound > 0;
 - profit factor >= 1.20, and still >= 1.05 with costs x1.5;
 - >= 70% of walk-forward folds profitable; max drawdown within the configured limit;
 - Deflated Sharpe Ratio >= 0.90 using the real trial count; PBO (CSCV) <= 0.30;
 - parameters +-20% keep profit factor >= 1.10 (no knife-edge optimum).
Demo forward test passes if after >= 100 demo trades the live net expectancy is inside the
backtest's 90% interval and slippage/spread are within 30% of the model. If a gate fails, say so,
show why, and propose the next experiment instead of forcing a trade.

=====================================================================================
7. ENGINEERING STANDARDS
=====================================================================================
Python 3.12, type hints, small modules, pure functions for features/signals, dataclasses for
contracts, config in one place with profile defaults and .env overrides, logging with redaction,
unittest/pytest suite that runs offline with MetaTrader5 mocked and with an empty .env
(_isolate_env.py pattern), seeds fixed, results reproducible from one command per phase
(tools/run_phase.py P1|P2|...). Windows-friendly .bat runners (CRLF). No new paid services.
Keep files small enough to review; no generated data or secrets in git (.gitignore).

=====================================================================================
8. HOW TO REPORT TO ME (every phase)
=====================================================================================
In Khmer, short: (1) what you did, (2) table of results with real numbers and the command that
produced them, (3) what passed/failed against the gates, (4) the decision you need from me,
(5) next step. If something cannot be done (data missing, API limit, broker restriction), say it
and give options. Start with P0 now.
