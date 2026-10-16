"""Rolling walk-forward selection for P2.

Folds: 12m train / 3m test, step 3m. Parameters are chosen on TRAIN
trades only (a combo needs >= 20 train trades to be eligible; score =
train net expectancy). Out-of-sample trades = the winning combo's
trades whose ENTRY time falls inside the test window. Signals and
features are causal by construction, so partitioning the full-sample
trade list by entry time does not leak future information into train
selection (the one-position busy state across a boundary is the only
approximation, and it is conservative).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Fold:
    idx: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp      # exclusive
    test_start: pd.Timestamp
    test_end: pd.Timestamp       # exclusive


def make_folds(start, end, train_months=12, test_months=3,
               step_months=3) -> list:
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    anchor = pd.Timestamp(year=start.year, month=start.month, day=1,
                          tzinfo=start.tzinfo)
    folds = []
    k = 0
    while True:
        tr_s = anchor + pd.DateOffset(months=step_months * k)
        tr_e = tr_s + pd.DateOffset(months=train_months)
        te_s = tr_e
        te_e = te_s + pd.DateOffset(months=test_months)
        if te_e > end + pd.Timedelta(days=31):
            break
        if tr_s >= end:
            break
        folds.append(Fold(k, tr_s, tr_e, te_s, te_e))
        k += 1
        if k > 200:
            break
    return folds


def _slice(trades: pd.DataFrame, a, b) -> pd.DataFrame:
    if trades is None or len(trades) == 0:
        return trades.iloc[0:0] if trades is not None else pd.DataFrame()
    t = pd.to_datetime(trades["entry_time"])
    return trades[(t >= a) & (t < b)]


def run_walkforward(combo_trades: dict, folds: list,
                    min_train_trades: int = 20) -> dict:
    """combo_trades: {combo_id: trades DataFrame (full sample)}.

    Returns {fold_rows, oos_trades, selected: {fold_idx: combo_id}}.
    """
    fold_rows = []
    oos_parts = []
    selected = {}
    for fold in folds:
        best_id, best_score, best_n = None, -np.inf, 0
        for cid, trades in combo_trades.items():
            tr = _slice(trades, fold.train_start, fold.train_end)
            if len(tr) < min_train_trades:
                continue
            score = float(tr["net_R"].mean())
            if score > best_score:
                best_id, best_score, best_n = cid, score, len(tr)
        if best_id is None:
            fold_rows.append({"fold": fold.idx,
                              "train_start": fold.train_start,
                              "test_start": fold.test_start,
                              "combo": None, "train_trades": 0,
                              "test_trades": 0, "test_net_R": 0.0,
                              "profitable": False})
            continue
        selected[fold.idx] = best_id
        te = _slice(combo_trades[best_id], fold.test_start, fold.test_end)
        test_net = float(te["net_R"].sum()) if len(te) else 0.0
        fold_rows.append({"fold": fold.idx,
                          "train_start": fold.train_start,
                          "test_start": fold.test_start,
                          "combo": best_id, "train_trades": best_n,
                          "test_trades": int(len(te)),
                          "test_net_R": test_net,
                          "profitable": bool(len(te) and test_net > 0)})
        if len(te):
            part = te.copy()
            part["fold"] = fold.idx
            part["combo"] = best_id
            oos_parts.append(part)
    oos = pd.concat(oos_parts, ignore_index=True) if oos_parts else pd.DataFrame()
    return {"fold_rows": fold_rows, "oos_trades": oos, "selected": selected}
