# ============================================================
#   rarc/mathlib.py
#   The mathematical core of the engine. Pure functions, no I/O,
#   no broker calls — fully unit-testable.
#
#   Contains:
#   - R-multiple statistics (expectancy, profit factor, streaks)
#   - Risk-of-ruin: analytic log-space diffusion bound + Monte Carlo
#     bootstrap verification
#   - `max_risk_fraction()` — bounded bisection solve for the largest
#     per-trade risk that keeps ROR under target
#   - Bayesian shrinkage for small-sample bucket statistics
# ============================================================

from __future__ import annotations

import math
import numpy as np
from dataclasses import dataclass, field


# ── BASIC TRADE STATISTICS ───────────────────────────────────

def expectancy_r(r_multiples: np.ndarray) -> float:
    """Mean R-multiple. Positive edge = > 0."""
    r = np.asarray(r_multiples, dtype=float)
    return float(r.mean()) if len(r) else 0.0


def profit_factor(profits) -> float:
    """
    PF = gross profit / |gross loss|. Structural convention: a bucket
    with zero losses returns inf (capped), zero trades returns 0.
    """
    p = np.asarray(profits, dtype=float)
    gp = float(p[p > 0].sum())
    gl = float(-p[p < 0].sum())
    if gl <= 0:
        return 999.0 if gp > 0 else 0.0
    return gp / gl


def max_consecutive_losses(outcomes) -> int:
    """
    Absolute worst-case losing streak in an outcome sequence.
    `outcomes`: iterable of 1/True (win) and 0/False (loss).
    Exact scan — O(n), no sampling, no binning: the whole point of
    Module 1 is that this number is measured, never estimated.
    """
    worst = run = 0
    for o in outcomes:
        if not o:            # loss
            run += 1
            if run > worst:
                worst = run
        else:
            run = 0
    return int(worst)


def expected_max_streak(n_trades: int, loss_rate: float) -> float:
    """
    E[max losing streak] ≈ log(n) / log(1/loss_rate)  (i.i.d. approx).
    Used as a sanity cross-check against the OBSERVED streak — if the
    observed streak is far above this, the trades are autocorrelated
    (regime clustering) and the safety factor earns its keep.
    """
    if n_trades <= 0:
        return 0.0
    lr = min(max(loss_rate, 1e-9), 1 - 1e-9)
    return math.log(n_trades) / math.log(1.0 / lr)


def streak_probability(n_trades: int, loss_rate: float, streak: int) -> float:
    """
    P(at least one run of >= `streak` losses in `n_trades` i.i.d. trades)
    ≈ 1 - (1 - loss_rate^streak)^(n - streak + 1).
    Used to report "the streak you saw was X% likely — not a fluke".
    """
    if streak <= 0 or n_trades < streak:
        return 0.0
    lr = min(max(loss_rate, 1e-9), 1 - 1e-9)
    per_window = lr ** streak
    windows = max(1, n_trades - streak + 1)
    return float(1.0 - (1.0 - per_window) ** windows)


# ── RISK OF RUIN ─────────────────────────────────────────────

@dataclass
class RuinAnalysis:
    """Full diagnostics of a risk-cap solve. Everything is bounded."""
    r_cap_ror: float          # risk fraction satisfying the ROR target
    r_cap_streak: float       # risk fraction surviving the planned streak
    r_cap: float              # min(r_cap_ror, r_cap_streak) — the cage
    ror_at_cap: float         # analytic ROR evaluated at the cap
    ror_mc_at_cap: float      # Monte Carlo ruin probability at the cap
    observed_streak: int      # measured from history (Module 1)
    planned_streak: int       # safety-factored design streak
    expectancy_r: float       # edge per 1R at f=... (independent of f)
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    n_trades: int
    notes: list = field(default_factory=list)


def _log_stats(r_multiples: np.ndarray, f: float):
    """
    Per-trade log-equity statistics at risk fraction f.
    equity *= (1 + f * R_i) each trade → i.i.d. log increments.
    Returns (mu, sigma2, valid) — valid=False if any increment <= -1
    (i.e. f would allow a wipe-out trade); then risk is unbounded.
    """
    inc = np.log1p(f * r_multiples)
    if np.any(inc <= -np.inf) or np.any(np.isnan(inc)):
        return 0.0, 0.0, False
    return float(inc.mean()), float(inc.var()), True


def ror_analytic(r_multiples: np.ndarray, f: float,
                 ruin_dd: float = 0.30) -> float:
    """
    Probability of EVER touching a `ruin_dd` fractional drawdown, for
    fixed-fractional sizing at f, under the classic diffusion (first
    passage) approximation for a biased random walk in log-equity:

        R ≈ exp( -2 * mu * D / sigma^2 ),  D = ln(1 - ruin_dd)

    with mu/sigma the per-trade drift/variance of log-equity. This is
    the fixed-fractional form of the textbook ((1-A)/(1+A))^N formula
    (A = edge, N = capital units) — see Kaufman / Balsara literature —
    generalized to unequal win/loss payoffs via the actual empirical
    R-multiple distribution instead of an even-money assumption.

   .mu <= 0 (no edge) → ROR = 1.0 (ruin is certain; size must be 0).
    """
    if len(r_multiples) == 0 or f <= 0:
        return 1.0
    mu, var, valid = _log_stats(np.asarray(r_multiples, float), f)
    if not valid:
        return 1.0
    if mu <= 0:
        return 1.0
    if var <= 1e-12:                       # degenerate: all trades identical
        return 0.0 if mu > 0 else 1.0
    D = math.log(max(1e-12, 1.0 - ruin_dd))   # negative distance to barrier
    return float(math.exp(2.0 * mu * D / var))


def ror_monte_carlo(r_multiples: np.ndarray, f: float,
                    ruin_dd: float = 0.30, horizon: int = 3000,
                    n_paths: int = 4000, seed: int = 42) -> float:
    """
    Bootstrap the EMPIRICAL R distribution (keeps fat tails and any
    discreteness the analytic diffusion smooths away) and measure the
    finite-horizon fraction of paths that touch the ruin drawdown.
    Finite horizon ⇒ this under-estimates the infinite-horizon analytic
    bound; we take max(analytic, MC) as the operative number.
    """
    if len(r_multiples) == 0 or f <= 0:
        return 1.0
    rng = np.random.default_rng(seed)
    r = np.asarray(r_multiples, float)
    ruined = 0
    for _ in range(n_paths):
        path = r[rng.integers(0, len(r), size=horizon)]
        eq = np.cumprod(1.0 + f * path)
        peak = np.maximum.accumulate(np.maximum(eq, 1.0))
        dd = eq / peak - 1.0
        if dd.min() <= -ruin_dd:
            ruined += 1
    return ruined / n_paths


def max_risk_fraction(r_multiples, target_ror: float = 0.0001,
                      ruin_dd: float = 0.30,
                      observed_streak: int = 0,
                      streak_safety: float = 1.5,
                      streak_pad: int = 2,
                      streak_max_dd: float = 0.12,
                      f_min: float = 0.0005, f_max: float = 0.05,
                      mc_verify: bool = True, seed: int = 42,
                      mc_horizon: int = 3000, mc_paths: int = 4000,
                      ) -> RuinAnalysis:
    """
    THE BOUNDED SOLVER. Returns the largest per-trade risk fraction f*
    such that BOTH:

      (1) P(ever touch ruin_dd drawdown) <= target_ror   (analytic,
          verified by Monte Carlo on the empirical distribution)
      (2) planned_streak consecutive losses lose <= streak_max_dd,
          where planned_streak = ceil(observed*1.5) + 2
              → f <= 1 - (1 - streak_max_dd)^(1/S_plan)

    Monotone bisection: ROR(f) is increasing in f, so the root is
    unique. `f_min` floor exists because below ~0.05% broker min-lot
    granularity makes sizing meaningless.
    """
    r = np.asarray(r_multiples, float)
    n = len(r)
    p_win = float((r > 0).mean()) if n else 0.0
    wins = r[r > 0]
    losses = r[r < 0]
    avg_w = float(wins.mean()) if len(wins) else 0.0
    avg_l = float(-losses.mean()) if len(losses) else 1.0
    e_r = float(r.mean()) if n else 0.0
    obs = int(observed_streak)
    planned = int(math.ceil(obs * streak_safety)) + streak_pad

    notes = []
    # Constraint (2): streak survival — closed form.
    if planned > 0:
        f_streak = 1.0 - (1.0 - streak_max_dd) ** (1.0 / planned)
    else:
        f_streak = f_max

    # No positive edge ⇒ the only safe risk is (almost) zero.
    if n == 0 or e_r <= 0:
        notes.append("expectancy <= 0 → risk floored (no measurable edge)")
        return RuinAnalysis(f_min, f_streak, f_min, 1.0, 1.0, obs, planned,
                            e_r, p_win, avg_w, avg_l, n, notes)

    # Constraint (1): monotone bisection on the analytic ROR.
    lo, hi = f_min, f_max
    if ror_analytic(r, lo, ruin_dd) > target_ror:
        notes.append("even f_min breaches ROR target → floored at f_min")
        f_ror = f_min
    elif ror_analytic(r, hi, ruin_dd) <= target_ror:
        f_ror = hi
    else:
        for _ in range(60):                     # 60 halves → exact to 1e-15
            mid = math.sqrt(lo * hi)            # geometric: f spans decades
            if ror_analytic(r, mid, ruin_dd) <= target_ror:
                lo = mid
            else:
                hi = mid
        f_ror = lo

    f_cap = max(f_min, min(f_ror, f_streak, f_max))

    ror_c = ror_analytic(r, f_cap, ruin_dd)
    ror_mc = (ror_monte_carlo(r, f_cap, ruin_dd, mc_horizon, mc_paths, seed)
              if mc_verify else float("nan"))
    if mc_verify and ror_mc > target_ror * 4:
        # Empirical distribution is meaner than the diffusion model
        # (fat left tail) → trust the simulation, ratchet down.
        notes.append(f"MC ROR {ror_mc:.5f} > 4x target — tightening cap")
        lo2, hi2 = f_min, f_cap
        for _ in range(24):
            mid = math.sqrt(lo2 * hi2)
            if ror_monte_carlo(r, mid, ruin_dd, mc_horizon,
                               max(500, mc_paths // 4), seed) <= target_ror * 4:
                lo2 = mid
            else:
                hi2 = mid
        f_cap = max(f_min, lo2)
        ror_c = ror_analytic(r, f_cap, ruin_dd)
        ror_mc = ror_monte_carlo(r, f_cap, ruin_dd, mc_horizon, mc_paths, seed)

    return RuinAnalysis(f_ror, f_streak, f_cap, ror_c, ror_mc, obs, planned,
                        e_r, p_win, avg_w, avg_l, n, notes)


# ── SMALL-SAMPLE SHRINKAGE ───────────────────────────────────

def shrunk_win_rate(wins: int, n: int, global_rate: float,
                    strength: float = 40.0) -> float:
    """
    Beta-Binomial shrinkage: bucket win rate pulled toward the global
    win rate with `strength` pseudo-trades. Prevents a 3-trade bucket
    at 0% WR from being declared "toxic hour" — the classic Module 1
    false positive. (Wilson intervals are the alternative; shrinkage
    is smoother for gating.)
    """
    if n <= 0:
        return global_rate
    w = strength * global_rate
    return (wins + w) / (n + strength)


def bucket_toxicity(wins: int, n: int, avg_win_r: float, avg_loss_r: float,
                    global_rate: float, strength: float = 40.0) -> float:
    """
    Shrunken Profit Factor for a structural bucket:
      PF ≈ p*·W̄ / ((1-p*)·L̄)   with p* the shrunk win rate.
    Conservative by construction: small buckets drift toward the
    global PF, only statistically real decay sinks below 1.0.
    """
    p = shrunk_win_rate(wins, n, global_rate, strength)
    w = max(avg_win_r, 1e-9)
    l = max(avg_loss_r, 1e-9)
    return (p * w) / ((1.0 - p) * l)
