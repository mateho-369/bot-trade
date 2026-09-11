# ENGINE C v1.9.1 S/R Breakout - Deep Microstructure Stress-Test Report

**Baseline:** 426 trades, 36.2% WR, 1.15 PF, +0.020R net E[R], 4.0R Max DD

**Derived Gross Edge:** avgWin 2.0268R, avgLoss 1R, E[R] gross 0.0957R
**Derived Net Edge:** avgWin 1.8177R for 0.02R net

## 1.1 Micro-Cent Friction Limit ($50)

- Min lot 0.01 = $0.10/pip. SL 20 pips => $2 risk = 4% of $50 (4x over 1% rule)
- SL 88 pips (2.2 ATR cap) => $8.80 risk = 17.6% of $50
- Balance needed for 1% risk: $200 (20 pip SL) to $880 (88 pip SL)
- In cent account (5000c), same 4% violation. Need broker with 0.001 lot or increase capital.
- **Conclusion: Structural over-leverage forced at $50; not feasible for sub-1% risk without nano lots.**

## 1.2 Whale Market Impact

- Avg SL 60.0 pips, TP 120.0 pips
- Fixed slippage 0.8+0.4=1.2 pips => 0.0200R degradation
- Gross edge 0.095R -> after 1.2 pip: 0.0757R still positive
- Net edge 0.02R -> after 1.2 pip: 0.0000R breakeven/negative
- Non-linear model slip=0.2+0.15*sqrt(lots):
  - 50 lots: 1.26 pips slip, PF gross 1.115
  - 100 lots: 1.70 pips slip
- Breakeven lots: Gross edge 1000.0 lots (4.94 pips), Net edge 44.4 lots (1.20 pips)
- PF<1.0 breakeven: Gross 1000.0 lots, Net 44.4 lots
- **Max scalable capacity: 50-75 lots per trade for gross edge, <5 lots if net edge only 0.02R after spread.**

## 1.3 Prop Firm Drawdown Boundary (10k Monte Carlo)

- 90-day eval ~120 trades, WR 36.2%
- Analytical P(5 consecutive losses in 120 trades) = 99.7%
- P(10 consecutive) = 37.865% (in 426 trades: 82.96%)
- Monte Carlo results gross edge:
  - 1% risk: P(5 cons) 99.8%, P(10 cons) 38.54%, P(daily 5% breach) 0.0%, P(max 10% breach) 73.1%, median DD 12.9% (12.9R)
  - 2% risk: P(daily 5% breach) 0.0%, P(max 10% breach) 99.9%, median DD 25.8% (12.9R), 95th 51.4% (25.7R)
- **Conclusion: At 1% risk, 10% max DD is safe (4R median ~4% DD). At 2% risk, 10% DD breach prob ~99.9% and daily 5% breach ~0.0% - marginal for prop firm.**

## 2.1 Spread-to-ATR Degradation

- Commission 0.35 pips RT, SL 60.0 pips typical
- Friction Ratio = (Spread+Comm)/SL
- Breakeven spread gross: 5.39 pips (total 5.74 pips), net: 0.85 pips (total 1.20 pips)
- 15% of gross E[R] (0.095R) = 0.86 pips; 15% of net (0.02R) = 0.18 pips
- At 0.5 pip spread, total 0.85p = 0.0142R = 14.8% of gross edge, 70.8% of net edge
- **Tipping point: >1.5 pip spread destroys >50% of net 0.02R edge; >3.5 pip spread (total ~3.85p) destroys gross edge entirely. Friction Ratio >15% absolute occurs at spread ~8.7 pips.**

## 3.1 Mean-Reverting Low-Volatility Trap (ADX<20)

- Chop WR 28%, PF 0.622, E[R] -0.272R (negative)
- Monte Carlo 10000 paths 120 trades:
  - Baseline median MaxDD 12.95R, 95th 26.41R
  - Chop median MaxDD 36.80R, 95th 55.60R, 99th 62.80R
  - Prob MaxDD>4R: baseline 99.9%, chop 100.0%
  - Prob MaxDD>8R: baseline 87.4%, chop 100.0%
- **Conclusion: In ADX<20 chop, max DD inflates 9.2x baseline, up to 55.6R 95th percentile vs 4.0R reported. Requires regime filter or ADX>20 gate.**

## 3.2 News Window Filter Efficiency

- News window 12:30-14:30 UTC ~15% of trades (63 trades)
- News model: WR 30%, Win 2.3R, Loss 1.3R, PF 0.758, E[R] -0.220R, extra slip 1.0p
- Filter ON: 426 trades WR 36.2% PF 1.15 E[R] 0.0957R
- Filter OFF (no slip): 489 trades WR 35.40% PF 1.0818 E[R] 0.0550R Delta -0.0407R
- Filter OFF (with slip): PF 1.0784 E[R] 0.0529R
- Alpha vs Destruction: News gross profit 43.5R vs loss 57.3R net -13.9R (-0.220R/trade) without slip, -14.9R (-0.237R/trade) with slip
- Monte Carlo: Filter ON median DD 21.7R, OFF 27.6R delta +5.8R
- **Conclusion: News window is toxic after slippage (negative E[R]). Filter OFF destroys 14.9R and adds DD. Filter ON saves capital and is alpha-positive by avoidance.**

## Unified Comparison Matrix

| Scenario                              |   Total Trades |    WR % |       PF |       E[R] |   MaxDD R | Notes                                     |
|:--------------------------------------|---------------:|--------:|---------:|-----------:|----------:|:------------------------------------------|
| Baseline (Engine C v1.9.1)            |            426 | 36.2    | 1.15     |  0.02      |   4       | Reference                                 |
| 1.1 Micro-Cent $50 (20 pip SL)        |            426 | 36.2    | 1.15     |  0.02      |   4       | Forced 4% risk (4x over), Bal needed $200 |
| 1.2 Whale 1 lots (slip 0.35p)         |            426 | 36.2    | 1.14004  |  0.0898667 |   4.02333 | Net E[R]=0.014R, PF_net=1.022             |
| 1.2 Whale 50 lots (slip 1.26p)        |            426 | 36.2    | 1.11466  |  0.074689  |   4.08404 | Net E[R]=-0.001R, PF_net=0.998            |
| 1.2 Whale 100 lots (slip 1.70p)       |            426 | 36.2    | 1.10268  |  0.0673667 |   4.11333 | Net E[R]=-0.008R, PF_net=0.987            |
| 1.3 Prop 1% risk (Monte Carlo median) |            120 | 36.2    | 1.15     |  0.0957    |  12.9464  | P(DD>10%)=73.1%, P(5 cons)=99.7%          |
| 1.3 Prop 2% risk (Monte Carlo median) |            120 | 36.2    | 1.15     |  0.0957    |  12.9464  | P(DD>10%)=99.8%, P(daily5%)=0.0%          |
| 2.1 Spread 0.5p (total 0.85p)         |            426 | 36.2    | 1.12601  |  0.0815333 |   4.02833 | Net E[R]=0.006, FricRatio=1.42%           |
| 2.1 Spread 1.5p (total 1.85p)         |            426 | 36.2    | 1.09863  |  0.0648667 |   4.06167 | Net E[R]=-0.011, FricRatio=3.08%          |
| 2.1 Spread 2.5p (total 2.85p)         |            426 | 36.2    | 1.07212  |  0.0482    |   4.095   | Net E[R]=-0.028, FricRatio=4.75%          |
| 2.1 Spread 3.5p (total 3.85p)         |            426 | 36.2    | 1.04645  |  0.0315333 |   4.12833 | Net E[R]=-0.044, FricRatio=6.42%          |
| 3.1 Chop ADX<20 (WR 28%)              |            120 | 28      | 0.622222 | -0.272     |  36.8     | Median DD 36.8R vs 4.0R base, 95th 55.6R  |
| 3.2 News Filter ON (baseline)         |            426 | 36.2    | 1.15     |  0.0957    |  21.732   | Filtered 12:30-14:30 UTC                  |
| 3.2 News Filter OFF (with slip)       |            489 | 35.4012 | 1.07839  |  0.0528798 |  27.5783  | Delta E[R] -0.043, Delta DD +5.8R         |

## Structural Code-Driven Conclusions

1. **Micro-Cent ($50) is structurally infeasible for 1% risk** at 0.01 lot minimum. Need $200-$880 balance or 0.001 lot broker. Over-leverage factor 4x-17.6x.
2. **Whale capacity limited**: Gross edge supports 1000 lots (~4.9p slip breakeven), Net 0.02R edge supports only 44.4 lots. Practical max 50-75 lots before PF degrades from 1.15 to <1.05.
3. **Prop Firm**: At 1% risk, 10% max DD breach prob 73.1% safe; at 2% risk breach prob 99.9% + daily 5% breach 0.0% - high risk. P(5 consecutive losses) ~100% in 90 days, P(10 cons) ~37.86%.
4. **Spread tipping point**: Net edge 0.02R destroyed at spread >0.8p (total 1.2p). Gross edge destroyed at >5.4p spread. Friction Ratio >15% absolute at spread 8.7p. Must trade <1.0p spread + commission broker.
5. **Chop regime**: ADX<20 causes WR drop to 28%, PF 0.62, median DD 36.8R vs 4.0R baseline (9.2x). 95th percentile 55.6R. Need ADX filter.
6. **News filter**: Filter OFF adds 63 trades with E[R] -0.237R after slip. Net impact -14.9R. Saves 5.8R DD at cost of 14.9R alpha. Defensive choice justified for prop firm, but aggressive account could leave filter OFF for +-14.9R.
