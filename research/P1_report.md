# P1 Report — SR_H1 baseline, shared v3 backtester

- Generated (UTC): 2026-10-16T06:45:14.634581+00:00
- Command: `python tools/run_phase.py P1 --fixture`
- Python: 3.11.2 · Linux-6.1.158+-x86_64-with-glibc2.36
- Data source: **SYNTHETIC FIXTURE (offline pipeline check -- NOT market data, no performance claim)**
- Bars: H1 240 (2026-09-28 00:00:00+00:00 -> 2026-10-07 23:00:00+00:00), H4 60
- Median spread used by cost model: 0.240 price units (point=0.01); slippage 0.3x spread, commission 0
- Signals: 5 · skipped (in-trade): 3 · skipped (bad stop): 0
- Spread fallback (hour-median/default, never free): 0 trades

## Results (a) — per-bar real spread, bid/ask model (BUY ask-in/bid-out, SELL bid-in/ask-out, gaps at open, triple-barrier, SL-first, next-bar-open entry)

| Metric | Net of cost | Costs x1.5 | Gross (no cost) |
|---|---|---|---|
| Trades | 2 | 2 | 2 |
| Win rate | 100.0% | 100.0% | — |
| Profit factor | inf (no losing trades) | inf (no losing trades) | — |
| Expectancy / trade | +1.4607 R | +1.4374 R | +1.5219 R |
| Expectancy 95% bootstrap CI | [+0.9391, +1.9822] R | — | — |
| Max drawdown | 0.00 R | 0.00 R | — |
| Total net | +2.92 R | +2.87 R | — |
| Total cost | 0.12 R (4.0% of gross profit) | 0.18 R | 0 |
| Trades/day | 1.92 | — | — |

## Results (b) — duel methodology (fixed 0.30 spread, no slippage; compare with REAL_WORLD_TEST.md)

| Metric | Duel-method net |
|---|---|
| Trades | 2 |
| Win rate | 100.0% |
| Profit factor | inf (no losing trades) |
| Expectancy / trade | +1.4663 R |
| Expectancy 95% bootstrap CI | [+0.9327, +2.0000] R |
| Max drawdown | 0.00 R |
| Total net | +2.93 R |
| Total cost | 0.12 R |

### Splits — (a) per-bar real spread

| Group | Trades | WR | PF | E[R] net |
|---|---|---|---|---|
| BUY | 2 | 100.0% | inf (no losing trades) | +1.4607 R |
| Year 2026 | 2 | 100.0% | inf (no losing trades) | +1.4607 R |

### Splits — (b) duel fixed 0.30

| Group | Trades | WR | PF | E[R] net |
|---|---|---|---|---|
| BUY | 2 | 100.0% | inf (no losing trades) | +1.4663 R |
| Year 2026 | 2 | 100.0% | inf (no losing trades) | +1.4663 R |

## Reproduction check

NOT APPLICABLE: synthetic fixture. This run only proves the pipeline works end-to-end. The reproduction check must be run on Windows with real MT5 data (see docs/P1_WINDOWS_RUNBOOK.md).

## Gate status (P1)

- Backtester/cost/triple-barrier/no-look-ahead unit tests: see pytest output (tests/).
- SR_H1 gate evaluation belongs to P2 (walk-forward). Nothing here authorises demo orders.
