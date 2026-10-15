# v2.3 Twin-brain scalper — how the work flows

Turn it on with `BOT_PROFILE=scalper` (demo account first). Everything below is
ON by default for that profile and OFF for `balanced` / `ultra_fast`.

## The three parties

| Party | Job | Speed | Can it trade? |
|---|---|---|---|
| **Bot** | finds the signal (M1 SMC), keeps its own read (`bot_view`), places orders, runs every risk gate | every 0.3 s | yes — the only one that orders |
| **Analyst (AI #1)** | reads the chart on its own, keeps a fresh *brief* per symbol: bias BUY/SELL/NEUTRAL, confidence, state, "stand aside" | one call per symbol per ~30 s, in the background | no — it only gives an opinion |
| **Guardian (AI #2 + Scalp Guard)** | watches every open trade: "is it working? close it before it costs money" | AI every 15 s; Scalp Guard every loop (no network) | closes only |

## Entry

```
every new M1 bar
  bot ──────────────► its signal (BUY/SELL) + bot_view (momentum, EMA slope, RSI, H1)
  Analyst ──────────► brief already in hand (written 0-60 s ago, valid 90 s)

  compare (instant, no network):
    AGREE      same direction, conf >= 0.55      -> trade NOW (fast lane)
    CONFLICT   opposite, conf >= 0.65, or the    -> the bot obeys the AI: no trade
               AI says "stand aside"
    NEUTRAL    no strong opinion                 -> per-signal AI gate decides (v2.2 flow)
    NO BRIEF   none yet / older than 90 s /      -> per-signal AI gate decides (v2.2 flow)
               price ran > 0.6 ATR since

  then, for any trade that goes ahead:
    spread gate -> $ sizing -> risk gates -> order
```

The bot shares its analysis with the AI in both directions: `bot_view` goes into
every brief, and the entry gate's snapshot carries the Analyst's brief, so the
two AI opinions are consistent.

**Failures never cost the bot its signal.** No key, provider down, slow answer,
open circuit, malformed reply: the brief is simply missing and the flow is
exactly v2.2's (which itself trades the bot's own signal after a 3 s budget).
A real REJECT/CONFLICT is always final.

## $ scalping (small, many, fast)

* The **market** decides the distance (ATR × TP_MULT / SL_MULT, 1.3 / 1.3 in the
  scalper profile). The **lot** is then sized so that TP distance is worth
  `SCALP_TARGET_USD` (default **$3**; try 2–5). With TP = SL distance the stop is
  worth about the same: small wins, small losses, win rate decides.
* The lot is rounded **down** to the broker step, never above the target.
* **Skipped** (with a log line) when:
  * the broker's minimum lot makes the stop worth more than `SCALP_MAX_LOSS_MULT`
    × target (e.g. BTC on many brokers),
  * the spread would cost more than 35 % of the target (`SCALP_MAX_SPREAD_PCT_OF_TARGET`),
  * the old cost gate (`MAX_SPREAD_COST_PCT`, 30 % of the stop) says so.
* Broker data missing → falls back to the percent-risk lot, never guesses.

## While a trade is open

Four independent watchers, none of which can widen or remove a stop:

1. **Trade manager** (v2.1): break-even and trailing on the broker-side SL.
2. **Scalp Guard** (local, every loop):
   * **BANK** — floating profit reached the $ target → close now
   * **GIVEBACK** — had ≥ 50 % of the target, gave back 40 % → bank the rest
   * **STALL** — 75 s and it never reached 25 % of the target → not working, out
   * **EARLY CUT** — never worked and already 70 % of the way to the stop → cut
   * **MAX HOLD** — 240 s, a scalp is not an investment
   * nothing fires in the first 3 s (spread noise)
3. **Analyst thesis-flip**: the Analyst now says the opposite with conf ≥ 0.70,
   **twice in a row**, with briefs **newer than the trade** → close. One wobble
   or an old opinion never closes anything.
4. **Guardian AI** (position reviewer, every 15 s): briefed in scalper terms
   (target, peak, seconds open, what the guard will do, the Analyst's brief).
   HOLD / CLOSE / EXTEND as before; it is told not to duplicate the guard.

## Cost and limits — read before running

* The Analyst adds about **2 AI calls per minute per symbol**. On a free tier
  (e.g. 30 requests/min) with 4 symbols, add keys to `AI_API_KEYS` or raise
  `AI_BRIEF_INTERVAL_SECONDS`. If calls fail, the bot trades as in v2.2.
* `AI_BRIEF_MODEL` can name a different model than `AI_MODEL` for a genuinely
  independent second opinion.
* A scalper pays the spread on every trade. `scalp_guard` and the spread gates
  reduce, not remove, that cost; this build has **not** been run against a real
  broker or provider — demo first, watch `logs/bot.log`:
  `AI BRIEF`, `AI+BOT AGREE`, `AI≠BOT`, `SCALP_BANK`, `SCALP_STALL`,
  `SCALP_GIVEBACK`, `SCALP_EARLY_CUT`, `AI_THESIS_FLIP`.
* The heartbeat line shows live counters: briefs, agree (fast-lane), conflict,
  neutral, none, guard-closes, flip-closes.

## Tuning cheatsheet (`~/.boss-bot/.env`, all optional)

| Want | Change |
|---|---|
| bigger / smaller wins | `SCALP_TARGET_USD` |
| AI must be surer before it can veto | `AI_BRIEF_VETO_CONF` ↑ |
| AI must be surer before the fast lane | `AI_BRIEF_AGREE_CONF` ↑ |
| every entry still goes through the AI gate | `AI_BRIEF_FASTLANE=false` |
| let trades breathe longer | `SCALP_STALL_SECONDS` ↑, `SCALP_MAX_HOLD_SECONDS` ↑ |
| fewer calls | `AI_BRIEF_INTERVAL_SECONDS` ↑ |
| turn a lane off | `AI_BRIEF_ENABLED`, `SCALP_GUARD_ENABLED`, `SCALP_USD_MODE`, `AI_BRIEF_FLIP_CLOSE` = false |

Do **not** put `ENTRY_ENGINE`, `AI_TIMEOUT_SECONDS`, `AI_WORKER_THREADS` etc. in
`.env` unless you mean to override the profile — `preflight.py` warns about it.
