# P2 v1 result checker (read-only)

## Windows usage — AFTER the existing P2 run finishes

Do not interrupt, pull into, or rerun the currently running job. Keep its
original report and CSVs together. Once it finishes, the local agent can
pull `arena/ba18f2e6-bot-trade` and run only:

```bat
venv\Scripts\python.exe tools\check_p2.py --leaderboard research\P2_leaderboard.md --trades-dir data\research
```

No MT5 connection, no orders, no AI calls, no data downloads, no writes.
Do not send `.env`, credentials, or large market-data files. Send the
leaderboard, checker output, original run command/code revision and data
manifest. CSVs are needed locally for the checker, not in Git.

Exit codes: **0** all v1 runner gates pass; **1** valid evidence with gate
failures (expected, not a program crash); **2** missing, malformed or
inconsistent evidence. All rows are validated before any PASS is printed.
`NO DATA` fails every gate. Missing CSVs for nonzero-trade rows are errors;
zero-trade rows ignore stale CSVs because the runner emits no empty CSV.
Only the plan v1 Markdown export format is supported.

## What is independently recomputed

The checker reads the original global trial counter, never resets or
increments it, and uses the same `summarise` and `deflated_sharpe_ratio`
functions as the frozen runner. CSV floats use round-trip parsing.
Recomputed quantities: trade count, win rate, net PF, net expectancy and
seed-42 bootstrap 95% CI, v1 max DD, cost share, trades/day, DSR, and the
profitable fraction of **nonempty** folds. It checks every corresponding
rounded leaderboard metric, fold count/net/profitable flag and the CSV's
engine/timeframe, direction, timestamps, finite returns and cost identity.
It does not sort the CSV (that would conceal corruption/change DD).

All eleven v1 gates are printed with PASS/FAIL and their evidence, including
both random-entry controls. Strict comparisons (`E[R] > 0`, CI lower >0,
`E[R] > control p95`) remain strict. There are no new configurable lower
thresholds.

## Information the existing exports do NOT contain

Stress trades, the CSCV block matrix, perturbation trades and full-precision
control distributions are not exported. Their values are read from the
leaderboard and labelled **runner-reported, rounded; not independently
recomputed**. At a rounding boundary the original runner failure reasons
are authoritative; the checker rejects contradictions outside the rounding
interval. For example stress PF 1.0499 is printed as 1.050 by v1 but must
remain FAIL. Missing/contradictory reasons cannot be treated as evidence.
No claim is made to reconstruct unexported precision from rounded text.

The original exports also cannot verify random seed count, complete source
bar date range, the code/data hash that produced each row, or trials from
prior research. Keep the original command, commit and manifest. The parsed
counter is v1's declared grid count, **not proof** of a complete cumulative
trial ledger. Fixture backtests in the tests are engineering checks only,
not real-data trials or evidence of edge.

## Frozen v1 semantics vs full section-6 certification

These are observations of the inherited code, NOT changes or a new plan:

- `folds_prof` excludes folds with zero trades; section 6 speaks of all
  walk-forward folds. The checker explicitly reports the v1 denominator.
- Perturbation PF is full-sample on the modal selected combo, not OOS.
  Passing it alone does not prove section-6 OOS parameter robustness.
- The random controls run over the full bar frame, not just OOS test folds,
  with modal session/time-stop, a target fallback of 1.5R (the OOS CSV has
  no `target_R` column), and a count target rather than guaranteed matched
  realised trade count. No per-seed realised counts are exported.
- The v1 DD formula starts the running peak at the first trade's equity,
  not at zero. The checker intentionally reproduces that formula.
- Train/test slicing uses entry time; a train trade exiting after the
  selection cutoff is not purged. The checker cannot certify point-in-time
  training outcomes from OOS-only CSVs.

Consequently a checker PASS means **parity with the v1 runner's gates**, not
independent section-6 certification or permission for demo/live orders.
If any real row passes v1, review these limitations before proposing P3 or
claiming that every original-spec gate is satisfied. Do not silently fix
the running experiment; any research semantic change needs owner/reviewer
approval and a new plan version with the cumulative trial count preserved.
The demo-forward gates remain NOT EVALUATED (no demo trade evidence).

## Offline regression proof

```sh
.venv/bin/python -m pytest tests/test_check_p2.py -q
```

Tests call the **unchanged** `run_p2` and `_write_leaderboard`:
- Existing short synthetic Parquet, all five engines on H1: empty-OOS
  gate/metric parity (fixture is shorter than the train window).
- Same seeded price fixture mapped to synthetic hourly bars over >12
  months, PULLBACK-H1 with nonempty OOS: bit-for-bit float equality of net
  E[R], PF, CI, DD, DSR and profitable-fold fraction after CSV round-trip;
  the failing gate list matches the runner exactly.
- Constructed synthetic exports: all gates PASS; report-only failures
  including rounding boundaries and NaN PBO; missing/corrupt evidence;
  and before/after file-byte checks proving the checker is read-only.

The integration controls use two seeds to test plumbing quickly; this does
NOT satisfy the real experiment's required 200 seeds. No performance claim.
No `--workers` feature was added. No engine, cost, backtester, grid, runner,
pre-registration, downloader or existing fixture was changed.
