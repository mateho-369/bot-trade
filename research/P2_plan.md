# P2 Pre-Registration Plan v1 (written BEFORE any real-data P2 run)

Registered: 2026-10-16. Any change after seeing real results requires a
new plan version entry below; the trial counter NEVER resets.

## Data & execution (fixed for all combos)
- Symbol XAUUSDm only. Bars are BID; backtester = core/backtester_v3.py
  (next-bar-open entry, bid/ask barriers, gap-at-open, SL-first,
  triple barrier). Cost model (a): per-bar spread + slippage 0.3x,
  commission 0. Stress run: costs x1.5.
- Timeframes: M1, M5, M15, H1 (M1 reported with cost share; structurally
  hard: spread ~0.20 vs M1 ATR ~1).
- All session variants exclude 21:00-23:59 UTC (rollover spreads).

## Engines (base signal rules; see core/research/engines.py)
- ORB: London 08:00 / NY 13:00 UTC opening range (30 or 60 min), first
  close beyond OR edge within 2h, stop = opposite OR edge capped at
  1.5 ATR from signal close.
- PULLBACK: TF trend (close vs EMA50 + EMA50 slope>0), pullback touch
  of EMA20 within last 5 bars, resumption = close recrosses EMA20
  (variant: also RSI>50), stop = 5-bar extreme -/+ 0.25 ATR.
- SMC: port of core/smc_engine signals (sweep+BOS, EMA200 trend,
  vol filter); variant bos_only; stop = last swing -/+ 0.25 ATR.
- RANGE: only inside deterministic RANGE regime
  (|EMA50 slope| < 0.15 ATR/10 bars AND ATR percentile in [0.1, 0.9]);
  Bollinger(20,2) re-entry cross; stop = swing -/+ 0.5 ATR;
  target = distance to Bollinger midline (intrinsic, clipped 0.8-2R).
- SQUEEZE: ATR percentile <= threshold (10 or 20) within last 5 bars,
  then close breaks prior 20-bar extreme with body >= 0.5 ATR;
  stop = 1.5 ATR.

## Combo grid (per engine; every combo x timeframe = 1 trial)
Axes: session in {LIQUID_07_20, OVERLAP_13_17, MORNING_00_12},
H1-trend filter in {off, on} (PULLBACK: inherent, axis fixed off),
target_R in {1.5, 2.0} (RANGE: intrinsic target, axis absent),
primary variant x2 (ORB minutes, PULLBACK rsi gate, SMC bos_only,
RANGE z is fixed at re-entry z=2 band with variant = regime slope
0.15/0.25, SQUEEZE pctile 10/20).
Time stop (bars, by hold minutes / TF): ORB 180m, PULLBACK 360m,
SMC 360m, RANGE 360m, SQUEEZE 480m (min 3 bars).
Combo counts: ORB 24, PULLBACK 12, SMC 24, RANGE 12, SQUEEZE 24
= 96 combos per timeframe; x 4 timeframes = 384 trials maximum.
(Fewer if a subset is run; the leaderboard prints the real count.)

## Walk-forward & selection
- Rolling folds: 12 months train / 3 months test, step 3 months, over
  all available data. A combo is eligible on train only if it has
  >= 20 train trades; selection = highest train net E[R]. OOS = the
  selected combo's trades inside each test window (signals/features
  are causal; trades are partitioned by entry time).
- Per-fold and per-year tables, BUY/SELL split, trades/day, cost
  share, max DD, bootstrap 95% CI.

## Mandatory controls (same bars, exits, sessions, time stops)
(i) Random-entry: 200 seeds, same OOS trade count target and BUY/SELL
    mix, stop distances sampled from the engine's own risk/ATR ratios.
(ii) Random-LONG-only: 200 seeds, all BUY (gold ~3x since 2018).
An engine-TF counts only if OOS net E[R] > 95th percentile of BOTH.

## Statistics & gates (section 6, research defaults declared here)
DSR with the REAL global trial count; PBO via CSCV with 16 blocks;
+-20% perturbation of target_R on the modal selected combo
(PF must stay >= 1.10 on both sides). Max DD research limit: 25R.
PASS requires ALL of: OOS trades >= 200; net E[R] > 0 with 95% CI
lower bound > 0; PF >= 1.20 and >= 1.05 at costs x1.5; >= 70% of
folds profitable; max DD <= 25R; DSR >= 0.90; PBO <= 0.30;
perturbation PF >= 1.10; beats both random baselines' 95th pct.
It is acceptable that nothing passes.

## Outputs
research/P2_leaderboard.md (PASS/FAIL + reason per engine-TF),
data/research/{ENGINE}_{TF}_trades.csv (OOS trades, git-ignored).

## Plan change log
- v1 (2026-10-16): initial registration, before any real P2 run.
