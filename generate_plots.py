import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Baseline
wr=0.362
pf=1.15
avg_loss=1.0
avg_win_gross = pf*(1-wr)/wr
avg_win_net = (0.02 + (1-wr))/wr
avg_sl=60.0
commission=0.35

def calc_metrics(avg_win, slip):
    slip_R = slip/avg_sl
    adj_win = max(avg_win - slip_R, 0.01)
    adj_loss = avg_loss + slip_R
    pf_adj = (wr*adj_win)/((1-wr)*adj_loss)
    er_adj = wr*adj_win - (1-wr)*adj_loss
    return pf_adj, er_adj

# Spread degradation
spreads = np.arange(0.5, 4.0, 0.1)
total_frics = spreads + commission
pf_gross = []
er_gross = []
er_net = []
pf_net = []
for tf in total_frics:
    pg, eg = calc_metrics(avg_win_gross, tf)
    pn, en = calc_metrics(avg_win_net, tf)
    pf_gross.append(pg)
    er_gross.append(eg)
    pf_net.append(pn)
    er_net.append(en)

plt.figure(figsize=(10,6))
plt.plot(spreads, er_gross, label='E[R] gross (0.095R base)', linewidth=2)
plt.plot(spreads, er_net, label='E[R] net (0.02R base)', linewidth=2)
plt.axhline(0, color='red', linestyle='--', label='Breakeven')
plt.axvline(0.85, color='orange', linestyle=':', label='Net breakeven 0.85p spread')
plt.axvline(5.39, color='purple', linestyle=':', label='Gross breakeven 5.39p')
plt.xlabel('Spread (pips) - commission extra 0.35p not included in x')
plt.ylabel('Expected Return per trade (R)')
plt.title('Spread-to-ATR Degradation: E[R] vs Spread (SL=60 pips)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('spread_degradation.png', dpi=150)
print("Saved spread_degradation.png")

plt.figure(figsize=(10,6))
plt.plot(spreads, pf_gross, label='PF gross', linewidth=2)
plt.plot(spreads, pf_net, label='PF net', linewidth=2)
plt.axhline(1.0, color='red', linestyle='--', label='PF=1.0 breakeven')
plt.xlabel('Spread (pips)')
plt.ylabel('Profit Factor')
plt.title('Profit Factor vs Spread')
plt.legend()
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('pf_vs_spread.png', dpi=150)
print("Saved pf_vs_spread.png")

# Whale slippage
lots = np.arange(1, 200, 1)
def slip_model(l):
    return 0.2 + 0.15*np.sqrt(l)

slips = slip_model(lots)
pf_gross_lots = []
er_gross_lots = []
er_net_lots = []
for s in slips:
    pg, eg = calc_metrics(avg_win_gross, s)
    pn, en = calc_metrics(avg_win_net, s)
    pf_gross_lots.append(pg)
    er_gross_lots.append(eg)
    er_net_lots.append(en)

plt.figure(figsize=(10,6))
plt.plot(lots, er_gross_lots, label='E[R] gross', linewidth=2)
plt.plot(lots, er_net_lots, label='E[R] net', linewidth=2)
plt.axhline(0, color='red', linestyle='--')
plt.xlabel('Lot size (lots per trade)')
plt.ylabel('E[R] (R)')
plt.title('Whale Market Impact: E[R] vs Lot Size (slip=0.2+0.15*sqrt(lots))')
plt.legend()
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('whale_impact.png', dpi=150)
print("Saved whale_impact.png")

plt.figure(figsize=(10,6))
plt.plot(lots, slips)
plt.xlabel('Lots')
plt.ylabel('Total Slippage (pips)')
plt.title('Non-linear Slippage Model')
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('slippage_model.png', dpi=150)
print("Saved slippage_model.png")

# Prop firm drawdown distribution - simulate quickly
np.random.seed(42)
n_paths=10000
n_trades=120
outcomes = np.random.binomial(1, wr, size=(n_paths, n_trades))
r_pnl = np.where(outcomes==1, avg_win_gross, -1.0)
equity = np.cumsum(r_pnl, axis=1)
equity0 = np.hstack([np.zeros((n_paths,1)), equity])
peak = np.maximum.accumulate(equity0, axis=1)
dd = peak - equity0
max_dd = np.max(dd, axis=1)

plt.figure(figsize=(10,6))
plt.hist(max_dd, bins=50, alpha=0.7, edgecolor='black')
plt.axvline(4.0, color='red', linestyle='--', label='Baseline reported 4.0R')
plt.axvline(np.median(max_dd), color='orange', linestyle='--', label=f'Median {np.median(max_dd):.1f}R')
plt.xlabel('Max Drawdown (R)')
plt.ylabel('Frequency')
plt.title(f'Max Drawdown Distribution - 10k Monte Carlo paths ({n_trades} trades, WR 36.2%)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('dd_distribution.png', dpi=150)
print("Saved dd_distribution.png")

# Chop vs baseline
chop_wr=0.28
chop_win=1.6
outcomes_chop = np.random.binomial(1, chop_wr, size=(n_paths, n_trades))
r_chop = np.where(outcomes_chop==1, chop_win, -1.0)
eq_chop = np.cumsum(r_chop, axis=1)
eq_chop0 = np.hstack([np.zeros((n_paths,1)), eq_chop])
peak_chop = np.maximum.accumulate(eq_chop0, axis=1)
dd_chop = peak_chop - eq_chop0
max_dd_chop = np.max(dd_chop, axis=1)

plt.figure(figsize=(10,6))
plt.hist(max_dd, bins=50, alpha=0.5, label='Baseline WR 36.2%', edgecolor='black')
plt.hist(max_dd_chop, bins=50, alpha=0.5, label='Chop WR 28%', edgecolor='black')
plt.axvline(4.0, color='red', linestyle='--', label='Reported 4.0R')
plt.xlabel('Max Drawdown (R)')
plt.ylabel('Frequency')
plt.title('Drawdown: Baseline vs Chop (ADX<20)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('chop_dd_comparison.png', dpi=150)
print("Saved chop_dd_comparison.png")
