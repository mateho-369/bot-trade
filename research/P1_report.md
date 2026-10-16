# P1 Report — SR_H1 baseline, shared v3 backtester

- Generated (UTC): 2026-10-16T06:34:34.733263+00:00
- Command: `python tools/run_phase.py P1 --fixture`
- Python: 3.11.2 · Linux-6.1.158+-x86_64-with-glibc2.36
- Data source: **SYNTHETIC FIXTURE (offline pipeline check -- NOT market data, no performance claim)**
- Bars: H1 240 (2026-09-28 00:00:00+00:00 -> 2026-10-07 23:00:00+00:00), H4 60
- Median spread used by cost model: 0.240 price units (point=0.01); slippage 0.3x spread, commission 0
- Signals: 5 · skipped (in-trade): 3 · skipped (bad stop): 0

## Results (triple-barrier, SL-first, next-bar-open entry)

| Metric | Net of cost | Costs x1.5 | Gross (no cost) |
|---|---|---|---|
| Trades | 2 | 2 | 2 |
| Win rate | 100.0% | 100.0% | — |
| Profit factor | inf (no losing trades) | inf (no losing trades) | — |
| Expectancy / trade | +1.4457 R | +1.4134 R | +1.5101 R |
| Expectancy 95% bootstrap CI | [+0.9731, +1.9182] R | — | — |
| Max drawdown | 0.00 R | 0.00 R | — |
| Total net | +2.89 R | +2.83 R | — |
| Total cost | 0.13 R (4.3% of gross profit) | 0.19 R | 0 |
| Trades/day | 1.92 | — | — |

## Reproduction check

NOT APPLICABLE: synthetic fixture. This run only proves the pipeline works end-to-end. The reproduction check must be run on Windows with real MT5 data (see docs/P1_WINDOWS_RUNBOOK.md).

## Gate status (P1)

- Backtester/cost/triple-barrier/no-look-ahead unit tests: see pytest output (tests/).
- SR_H1 gate evaluation belongs to P2 (walk-forward). Nothing here authorises demo orders.
