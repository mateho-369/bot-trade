# ============================================================
#   core/ai_context.py
#   The "bot → AI" half of the conversation (v2.1).
#
#   Two things the AI could not see before:
#
#   1. management_contract() — the REAL protection levels the bot applies to
#      an open trade. v2.0 hardcoded them in the prompts ("break-even at 50%,
#      trail at 60/90%, review every 15 minutes") while the config actually
#      said 30% / 30-50-70% / every 5 minutes. A reviewer arguing about
#      protection it cannot see, about protection that is not real, makes
#      wrong CLOSE/EXTEND calls. Now both roles read the live values.
#
#   2. lessons() — the track record, computed from the bot's OWN closed deals
#      and the AI's own journal: overall win rate, per-symbol win rate, cost
#      drag, and how the AI's own APPROVE/CLOSE/EXTEND decisions turned out
#      (by model and by reason_code). This is the feedback loop the docs said
#      was "out of scope": the AI is stateless between calls, so the bot hands
#      it its own memory. It is data, never instructions — same as everything
#      else in the snapshot.
#
#   Everything here is bounded (few rows, short strings) and cached: the hot
#   loop must never pay a CSV read per candidate.
# ============================================================

import json
import logging
import time

import config

logger = logging.getLogger("AIContext")

_cache = {}          # key -> (ts, value)
_CACHE_TTL = 45.0    # seconds


def _cached(name, builder, ttl=None):
    now = time.time()
    hit = _cache.get(name)
    if hit and now - hit[0] < (ttl or _CACHE_TTL):
        return hit[1]
    try:
        val = builder()
    except Exception as e:
        logger.debug(f"{name} build failed ({type(e).__name__})")
        val = {}
    _cache[name] = (now, val)
    return val


def invalidate():
    _cache.clear()


# ── 1. what the bot will actually do to this trade ──────────

def management_contract():
    """Live, sanitized copy of the deterministic rules that manage a trade.

    Injected into every AI snapshot so the model reasons about the protections
    that are really in force (and knows which levers it owns)."""
    def _r(v, nd=3):
        try:
            return round(float(v), nd)
        except (TypeError, ValueError):
            return None

    return {
        "engine": getattr(config, "ENTRY_ENGINE", "?"),
        "profile": getattr(config, "BOT_PROFILE", "?"),
        "tp_scale": _r(getattr(config, "TP_SCALE", 1.0), 2),
        "break_even_at_progress": _r(getattr(config, "BREAK_EVEN_PROGRESS", 0.45)),
        "trail_stages_progress": [_r(getattr(config, f"TRAIL_STAGE_{i}_PROGRESS"), 2)
                                  for i in (1, 2, 3)],
        "trail_stages_atr_mult": [_r(getattr(config, f"TRAIL_STAGE_{i}_ATR_MULT"), 2)
                                  for i in (1, 2, 3)],
        "cooldown_bars": _r(getattr(config, "COOLDOWN_BARS", 5), 0),
        "risk_percent_per_trade": _r(getattr(config, "RISK_PERCENT_PER_TRADE", 0.5), 2),
        "max_positions": int(getattr(config, "MAX_POSITIONS", 2)),
        "max_spread_pct_of_price": _r(getattr(config, "MAX_SPREAD", 0.05), 4),
        "max_spread_pct_of_stop": _r(getattr(config, "MAX_SPREAD_COST_PCT", 0.0), 1),
        "consec_loss_limit": int(getattr(config, "CONSEC_LOSS_LIMIT", 3)),
        "loss_paused_min": _r(getattr(config, "CONSEC_LOSS_PAUSE_MIN", 90), 0),
        "sl_widening_allowed": bool(getattr(config, "SL_BUFFER_ENABLED", False)),
        "session_utc": ([int(getattr(config, "SESSION_START", 0)),
                         int(getattr(config, "SESSION_END", 24))]
                        if getattr(config, "SESSION_FILTER", False) else "24/7"),
        "ai_cadence_seconds": {
            "entry_gate": "per new signal bar",
            "position_review": _r(getattr(config, "AI_POSITION_INTERVAL", 900), 0),
            "directives": _r(getattr(config, "AI_REGIME_INTERVAL", 7200), 0),
        },
        "extend_rule": {
            "min_progress": _r(getattr(config, "AI_EXTEND_MIN_PROGRESS", 0.9)),
            "tp_mult": _r(getattr(config, "AI_EXTEND_TP_MULT", 1.15), 2),
            "lock_pct_of_float": _r(getattr(config, "AI_EXTEND_LOCK_PCT", 0.8), 2),
            "once_per_position": True,
        },
        "ai_can": ["veto an entry (REJECT/WAIT)", "close an open trade early",
                   "extend a winner's TP once at >= min_progress",
                   "propose bounded settings inside AI_REGIME_ALLOWED"],
        "ai_cannot": ["place or size an order", "remove or widen a stop",
                      "raise risk above the owner baseline", "touch loss "
                      "limits, drawdown halt, margin buffer or the kill switch"],
    }


# ── 2. cost of this specific entry (spread vs stop distance) ─

def spread_cost_pct(spread_pct_of_price, sl_distance, price):
    """% of the stop distance the round-trip spread eats. The single most
    important number for a scalper's survival; both the deterministic gate
    and the AI see the same value."""
    try:
        sl_distance = float(sl_distance)
        price = float(price)
        if sl_distance <= 0 or price <= 0:
            return None
        return round(float(spread_pct_of_price) / 100.0 * price / sl_distance * 100.0, 2)
    except (TypeError, ValueError):
        return None


# ── 3. the memory: bot results + the AI's own track record ──

def _load_trade_log():
    import pandas as pd
    try:
        return pd.read_csv(config.TRADE_LOG_PATH)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug(f"trade log read failed: {type(e).__name__}")
        return None


def _load_journal():
    import pandas as pd
    try:
        df = pd.read_csv(config.AI_JOURNAL_PATH)
        return df if len(df) else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug(f"AI journal read failed: {type(e).__name__}")
        return None


def bot_results(n_trades=25):
    """Recent REAL outcomes of the bot itself (never guesses)."""
    def build():
        df = _load_trade_log()
        if df is None or "outcome" not in df.columns:
            return {}
        import pandas as pd
        closed = df.dropna(subset=["outcome"]).tail(int(n_trades))
        if not len(closed):
            return {}
        out = (pd.to_numeric(closed["outcome"], errors="coerce").fillna(0) > 0)
        wins = int(out.sum())
        total = int(len(closed))
        res = {"trades": total, "wins": wins, "losses": total - wins,
               "winrate_pct": round(100.0 * wins / total, 1),
               "sequence": "".join("W" if w else "L" for w in out.tail(12))}
        profit = pd.to_numeric(closed.get("profit"), errors="coerce").dropna()
        if len(profit):
            res["net_usd"] = round(float(profit.sum()), 2)
            res["avg_win_usd"] = round(float(profit[out.reindex(profit.index)
                                                    .astype(bool)].mean() or 0), 2) \
                if any(out) else 0.0
            res["avg_loss_usd"] = round(float(profit[~out.reindex(profit.index)
                                                     .astype(bool)].mean() or 0), 2) \
                if (~out).any() else 0.0
        # cost drag: the spread as % of the stop distance, trade by trade
        try:
            entry = pd.to_numeric(closed["entry"], errors="coerce")
            sl = pd.to_numeric(closed["sl"], errors="coerce")
            sp = pd.to_numeric(closed["spread_pct"], errors="coerce")
            dist = (entry - sl).abs()
            ok = (dist > 0) & (entry > 0) & sp.notna()
            if ok.any():
                cost = (sp[ok] / 100.0 * entry[ok] / dist[ok] * 100.0)
                res["spread_pct_of_stop_avg"] = round(float(cost.mean()), 1)
                res["spread_pct_of_stop_worst"] = round(float(cost.max()), 1)
        except Exception:
            pass
        # per-symbol split — a single bad pair should not poison every decision
        try:
            per = {}
            for sym, grp in closed.groupby("symbol"):
                w = int((pd.to_numeric(grp["outcome"], errors="coerce").fillna(0) > 0).sum())
                per[str(sym)[:16]] = {"n": int(len(grp)), "winrate_pct":
                                      round(100.0 * w / len(grp), 1)}
            res["per_symbol"] = dict(list(per.items())[:8])
        except Exception:
            pass
        return res
    return _cached("bot_results", build)


def ai_track_record(n=40):
    """How the AI's OWN decisions performed, from linked closed deals only."""
    def build():
        df = _load_journal()
        if df is None or "kind" not in df.columns:
            return {}
        import pandas as pd
        res = {}
        ent = df[(df["kind"] == "entry") & (df["execution"] == "executed")
                 & (df["outcome"].notna())].tail(int(n))
        if len(ent):
            out = pd.to_numeric(ent["outcome"], errors="coerce").fillna(0) > 0
            res["approved_trades"] = {"n": int(len(ent)),
                                      "winrate_pct": round(100.0 * float(out.mean()), 1)}
            prof = pd.to_numeric(ent.get("profit"), errors="coerce").dropna()
            if len(prof):
                res["approved_trades"]["net_usd"] = round(float(prof.sum()), 2)
            vetoed = df[(df["kind"] == "entry")
                        & (df["execution"] == "not_executed")
                        & (df["action"].isin(["REJECT", "WAIT"]))].tail(int(n))
            res["blocked_candidates"] = int(len(vetoed))
            # which reason codes actually pay off?
            try:
                by_code = {}
                for code, grp in ent.groupby("reason_code"):
                    o = pd.to_numeric(grp["outcome"], errors="coerce").fillna(0) > 0
                    if len(grp) >= 3:
                        by_code[str(code)[:24]] = {"n": int(len(grp)),
                                                   "winrate_pct": round(100.0 * float(o.mean()), 1)}
                if by_code:
                    res["approve_reason_codes"] = dict(list(by_code.items())[:6])
            except Exception:
                pass
            # and the blocks: what did we dodge? (unrealised, so only counts)
        pos = df[(df["kind"] == "position")].tail(int(n))
        if len(pos):
            acts = pos["action"].astype(str).str.upper()
            res["position_reviews"] = {
                "n": int(len(pos)),
                "close": int((acts == "CLOSE").sum()),
                "extend": int((acts == "EXTEND").sum()),
                "hold": int((acts == "HOLD").sum()),
                "unavailable": int(pos["source"].astype(str)
                                   .isin(["ai_unavailable", "ai_error"]).sum())}
        dir_rows = df[(df["kind"] == "entry") & (df["outcome"].notna())]
        if len(dir_rows):
            try:
                by_dir = {}
                for d, grp in dir_rows.groupby("direction"):
                    o = pd.to_numeric(grp["outcome"], errors="coerce").fillna(0) > 0
                    if len(grp) >= 3:
                        by_dir[str(d)[:6]] = {"n": int(len(grp)), "winrate_pct":
                                              round(100.0 * float(o.mean()), 1)}
                if by_dir:
                    res["winrate_by_direction"] = by_dir
            except Exception:
                pass
        return res
    return _cached("ai_track", build, ttl=90.0)


def directives_state():
    """What the market-adaptation AI has currently told the bot to do (so the
    entry/position AIs know the rules they are trading under, and so the
    bot's own logs stay explainable)."""
    def build():
        try:
            with open(config.AI_REGIME_STATE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            applied = d.get("applied") or {}
            age = time.time() - float(d.get("ts", 0))
            return {"applied": {k: (round(float(v), 4) if isinstance(v, (int, float))
                                    else v) for k, v in list(applied.items())[:16]},
                    "age_seconds": round(age, 0),
                    "expires_in_seconds": round(max(0.0, float(
                        getattr(config, "AI_REGIME_TTL_SECONDS", 14400)) - age), 0),
                    "regime": str(d.get("regime", ""))[:48]}
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
    return _cached("directives", build, ttl=15.0)
