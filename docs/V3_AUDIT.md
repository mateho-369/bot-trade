# V3 AUDIT (P0) — BOSS SMC BOT → "EDGE-FIRST AI SCALPER"

> **ដំណាក់កាល:** P0 AUDIT — គ្មានការកែ code (ឯកសារនេះជាឯកសារតែមួយគត់ដែលបានបង្កើត)
> **កាលបរិច្ឆេទ:** 2026-10-16 · **មូលដ្ឋាន:** `main` @ `d3cafe8` (v2.3.3)
> **វិសាលភាពដែលបានពិនិត្យ:** គ្រប់ file ក្នុង repo (13,460 បន្ទាត់ Python) — `main.py`, `config.py`,
> `core/*`, `ml/*`, `tg_alerts/*`, `tools/*`, `verify_*.py`, `docs/*`, `REAL_WORLD_TEST.md`
> **មិនបានប៉ះ:** `.env` (មិនមានក្នុង workspace នេះ ហើយមិនបានអាន), គ្មានការបញ្ជាទិញ, គ្មាន MT5 connection

---

## ០) បញ្ហា Branch (ត្រូវដឹងមុនគេ)

អ្នកសុំ branch ថ្មីឈ្មោះ `v3-rebuild` ប៉ុន្តែ session ធ្វើការនេះត្រូវបានភ្ជាប់ជាប់នឹង branch
`arena/a951958a-bot-trade` (ចេញពី `main` @ `d3cafe8`) — ការងារទាំងអស់ត្រូវនៅលើ branch នេះ
ទើបប្រព័ន្ធតាមដានបាន។ ដូច្នេះ៖

- ការងារ v3 ទាំងអស់នឹងធ្វើលើ `arena/a951958a-bot-trade` ដោយប្រើ **folder ថ្មីដាច់ដោយឡែក**
  (`core/data/`, `core/costs.py`, `core/ml/`, `core/execution/`, `research/`, `tools/run_phase.py` តាមផែនការ P1+)
  ដើម្បីឱ្យ code ចាស់នៅ `main` ដំណើរការដដែល ១០០% រហូតដល់ v3 ឆ្លង gates។
- **មិនទាន់ commit / push អ្វីទាំងអស់** — រង់ចាំការយល់ព្រមពីអ្នក (Hard Rule #5)។
  *(បច្ចុប្បន្នភាព 2026-10-16: ប្រយោគនេះពិតនៅពេលសរសេរ។ ក្រោយមក អ្នកបានសុំបើក PR —
  commit `6c62410` ត្រូវបាន push ហើយ PR #6 កំពុងបើក។ ម្ចាស់បានយល់ព្រមឱ្យ commit
  តូចៗលើ branch នេះបន្តទៀត; ហាម push ទៅ `main`, ហាម merge, ហាម force-push។
  មើល `docs/V3_DECISIONS.md`។)*

## ១) អ្វីដែលខ្ញុំបានធ្វើក្នុង P0

| # | ការងារ | លទ្ធផល |
|---|--------|--------|
| 1 | រាប់ file + បន្ទាត់ code ទាំងអស់ (`find`, `wc -l`) | 46 files, Python 13,460 បន្ទាត់; ធំជាងគេ: `main.py` 1,935, `verify_twin_brain.py` 1,273, `verify_ai_flow.py` 1,006 |
| 2 | អាន `config.py` (591 បន្ទាត់), `.env.example`, loader search order | secrets មកពី `$BOT_ENV_FILE` → `~/.boss-bot/.env` → `<repo>/.env` (env var ឈ្នះមុន) — **រក្សាទុកដដែល** |
| 3 | អាន/poll គ្រប់ module ក្នុង `core/`, `ml/`, `tg_alerts/`, `tools/` | តារាង reuse/drop នៅផ្នែក ៣ |
| 4 | ឆែក Hard Rules ទល់នឹង code បច្ចុប្បន្ន | រកឃើញ **ចន្លោះសំខាន់ ៤** (ផ្នែក ៤, R1–R4) |
| 5 | រត់ `python3 -m py_compile config.py core/*.py ml/*.py tools/*.py` | **PASS** (exit 0) — syntax ត្រឹមត្រូវទាំងអស់ |
| 6 | រត់ `python3 tools/scan_secrets.py --all` | **clean** (exit 0) — គ្មាន secret ក្នុង tracked files |
| 7 | ឆែក environment របស់ sandbox នេះ | Python **3.11.2**, **គ្មាន** pandas/numpy/sklearn/pyarrow/pytest, **គ្មាន** MetaTrader5 (Linux) — មើលផ្នែក ៥ |

**អ្វីដែលខ្ញុំមិនបានធ្វើ (ស្មោះត្រង់):** មិនបានរត់ test suite (`verify_*.py`) ព្រោះ sandbox នេះគ្មាន
pandas ហើយគ្មាន MT5 terminal — ដូច្នេះខ្ញុំ **មិនអះអាងថា tests ឆ្លង** ទេ។ ការ verify នោះត្រូវធ្វើនៅ
ម៉ាស៊ីន Windows របស់អ្នក (មើល decision D3)។

## ២) ផែនទី repo បច្ចុប្បន្ន (Current Map)

```
main.py (1,935)          ម៉ាស៊ីនធំតែមួយ: loop, entry gate, AI orchestration, scalp sizing, execution
config.py (591)          config + profiles (balanced / ultra_fast / scalper) + .env loader
core/
  mt5_connector.py (379) MT5 connect, candles, place_buy/sell, modify, close
  smc_engine.py (298)    SMC sweep/BOS features + signal (M1)
  mtf_engine.py (252)    M5/M15 scan + H1 trend gate
  sr_engine.py (191)     SR_H1 — engine ឈ្នះក្នុង duel (PF 1.15)
  flash_engine.py (282)  FLASH — គ្មាន edge (PF 0.67–0.78), experiment
  backtester.py (200)    backtest សម្រាប់ SMC engine តែមួយ, គ្មាន cost model
  risk_manager.py (292)  lot sizing, daily-loss/DD/margin gates, RiskState persistence
  trade_manager.py (317) break-even, progressive trailing, ML-confidence exit check
  scalp_guard.py (101)   BANK / GIVEBACK / STALL / EARLY_CUT / MAX_HOLD — pure function ✅
  ai_provider.py (328)   OpenAICompatClient: strict JSON, key rotation, circuit breaker ✅
  ai_bus.py (182)        async worker queue សម្រាប់ AI calls ✅
  ai_brief.py (462)      Analyst ("twin-brain" brief) + consensus() + FlipTracker
  ai_review.py (595)     Entry reviewer APPROVE/REJECT/WAIT + appeal
  ai_position.py (513)   Position reviewer HOLD/CLOSE/EXTEND (votes)
  ai_regime.py (469)     AI កែ parameters របស់ bot ដោយផ្ទាល់ (clamped directives) ⚠️
  ai_context.py (283)    context builders — មានបញ្ហា P&L leak (R2)
  ai_journal.py (210)    CSV journal នៃ AI decisions + outcome linking ✅
  ai_brief/ai_review/ai_position/ai_regime — schemas តឹងរ៉ឹង, validate local ✅
  news.py (172)          FXStreet/investing RSS, seen-store, rate-limited alerts
  secret_guard.py (207)  log redaction + secret scanner ✅
  state_logger.py (61)   change-only logging + heartbeat helper
ml/model.py (299)        RandomForest + KMeans pattern blacklist/whitelist (មិនមែន meta-label)
tg_alerts/alerts.py (312) Telegram (rate-limited worker) ✅
tools/                   scan_secrets.py (pre-commit), fresh_start.py
verify_*.py (7 files)    script-style tests (assert counts: 9–312/file), MT5 mocked, _isolate_env.py ✅
backtest_ml.py           walk-forward OOS validation នៃ ML filter (ចាប់ផ្តើមល្អ)
REAL_WORLD_TEST.md       ភស្តុតាងសំខាន់: SR_H1 PF 1.15 · SMC_MTF 0.93 · M1 PF 0.69 · ML AUC 0.512
```

## ៣) Reuse / Rework / Drop — តាម Layer របស់ v3

### រក្សាទុកប្រើដដែល (Reuse as-is ឬស្ទើរតែដដែល)

| Module | Layer v3 | ហេតុផល |
|--------|----------|---------|
| `config.py` (.env loader search order) | ទាំងអស់ | Hard Rule #1 — search order ត្រឹមត្រូវហើយ, បន្ថែម `ALLOW_LIVE` + v3 keys ទៅក្នុងនោះ |
| `core/secret_guard.py` + `tools/scan_secrets.py` + `.githooks/pre-commit` | ទាំងអស់ | scan ទើបរត់: **clean** — ហាមបិទ |
| `core/scalp_guard.py` | L5 | pure function, unit-testable, មាន BANK/GIVEBACK/STALL/EARLY_CUT/MAX_HOLD រួច — គ្រាន់តែត្រូវ tune thresholds ដោយ research (មិនមែនទាយ) |
| `core/ai_provider.py` (`OpenAICompatClient`, `strict_json`) | L3 | strict JSON schema, timeout/retry, key rotation, circuit breaker មានរួច |
| `core/ai_bus.py` | L3/L4 runtime | async workers — AI មិន block loop ✅ |
| `core/ai_journal.py` | L6 | journal មាន decision_id, latency, model, outcome linking — ពង្រីក columns សម្រាប់ v3 |
| `core/risk_manager.py` (ផ្នែក `can_open_new_trade`, `positions_allowed`, `calc_lot_for_usd`) | L4 | daily-loss halt, DD halt, free-margin check, max-positions 1 ក្រោម $50 មានរួច ✅ |
| `core/news.py` | L3 context | RSS + publish timestamp (`items` រក្សា `(epoch, title)`) — ត្រូវបន្ថែម ingestion-delay guard សម្រាប់ replay |
| `core/sr_engine.py`, `core/smc_engine.py` (logic) | L1 candidates (b)/(c) | port ជា pure functions — SR_H1 = baseline ត្រូវ reproduce នៅ P1; SMC = candidate (c) |
| `core/mtf_engine.py` (`h1_trend_direction`, `h1_gate_ok`) | L1 filters | H1 trend filter មានរួច |
| `core/trade_manager.py` (BE/progressive trailing logic) | L5 | port thresholds ទៅ config ថ្មី |
| `tg_alerts/alerts.py` | L6 | rate-limited worker មានរួច |
| `verify_*.py` + `_isolate_env.py` pattern | tests | MT5 mocked, empty-.env isolation — យកធ្វើគំរូសម្រាប់ pytest suite ថ្មី |
| `backtest_ml.py` (walk-forward idea) | L2/P3 | expanding-window OOS មានរួច — ពង្រីកទៅ purged+embargoed CV |
| `run_bot.bat`, `run_bot_forever.bat`, `preflight.py`, `install.bat` | runtime | Windows runners (CRLF) — បន្ថែម v3 runner, មិនកែរបស់ចាស់ |

### ត្រូវកែ/សរសេរឡើងវិញ (Rework)

| Module | បញ្ហា | ការងារ v3 |
|--------|-------|-----------|
| `core/mt5_connector.py` | `type_filling` hardcode FOK→IOC (មិនអាន `symbol_info.filling_mode` → ហានិភ័យ retcode 10030); **គ្មាន `order_check`** មុន `order_send`; deviation ថេរ 50; `get_candles*` **បោះចោល column `spread` និង `real_volume`** របស់ MT5 rates; គ្មាន `copy_ticks_range` | L0/L4: សរសេរ execution layer ថ្មី (`filling_mode` ពី symbol_info, `order_check`, bounded retries សម្រាប់ 10004/10006/10018/10019/10021, idempotency); data layer ថ្មីរក្សា spread/ticks ជា Parquet |
| `core/backtester.py` | engine តែមួយ (SMC); **TP មុន SL** ក្នុង bar ដដែល (optimistic); `spread_pct` hardcode `0.0` ក្នុង trade rows; គ្មាន slippage/commission/swap; គ្មាន time-stop (triple barrier) | P1: សរសេរ event-driven backtester ថ្មី — SL-first, cost model ពី L0, triple-barrier, shared ដោយគ្រប់ L1 candidates |
| `core/ai_brief.py` (Analyst) | schema `state` មានតែ TREND/RANGE/CHAOS/QUIET (គ្មាន TREND_UP/DOWN, គ្មាន `key_levels`); interval 30s (ថ្លៃ AI calls ខ្លាំង vs spec 3–5 នាទី); អានតែ 24 M1 bars | L3 AI-1: schema ថ្មីតាម spec, interval 3–5 នាទី, 30 M1 + 20 M5 + H1 context |
| `core/ai_position.py` (Guardian) | context ខ្វះ MFE/MAE, candle shape (body/wick ជា ATR), structure distances, peak/target/stop ជា $ តាម spec; បន្ថែមលើនេះមាន R2 leak (ខាងក្រោម) | L3 AI-3: context ថ្មីតាម spec, interval 10–20s ដដែល, EXTEND នៅតែត្រូវគោរព `AI_EXTEND_MIN_PROGRESS` |
| `core/ai_review.py` (Entry reviewer) | គ្រោងការណ៍ល្អហើយ (budget 3s, appeal, cache) ប៉ុន្តែ context មាន R2 leak; គ្មាន "cost as % of target" ជា field ផ្លូវការ | L3 AI-2: រក្សា mechanism, កែ context |
| `ml/model.py` | **មិនមែន meta-label**: train លើ live trade log តិចតួច, labels មិនទាន់ដក cost, គ្មាន purged CV; `predict()` ពេលមិនទាន់ train **fail-open** (return `True, 1.0`) | P3: `core/ml/meta.py` ថ្មី — triple-barrier labels NET OF COST, purged+embargoed CV, expectancy threshold. របស់ចាស់ទុកឱ្យ profile ចាស់ប្រើ |
| `main.py` | 1,935 បន្ទាត់ — loop + orchestration + execution លាយគ្នា | v3 runtime ថ្មីជា modules តូចៗ; `main.py` ចាស់មិនប៉ះ |

### បោះចោល / បិទក្នុង v3 (Drop — មិនលុប file, គ្រាន់តែមិនយកចូល v3)

| អ្វី | ហេតុផល |
|------|---------|
| `core/ai_regime.py` directives (AI កែ bot parameters ដោយផ្ទាល់) | **ខុសនឹង v3 spec**: "No self-modifying live config" — AI-4 សរសេរ hypotheses ទៅ `research/hypotheses.md` តែប៉ុណ្ណោះ។ Clamps របស់វាល្អ ប៉ុន្តែ concept ត្រូវប្តូរ |
| `core/flash_engine.py` (FLASH + hedge) | repo-measured PF 0.67–0.78, hedge = បង់ spread 2 ដង — គ្មាន edge |
| Profile `ultra_fast` / `scalper` loss limits (25% daily / 40% DD) | រលុងពេកសម្រាប់ v3 gates; v3 ប្រើ kill switches តឹង (រក្សា mechanism, កែតម្លៃតាម config ថ្មី) |
| Symbols default 4 (`XAUUSDm,EURUSDm,GBPUSDm,BTCUSDm`) | v3 = **XAUUSDm តែមួយ** រហូតដល់ឆ្លង gates |
| Pattern blacklist/whitelist (KMeans) ជា live gate | REAL_WORLD_TEST: ML AUC 0.512 ≈ coin flip — មិនអាចធ្វើជា gate; ទុកជា research feature បាន |

## ៤) ចន្លោះធៀបនឹង Hard Rules (Risks — ត្រូវជួសជុលមុន P5)

| ID | ភាពធ្ងន់ធ្ងរ | បញ្ហា | ភស្តុតាងក្នុង code |
|----|--------------|-------|---------------------|
| **R1** | 🔴 ខ្ពស់ | **គ្មាន `ALLOW_LIVE` gate**: bot login គណនីណាក៏បាន; `preflight.py` គ្រាន់តែ *ព្រមានទន់* (soft) បើ server មិនមែន demo — ខុស Hard Rule #2 | `core/mt5_connector.py:connect()`; `preflight.py:113-116` (soft warning only) |
| **R2** | 🔴 ខ្ពស់ | **AI មើលឃើញ P&L អតីតកាល**: `ai_context.bot_results()` / `ai_track_record()` ផ្ញើ `net_usd`, avg win/loss, AI track record ទៅ entry reviewer, position reviewer និង regime — ខុស spec ("AI must not steer by account balance or past P&L"; AI-3 ហាមមើល past results) | `core/ai_context.py:150-260`; ប្រើនៅ `core/ai_position.py:283-285` (`ai_lessons`), `core/ai_review.py`, `core/ai_regime.py` |
| **R3** | 🟠 មធ្យម | **Cost model មិនមាន**: backtester ប្រើ `spread_pct = 0.0`, TP-first intra-bar; លេខ PF ចាស់ៗមកពី lab scripts **ដែលមិននៅក្នុង repo** (គ្មាន data file, គ្មាន script reproduce) — P1 ត្រូវ "reproduce SR_H1 PF 1.15" ប៉ុន្តែមិនមានរបស់ឱ្យ reproduce ដោយផ្ទាល់ ត្រូវសង់ឡើងវិញពី `sr_engine.py` logic | `core/backtester.py:170-196`; `REAL_WORLD_TEST.md:154-155` |
| **R4** | 🟠 មធ្យម | **Execution មិនទាន់ MT5-safe ពេញលេញ**: គ្មាន `order_check`, filling mode hardcode, គ្មាន bounded retry តាម retcode, គ្មាន restart-reconciliation/idempotency លើសពី ticket-diff | `core/mt5_connector.py:141-256` |
| R5 | 🟡 ទាប | គ្មាន tick data / spread-per-bar / Parquet / spread-by-hour sampling (L0 ទាំងស្រុងមិនទាន់មាន) | គ្មាន `copy_ticks_range` នៅកន្លែងណាទេ |
| R6 | 🟡 ទាប | គ្មាន walk-forward/PBO/DSR/trial-counter; គ្មាន `research/`, `tools/run_phase.py`, `tools/daily_report.py`; heartbeat រាល់ 15 នាទី (spec: 1 នាទី) | `main.py:1860` |
| R7 | 🟡 ទាប | `SMC_Bot_Data/data/patterns.json` ត្រូវបាន git-tracked ទោះជា `.gitignore` មានវា (file ចាស់មុន ignore) — មិនមែន secret ប៉ុន្តែត្រូវដឹង | `git ls-files` |
| R8 | 🟡 ទាប | Environment មិនទាន់ត្រូវ spec: repo រត់ Python 3.10–3.12 នៅ Windows; sandbox នេះ 3.11.2 + Linux (គ្មាន MT5) | `docs/RUN_ON_WINDOWS.md:12` |

**ចំណុចល្អដែលត្រូវកត់សម្គាល់ (អ្វីដែល v2 ធ្វើត្រូវហើយ):** SL-first ត្រូវបាន fix ក្នុង lab,
look-ahead entry bug ត្រូវបាន fix (`backtester.py` ចូលនៅ bar បន្ទាប់), kill switches មានគ្រប់
(daily loss, DD, margin, consec-loss pause, max spread + spread-cost-% gate), AI fail-safes មាន
(circuit breaker, journal គ្រប់ decision, directives មាន TTL/clamps), secret hygiene ល្អ (scan clean)។

## ៥) ឧបសគ្គ Environment (ត្រូវសម្រេចមុន P1)

| បញ្ហា | ការពិត | ជម្រើស |
|-------|--------|--------|
| MT5 មានតែ Windows | sandbox នេះជា Linux — ទាញ history/ticks ពិត និងរត់ demo **ធ្វើមិនបាននៅទីនេះ** | (ក) អ្នករត់ script ទាញ data នៅម៉ាស៊ីន Windows របស់អ្នក ហើយដាក់ Parquet ចូល `data/`; (ខ) P1 សង់ backtester + synthetic-data tests នៅទីនេះ ហើយផ្ទៀងផ្ទាត់ data ពិតនៅ Windows |
| Python packages | sandbox គ្មាន pandas/numpy/sklearn/pyarrow/pytest | P1 នឹងបន្ថែម `pyarrow` ទៅ `requirements.txt` (ត្រូវការអនុញ្ញាតពីអ្នក ព្រោះកែ file) និងប្រើ venv នៅ Windows |
| AI providers | អ្នកបញ្ជាក់ Groq (`qwen/qwen3.8-27b`) + Gemini (`gemini-3.5-flash-lite`) ប៉ុន្តែ `config.py`/`.env.example` default នៅជា `gpt-4o-mini` generic — ខ្ញុំ **មិនបានអាន .env** ដូច្នេះមិនដឹងថា keys/models ពិតត្រូវបាន set ឬអត់ | អ្នកបញ្ជាក់នៅ P4 (មិនចាំបាច់ឥឡូវ) |

## ៦) ផែនការទៅ P1 (ស្នើ — មិនទាន់ធ្វើ)

1. `core/data/` — downloader script (Windows/MT5): `copy_rates_range` M1/M3/M5/M15/H1 + `copy_ticks_range`
   សម្រាប់ spread ពិត, រក្សា Parquet (UTC), `symbol_info` snapshot (commission/swap/contract/tick value/
   volume min-step/stops/freeze/filling), spread-by-hour report។ XAUUSDm តែមួយ។
2. `core/costs.py` — cost = spread ពេលនោះ + slippage 0.3×spread + commission; stress ×1.5 switch។
3. Backtester ថ្មី (event-driven, SL-first, triple-barrier, net-of-cost) + unit tests: fills, costs,
   triple barrier, no-look-ahead (offline, MT5 mocked, `_isolate_env.py` pattern)។
4. **Reproduce check:** រត់ SR_H1 logic (`core/sr_engine.py`) លើ data ពិត ប្រៀបធៀបនឹងលេខឯកសារ
   `426 trades · WR 36.2% · PF 1.15 · E[R] +0.020 · maxDD 4.0R` (REAL_WORLD_TEST.md:155) ក្នុង tolerance
   ដែលកំណត់មុន — បើមិនត្រូវគ្នា រាយការណ៍ត្រង់ៗ មិនកែទិន្នន័យ។
5. បញ្ចប់ដោយរបាយការណ៍ Khmer ដូចទម្រង់ផ្នែក ៨ របស់ spec ហើយឈប់រង់ចាំ "OK"។

**គ្មាន strategy ថ្មីណាមួយ (P2), គ្មាន meta-label (P3), គ្មាន AI (P4), គ្មាន demo order (P5) ក្នុង P1 ទេ។**

## ៧) ការសម្រេចចិត្តដែលខ្ញុំត្រូវការពីអ្នក

| ID | សំណួរ | ជម្រើស |
|----|-------|--------|
| **D1** | Branch: ធ្វើការ v3 លើ `arena/a951958a-bot-trade` (folder ថ្មីដាច់) ជំនួស `v3-rebuild` បានទេ? | បាន / មិនបាន (បើមិនបាន ត្រូវពន្យល់ពីដែនកំណត់ session) |
| **D2** | P1 data: អ្នកនឹងរត់ downloader នៅ Windows MT5 (Exness-MT5Trial6 demo) ហើយផ្តល់ Parquet, ឬឱ្យខ្ញុំសង់ P1 ដោយ synthetic/offline tests មុន រួចអ្នកផ្ទៀងផ្ទាត់ data ពិតតាមក្រោយ? | Windows-first / Offline-first |
| **D3** | ឱ្យខ្ញុំរត់ test suite ចាស់ (`verify_*.py`) នៅទីណា? sandbox នេះរត់មិនបាន (គ្មាន pandas/MT5) | អ្នករត់នៅ Windows ហើយផ្ញើ output / ខ្ញុំសង់ CI-style offline runner នៅ P1 |
| **D4** | យល់ព្រមចាប់ផ្តើម **P1** តាមផែនការផ្នែក ៦ ទេ? (រួមទាំងបន្ថែម `pyarrow` ទៅ requirements នៅពេលត្រូវការ) | OK ចាប់ផ្តើម P1 / កែផែនការសិន |

---

*ឯកសារនេះមិនមាន secret, មិនមានលេខ performance ថ្មីដែលប្រឌិត — លេខទាំងអស់ដកស្រង់ពី
`REAL_WORLD_TEST.md` របស់ repo ផ្ទាល់ និងពី commands ដែលបានរត់ពិតក្នុង P0 (py_compile PASS, secret scan clean)។*
