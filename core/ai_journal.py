# ============================================================
#   core/ai_journal.py
#   Additive decision journal for the AI entry reviewer.
#
#   Reuses the bot's EXISTING storage style (a pandas CSV under DATA_ROOT_PATH,
#   exactly like ml/model.TradeLogger) — there is no second database. New
#   columns are added additively and an existing file stays readable.
#
#   One row per AI-reviewed candidate. Rows are bounded and sanitized: no
#   secrets, no API keys, no raw provider/exception bodies, no account
#   identifiers. The decision-time snapshot is stored as a short safe summary
#   plus a sha256 hash, not the full prompt.
#
#   A row's lifecycle:
#     record(...)            -> decision made (execution="pending")
#     mark_execution(...)    -> candidate reached the order path (executed)
#     finalize_pending(...)  -> candidate never executed (AI-approved but a
#                              deterministic gate/risk veto blocked it, etc.)
#     link_outcome(...)      -> reconciled closed-trade P&L attached later,
#                              from the bot's OWN closed deals only.
# ============================================================

import os
import json
import hashlib
import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("AIJournal")

# Column order is stable; new columns are appended additively.
_COLUMNS = [
    "decision_id", "time", "kind",
    "symbol", "timeframe", "direction", "engine",
    "snapshot_hash", "input_summary",
    "provider", "model", "schema_version", "feature_version",
    "action", "confidence", "reason_code", "reason", "risk_flags",
    "latency_ms", "cached", "source",
    "risk_result", "execution", "ticket", "final_action",
    "outcome", "profit",
]

_MAX_SUMMARY = 800  # bound the JSON summary stored per row


def _sanitize_summary(summary: dict) -> str:
    """Canonical, bounded, secret-free JSON summary of the decision context."""
    try:
        text = json.dumps(summary or {}, sort_keys=True, separators=(",", ":"),
                          default=str)
    except (TypeError, ValueError):
        text = "{}"
    if len(text) > _MAX_SUMMARY:
        text = text[:_MAX_SUMMARY]
    return text


class AIJournal:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            pd.DataFrame(columns=_COLUMNS).to_csv(path, index=False)
            logger.info(f"AI decision journal created: {path}")
        else:
            self._migrate()

    def _migrate(self):
        """Backward-compatible: keep an existing file, append any new columns
        (old rows read as empty for the new columns). Never drops data."""
        try:
            df = pd.read_csv(self.path)
        except Exception as e:
            logger.error(f"AI journal read error (left untouched): {e}")
            return
        missing = [c for c in _COLUMNS if c not in df.columns]
        if missing:
            for c in missing:
                df[c] = np.nan
            df = df[_COLUMNS]
            df.to_csv(self.path, index=False)
            logger.info(f"AI journal migrated (+{len(missing)} columns): {missing}")

    # ── writes ───────────────────────────────────────────────
    def record(self, *, symbol, timeframe, direction, engine, snapshot_hash,
               input_summary, provider, model, schema_version, feature_version,
               action, confidence, reason_code, reason, risk_flags,
               latency_ms, cached, source, final_action,
               execution="pending", risk_result="pending") -> int:
        """Append one AI-reviewed candidate. Returns its decision_id."""
        decision_id = self._next_id()
        row = {
            "decision_id": decision_id,
            "time": pd.Timestamp.now(tz="UTC").isoformat(),
            "kind": "entry",
            "symbol": symbol, "timeframe": timeframe,
            "direction": direction, "engine": engine,
            "snapshot_hash": snapshot_hash,
            "input_summary": _sanitize_summary(input_summary),
            "provider": provider, "model": model,
            "schema_version": schema_version, "feature_version": feature_version,
            "action": action,
            "confidence": None if confidence is None else float(confidence),
            "reason_code": reason_code, "reason": reason,
            "risk_flags": json.dumps(list(risk_flags or [])),
            "latency_ms": None if latency_ms is None else int(latency_ms),
            "cached": bool(cached), "source": source,
            "risk_result": risk_result, "execution": execution, "ticket": np.nan,
            "final_action": final_action,
            "outcome": np.nan, "profit": np.nan,
        }
        pd.DataFrame([row])[_COLUMNS].to_csv(self.path, mode="a",
                                             header=False, index=False)
        return decision_id

    def _next_id(self) -> int:
        try:
            df = pd.read_csv(self.path, usecols=["decision_id"])
            if len(df) and df["decision_id"].notna().any():
                return int(df["decision_id"].max()) + 1
        except Exception:
            pass
        return 1

    def mark_execution(self, decision_id: int, *, ticket, risk_result="passed",
                       execution="executed", final_action="executed"):
        self._update(decision_id, ticket=ticket, risk_result=risk_result,
                     execution=execution, final_action=final_action)

    def finalize_pending(self, decision_id: int, *, block_reason=None):
        """If the row is still 'pending' execution, mark it not-executed.
        Safe to call on an already-finalized row (it is a no-op then)."""
        self._update(decision_id, risk_result=block_reason or "blocked",
                     execution="not_executed",
                     final_action=block_reason or "blocked_before_order",
                     only_if_pending=True)

    def link_outcome(self, symbol: str, ticket, profit) -> int:
        """Attach a reconciled closed-trade result to every still-open journal
        row for this (symbol, ticket). Outcome comes ONLY from the bot's own
        closed deals — never a guess or an open trade."""
        try:
            df = pd.read_csv(self.path)
        except Exception as e:
            logger.error(f"AI journal link_outcome read error: {e}")
            return 0
        mask = ((df["symbol"] == symbol) & (df["ticket"] == ticket)
                & (df["outcome"].isna()) & (df["ticket"].notna()))
        if mask.sum() == 0:
            return 0
        df.loc[mask, "outcome"] = 1 if profit > 0 else 0
        df.loc[mask, "profit"] = float(profit)
        df.to_csv(self.path, index=False)
        return int(mask.sum())

    # ── internal ─────────────────────────────────────────────
    def _update(self, decision_id, *, ticket=None, risk_result=None,
                execution=None, final_action=None, only_if_pending=False):
        try:
            df = pd.read_csv(self.path)
        except Exception as e:
            logger.error(f"AI journal update read error: {e}")
            return
        mask = df["decision_id"] == decision_id
        if only_if_pending:
            mask &= (df["execution"] == "pending")
        if not mask.any():
            return
        if ticket is not None:
            df.loc[mask, "ticket"] = ticket
        if risk_result is not None:
            df.loc[mask, "risk_result"] = risk_result
        if execution is not None:
            df.loc[mask, "execution"] = execution
        if final_action is not None:
            df.loc[mask, "final_action"] = final_action
        df.to_csv(self.path, index=False)


def snapshot_hash(summary: dict) -> str:
    """Stable sha256 over the canonical (sorted-key) summary."""
    canonical = json.dumps(summary or {}, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
