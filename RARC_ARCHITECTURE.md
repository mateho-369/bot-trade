# RARC — Regime Adaptation & Risk Correction Engine

**Architectural blueprint for a mathematically bounded, non-overfitting, continuous
learning loop.** Built for the BOSS SMC bot (`main.py` v1.9.2), MT5-connected.

> **Prime directive — what RARC is NOT.** RARC contains **zero directional price
> prediction**. It never predicts entries or exits. It learns **where the strategy
> dies**: which hours, weekdays, friction levels, and live failure sequences have
> historically destroyed expectancy. Its outputs are only ever one of three things:
>
> 1. **a smaller `Risk_Per_Trade`** (bounded below the baseline, never above),
> 2. **a BLOCKED entry** (it can never force or open a trade),
> 3. **a re-scaled SL/TP distance factor** (bounded to [0.75×, 1.50×]).
>
> Long-term backtest history is used **strictly** to extract statistical risk
> boundaries, blindspots, and behavioral safety limits — never to memorize price
> paths. Recent live history is used **strictly** to detect whether the current
> regime matches a known failure structure — never to forecast direction.

---

## 1. System map

```
                                ┌──────────────────────────────────────────────┐
                                │                main.py  (1s loop)            │
                                │                                              │
   MT5 candles ──► smc/sr engines ──► signal ──► execute_trade()               │
                                                    │                          │
                                                    ▼                          │
                                     ┌──────────────────────────┐               │
                                     │  RarcEngine.pre_trade_   │               │
                                     │  gate()   (engine.py)    │               │
                                     └────────┬─────────────────┘               │
            ┌──────────────┬─────────────────┼──────────────────┐              │
            ▼              ▼                 ▼                  ▼              │
   ┌───────────────┐ ┌───────────┐ ┌──────────────────┐ ┌────────────────┐     │
   │ 1 COOLDOWN    │ │ 2 FRICTION│ │ 3 BLACKLIST      │ │ 4 SMART GATE   │     │
   │ hard gate     │ │ gate      │ │ MATRIX blindspots│ │ (walk-forward  │     │
   │ (Module 4)    │ │ (Module 2)│ │ (Module 1)       │ │  ML, Module 3) │     │
   │ fail-CLOSED   │ │ fail-CLOSED│ │ fail-OPEN       │ │ fail-OPEN      │     │
   └───────┬───────┘ └─────┬─────┘ └────────┬─────────┘ └───────┬────────┘     │
           │               │                │                   │              │
           │  ANY BLOCK → HOLD (nothing below runs)                               │
           ▼                                                                     │
   ALL PASS → GateResult(allowed=True,                                           │
              risk_pct   = ROR cap × cooldown tier)   ──► calc_lot_size()        │
              sl/tp_mult = ATR regulator factor)      ──► SL/TP distances        │
                                                                                   │
   order filled ──► rarc.on_fill()          (slippage sample → Module 2)           │
   trade closed ──► rarc.on_trade_closed()  (streak feed  → Module 4)             │
   every tick   ──► rarc.run_due_maintenance()                                    │
                     ├─ hourly : friction window + ATR regulator (Module 2)       │
                     ├─ daily  : 00:00 UTC walk-forward retrain    (Module 3)     │
                     └─ 15-min : shadow-trade self-audit           (Module 3)     │
                                                                                  │
   OFFLINE (you, monthly/weekly):                                                 │
   multi-year backtest ──► boundaries.build_matrix() ──► risk_blacklist_matrix.   │
                            (Module 1)                  json ◄── loaded by 3      │
```

**Gate precedence is cheap → expensive, hard → statistical.** The cooldown matrix
is a dict lookup; the ML gate is the only O(forest) step, and it runs last so a
blocked-by-cooldown signal never burns inference time.

**Fail-closed vs fail-open is deliberate:**
| Gate | Failure behavior | Why |
|---|---|---|
| Cooldown, Friction | fail-**CLOSED** (block) | Safety must not vanish with a missing file |
| Blindspot matrix, Smart gate | fail-**OPEN** (allow) | Missing *statistics* must not silence the bot; the upstream hard gates still run |

---

## 2. Module 1 — Historical Risk Boundary & Blindspot Extraction
`rarc/boundaries.py`, `rarc/mathlib.py` · offline, re-run ≤ 1×/day

### 2.1 Max Consecutive Losses → Risk-of-Ruin-bounded `Risk_Per_Trade`

The scanner walks the **entire** multi-year outcome sequence exactly (no sampling,
no binning — the worst streak is *measured*, not estimated). Then three independent
mathematical constraints bound the risk fraction `f`:

**Constraint A — Analytic Risk of Ruin (first-passage/diffusion bound).**
For fixed-fractional sizing, log-equity performs a biased random walk with per-trade
drift μ and variance σ² of `ln(1 + f·Rᵢ)` over the empirical R-multiple distribution.
The probability of *ever* touching a fractional drawdown barrier `z`:

```
ROR(f, z) ≈ exp( −2·μ·ln(1−z) / σ² )          (μ > 0 required)
```

This is the generalized form of the textbook `ROR = ((1−A)/(1+A))^N` (A = edge,
N = capital units = 1/f) — but it accepts **unequal win/loss payoffs** via the real
empirical R distribution instead of assuming even-money bets. `f` is solved by
monotone bisection so that `ROR ≤ 0.0001` (0.01%, the target in the spec) at
`z = MAX_DRAWDOWN_PERCENT` (30%).

**Constraint B — Monte Carlo verification.** The analytic bound is a diffusion
*approximation*; real trade distributions are fat-tailed and discrete. The engine
bootstraps the empirical R-multiples (4,000 paths × 3,000 trades) and measures the
fraction of paths touching the ruin barrier at the proposed cap. If empirical ROR
exceeds 4× the target, the cap is re-solved against the simulation. Take
`max(analytic, MC)` — conservatism compounds.

**Constraint C — Streak survival (closed form).** The observed worst streak `S_obs`
is a *sample*, not a law. Design streak `S_plan = ceil(1.5·S_obs) + 2`, then:

```
f  ≤  1 − (1 − streak_max_dd)^(1/S_plan)      (streak_max_dd = 12%)
```

An i.i.d. cross-check `E[max streak] ≈ ln(n)/ln(1/loss_rate)` is exported so you can
see whether the observed streak is above or below pure-chance expectation (above ⇒
autocorrelation ⇒ the safety factor is earning its keep).

**Final:** `Risk_Per_Trade_cap = clip(min(f_A, f_B, f_C), 0.10%, baseline)`.

> Worked example from the bundled demo (6,000 synthetic trades, 2 years):
> observed streak 13 (i.i.d. expectation ≈ 16.5) → S_plan 22 → constraints solve to
> a **0.28% cap** vs the 1.0% baseline, with analytic ROR at cap ≈ 2×10⁻⁷.

### 2.2 Market Regime Blindspots → `risk_blacklist_matrix`

Automated failure-correlation parser over **structural features only**:

- **Toxic Hours of Day (UTC)** — per-hour shrunk Profit Factor.
- **Toxic Days of Week** — same, per weekday.
- **Maximum Bid-Ask Spread Tolerance** — trades sorted by recorded spread;
  candidate thresholds walked upward across percentiles (50th→99th); the **highest**
  threshold whose subset PF still ≥ 1.0 is the tolerance. The first threshold where
  PF < 1.0 is exported as `friction_decay_spread_pct`.

**Shrinkage is the anti-overfitting device here.** Raw bucket PF on small samples is
a false-positive factory (3 midnight losses ⇒ "hour 2 is cursed"). Each bucket's win
rate is pulled toward the global win rate with a Beta prior of `SHRINKAGE_STRENGTH`
pseudo-trades:

```
p* = (wins + m·p_global) / (n + m),    PF* ≈ p*·W̄ / ((1−p*)·L̄)
```

A bucket is enforceable toxic only when `PF* < 1.0` **and** `n ≥ 30`. The exported
matrix (`risk_blacklist_matrix.json`) is versioned, timestamped, and carries the
full n/PF table so every block is auditable after the fact.

---

## 3. Module 2 — Hourly Market Friction & Volatility Tracker
`rarc/friction.py` · runs every 60 min + cheap per-fill sampling

### 3.1 FrictionTracker — trailing 3h spread & slippage + strict gate

- Samples `(timestamp, spread_pct, slippage)` into a **bounded deque**
  (`maxlen=400` — memory can never grow, see §8) and an append-only JSONL with
  size rotation.
- `stats()` computes p50/p90/p95/max over the trailing 3h window; the gate uses
  **p95** by default (robust to a single anomalous tick, sensitive to a real storm).
- **Gate:** if window-p95 spread > matrix tolerance ⇒ `trading_allowed = False`
  immediately. Re-opening requires the storm to age out of the 3h window **plus**
  `FRICTION_REOPEN_STREAK = 2` consecutive clean hourly checks (**hysteresis** —
  a single calm hour must not re-arm the bot mid-regime-change).
- Gate state persists; a restart during a friction storm stays closed.

### 3.2 ATRRegulator — hourly SL/TP distance recalibration

Every hour: `vol_ratio = live_ATR / median_ATR(30d)`, multiplier `= clamp(vol_ratio, 0.75, 1.50)`.

- **Reference is the 30-day median**, not a moving average — a median does not
  chase the news candle we are trying to measure.
- The **same** factor scales SL and TP distances ⇒ RR geometry is preserved; only
  the structural breathing room adapts. Bounded [0.75×, 1.50×]: adaptation can never
  degenerate into arbitrary stops.
- On the SR_H1 engine's **level-based** SLs the regulator is *not* applied to the
  level itself (the level is structural); only the RR-scaled TP breathes.

---

## 4. Module 3 — Daily Rolling Walk-Forward ML Filter
`rarc/walkforward.py` · trains 00:00 UTC daily, serves every signal

### 4.1 What it learns

`y = 1` if the trade **lost**. Features are structural execution metadata captured
at signal time — `hour_utc` (+ sin/cos cyclical encoding, since toxic hours cluster
across midnight and trees split linearly), `day_of_week`, `spread_pct`, `atr_pct`,
`trend_strength` (|close_vs_ema|), `session_active`, and `streak_state` (consecutive
losses *before* the trade — computed causally, no label leakage). **No price levels,
no direction targets.** A small, heavily-regularized `RandomForestClassifier`
(depth 4, min-leaf 10, class-weight balanced) — 150 trees train in well under a
second on a 30–60 day window, inside the 1s loop budget.

### 4.2 The four anti-overfitting tripwires

1. **Rolling window (30–60d).** Old regimes decay out of the sample by construction;
   the model cannot memorize a market that no longer exists.
2. **Purged + embargoed walk-forward CV.** Chronological folds; training rows inside
   a ±24h time-halo around each test block are removed — purging kills label-horizon
   overlap, embargo kills serial-correlation leakage (the López de Prado protocol).
   The reported AUC is an honest out-of-sample number, not a resampled fantasy.
3. **Deployment gate.** The new model replaces yesterday's only if
   `purged-CV AUC ≥ 0.55`. Otherwise the job logs "no learnable structure" and keeps
   the previous model. Noise never deploys. If no model exists, the gate is simply
   open — **the system degrades to Modules 1/2/4, never to nonsense.**
4. **Live self-audit.** Every blocked signal is recorded as a **shadow trade**
   (entry/SL/TP at block time) and resolved against actual candles 24h later.
   If the *blocked set* would have won > 55% of the time, the gate is deleting edge
   ⇒ threshold auto-relaxes (+0.10). A circuit breaker also suspends the gate 24h
   if it blocks > 60% of recent signals. The gate must continuously *prove itself*
   against live reality to keep its authority.

### 4.3 The prediction bridge ("Smart Gate Bypass")

For the next 24h (until the next 00:00 UTC retrain), every signal's metadata is
scored: `p_loss ≥ 0.65` ⇒ complete block of the entry. Idempotency: the job is
keyed by UTC date, so a crash-and-restart at 00:05 does not double-train; state
files make the whole thing replay-safe.

---

## 5. Module 4 — Automated Self-Correction / Cooldown Matrix
`rarc/cooldown.py` · updated on every resolved live trade

- Streak counting is **localized to a 12-hour rolling frame**: loss timestamps fall
  out of the window, so yesterday's bleed cannot suppress today's fresh regime —
  while an active cooldown keeps its full independent timer.
- **Tier ladder** (the "matrix"):

  | consecutive losses (12h) | risk multiplier | effective @1.0% baseline | action |
  |---|---|---|---|
  | 0–1 | ×1.00 | 1.00% | normal |
  | 2 | ×0.50 | 0.50% | downscale |
  | 3 | ×0.25 | 0.25% | defensive |
  | ≥4 | ×0.25 | 0.25% | **+ 12h operational cooldown** (hard gate) |

- **Recovery hysteresis:** after cooldown expiry or a win, the bot climbs back
  **one tier at a time**, requiring `RECOVERY_TRADES_PER_TIER = 2` resolved trades
  per tier. This kills the classic flapping oscillation
  (win → full risk → loss → panic) that naive streak logic produces.
- All state persists with timestamps — a cooldown survives a bot restart with its
  remaining time intact (verified by test).
- Final sizing: `risk_pct = clip(baseline × tier_mult × , floor, Module-1 cap)` —
  and `risk_manager.calc_lot_size(..., risk_pct=...)` hard-refuses any override
  above the configured baseline. Three independent layers would each have to fail
  for an oversized position to exist.

---

## 6. How it all connects to `main.py`

Four integration points, all soft-coupled (`try/except ImportError` — delete the
`rarc/` folder and the bot still runs its legacy stack):

1. **Singleton** (module scope): `rarc = RarcEngine(symbols=config.SYMBOLS)`,
   plus `_rarc_shadow_lookup()` — the M15 candle-replay resolver handed to the
   engine with `set_shadow_lookup()` for the Module-3 self-audit.
2. **`execute_trade()`** — immediately after the existing `MAX_SPREAD` filter:

   ```python
   rarc_gate = rarc.pre_trade_gate(symbol, spread_pct, hour_utc, day_of_week,
                                   atr_pct, trend_strength, session_active)
   if not rarc_gate.allowed:
       signal_log.log(f"{symbol}:rarc", f"● HOLD {symbol} — {rarc_gate.reason}")
       if smart_gate_blocked: rarc.on_blocked_signal(...)   # shadow trade
       return
   # pass → bounded numbers feed the order:
   sl = price − atr · decision['sl_mult'] · rarc_gate.sl_mult
   tp = price + atr · decision['tp_mult'] · rarc_gate.tp_mult
   lot = risk_manager.calc_lot_size(..., risk_pct=rarc_gate.risk_pct)
   ```

   Note what RARC did **not** touch: signal generation, direction, RR structure,
   cooldown bars, entry timing. The strategy stays exactly the strategy.
3. **Closed-trade path** (in `process_symbol_hf`, where outcomes resolve):
   `rarc.on_trade_closed(symbol, ticket, profit)` → Telegram alert on
   `cooldown_started` / tier changes.
4. **Main loop tick**: `rarc.run_due_maintenance(now, live_spread=...)` — usually a
   no-op; runs the hourly friction/ATR job, the 00:00 UTC retrain, and periodic
   shadow-trade resolution. Also `note_atr()` from the feature cache and
   `on_fill()` after each placement (slippage = |fill − requested|).

Interaction with the **existing** `ml/model.py` SMCModel: both coexist. The SMCModel
filters on signal-shape features (sweeps/BOS/RSI); RARC's gate filters on
*execution structure* (hour/day/friction/volatility/streak) and can only block.
RARC is the outer safety ring; the existing retraining loop is untouched.

---

## 7. Edge-case failure modes & their engineered answers

**7.1 Database / file locking (the classic 1s-loop vs 00:00-retrain collision).**
The legacy `TradeLogger.update_outcome()` does read-modify-write of the whole CSV
— a retrain reading mid-write sees a torn file; two writers can interleave.
Answers, in layers: (a) the daily job runs **under a cross-process advisory
`FileLock`** (`rarc/store.py`, fcntl/msvcrt, stale-lock auto-expiry so a killed
process can't wedge the bot); (b) all RARC state is written with
**atomic tmp+fsync+`os.replace()`** — a reader sees old or new, never a mix;
(c) the friction stream is **append-only JSONL** with rotation (appends never
rewrite); (d) the retrain trains on an in-memory snapshot, never on a live-open
handle. *Residual risk:* the legacy CSV itself remains non-atomic under a crash —
mitigation roadmap: move `trade_log.csv` to SQLite in WAL mode (single writer,
atomic commits, crash-safe) behind the same lock; the `store.py` primitives are
already the right shape for it.

**7.2 Memory leaks during daily retraining.** Three defenses: (a) **bounded
everything** — friction deque (400), shadow buffer (300), signal windows (500),
ATR history (720); nothing in RARC grows with uptime; (b) the retrain window is
hard-capped (30–60 days, `ML_MIN_TRADES ≤ n ≤ few thousand rows`) and the
DataFrames are explicitly dropped with `gc.collect()` in a `finally:` block;
(c) `TRAIN_IN_SUBPROCESS` flag in settings — flip to `True` to run the daily job
via `multiprocessing` (spawn), so even a pathological leak in sklearn cannot
accumulate across days; the parent gets back only the model file + diagnostics.
`n_jobs=1` on the forest avoids fork-storms inside the loop. Rule of thumb: a
depth-4 forest on ≤3,000 rows uses ~10–20 MB transiently — inline is safe, the
subprocess switch exists for paranoia.

**7.3 Clock & timezone hazards.** Everything internal is UTC; hour buckets are
UTC so backtest-extracted toxic hours and live checks agree regardless of the
VPS timezone. Broker "daily" rollovers (existing `RiskState`) remain local-date —
documented, unchanged. DST does not shift UTC hours (that's *why* the matrix
stores UTC, even though your broker's sessions shift).

**7.4 Cold start.** No matrix ⇒ blindspot gates open, friction tolerance falls
back to `config.MAX_SPREAD`, risk cap falls back to baseline (the ladder still
works — Module 4 needs no history). No WF model ⇒ smart gate open. Run
`engine.rebuild_matrix_from_backtest("multi_year_trades.csv")` once; the demo
(`rarc_demo.py`) shows the exact flow.

**7.5 Crash mid-write / mid-retrain.** Atomic writes leave either the old or new
file. Model writes keep a `.bak` rollback copy. The daily job is date-keyed ⇒
idempotent. A torn last line in the friction JSONL is tolerated on read.

**7.6 Degenerate/hostsile conditions.** Expectancy ≤ 0 in the history ⇒ risk
floored at 0.10% and a loud note in the matrix. Friction data stale ⇒ gate keeps
its last state. All-RF-errors ⇒ fail-open with error logs, never exception
propagation into the 1s loop (`run_due_maintenance` is catch-all). Circuit
breaker prevents a degenerate model from starving the strategy.

**7.7 Regime drift vs matrix staleness.** The matrix is deliberately slow
(14-day staleness flag). Fast structural change is Module 4's job (reacts in
minutes); the matrix only moves when *years* of evidence move.

---

## 8. Repository map of the deliverable

```
rarc/
  settings.py      every knob + the mathematical bounds, documented
  store.py         atomic JSON writes · cross-process FileLock · JSONL rotation
  mathlib.py       streaks · PF · ROR analytic+MC · shrinkage · bisection solver
  boundaries.py    MODULE 1  → risk_blacklist_matrix.json
  friction.py      MODULE 2  FrictionTracker + ATRRegulator
  walkforward.py   MODULE 3  purged WF training + prediction bridge + self-audit
  cooldown.py      MODULE 4  tier ladder + hysteresis recovery
  engine.py        RarcEngine façade — the ONLY class main.py touches
tests/test_rarc.py  32 synthetic-data tests (math, modules, integration)
rarc_demo.py        end-to-end demo on synthetic multi-year data (no MT5 needed)
```

**Run it:**
```bash
python -m pytest tests/test_rarc.py -v     # 32 tests
python rarc_demo.py                        # full pipeline walkthrough
```

**First production use:**
1. Produce a multi-year backtest trade log (columns: `time, outcome, profit,
   spread_pct[, symbol]`) — e.g. `run_backtest` over `get_candles_history(...)`
   in yearly chunks, concatenated.
2. `python -c "from rarc import RarcEngine; e = RarcEngine(); e.rebuild_matrix_from_backtest('backtest_trades.csv')"`
3. Start the bot. Everything else (hourly, daily, per-trade) is automatic.

---

## 9. Honest limitations (so the bounds stay honest)

- The diffusion ROR bound assumes i.i.d. trade outcomes; the Monte Carlo check
  keeps the empirical dependence but only over the sampled history horizon.
  The streak safety factor (×1.5, +2) is the insurance premium for that gap.
- Shadow trades use M15 replay: intrabar TP/SL ordering on the same candle is
  resolved **conservatively** (SL-first on ties), so the self-audit can only
  *understate* the gate's benefit — it can never flatter itself.
- Module 1 blindspots are computed per whole history; multi-symbol portfolios
  should build one matrix per symbol (pass `symbol=` and per-symbol CSVs) before
  enabling more instruments.
- A 30–60 day window is thousands of trades only for high-frequency engines. On
  slower strategies the model simply won't train (min-sample guard) — that is
  correct behavior, not a bug: no data, no gate.
