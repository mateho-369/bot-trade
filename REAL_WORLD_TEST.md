# ការពិសោធជាម្លក់ — BOSS SMC BOT លើទិន្នន័យពិត (Real-World Backtest)

**កាលបរិច្ឆេទ:** 2026-09-11 · **ទិន្នន័យ:** XAUUSD M1 ពិតពី MT5 (ឆ្នាំ 2024–2025, 709,543 bars)
**Broker ដដែល:** Exness · **ចំណាយដែលបានគណនា:** spread 0.25 + slippage 0.05 = 0.30/ជួល (stress: 0.45)

## លទ្ធផលសំខាន់ (ត្រង់ៗ)

| បែប | ការវាស់វែង |
|---|---|
| **Backtest លាម** (មាន bug look-ahead) | WR 57–59% · PF 2.7 · ហាក់ដូចជា «ឈ្នះ» |
| **M1 បាន fix ហើយ គ្មានចំណាយ** | WR ~34% · PF ~1.0 · **ស្មើសូន្យ** |
| **M1 បាន fix + spread Exness** | WR 34% · **PF 0.69** · E[R] = **-0.34** · បាត់ ~$3,000/lot/ឆ្នាំ |
| **MTF ពេញលេញ (ដដែល bot ធ្វើស្រាប់) + spread** | 10,746 ដង/2 ឆ្នាំ · WR 36% · **PF 0.93** · E[R] = **-0.165** · បាត់ $1,107 (2024) + $349 (2025) /lot |
| **ML gate (walk-forward, OOS)** | **AUC 0.512 = ដើកកុក** · ៥% ចុងក្រោយដែល ML យល់ព្រម៖ WR 38.5% → E[R] = **-0.14** (នៅតែបាត់) |
| ដាច់ខាតតែ M15 (with cost) | E[R] ≈ **+0.004** (ស្មើសូន្យ — ចំណែកតែមួយគត់ដែលមិនបាត់) |

## សេចក្តីសន្និដ្ឋាន

**បើប្រើ bot បែបនេះក្នុងជីវិតពិត → បាត់បង់ (LOSS) មិនមែនឈ្នះទេ។**

- គណនា $50 ហានិភ័យ 1%/trade: E[loss] ≈ **-0.165R/ដង** × ~450 ដង/ខែ ≈ **-74% របស់គណនា/ខែ** → គណនាអស់ ក្នុង ~1.5 ខែ (ទោះបីមាន daily-stop/drawdown-stop ក៏ដោយ — វាគ្រាន់តែធ្វើឱ្យវាបាត់យឺត។)
- **មូលហេតុសំខាន់:** 1) spread មាសនៅ Exness (~0.25–0.30) គឺ ≈ ៤០–៥០% នៃ SL ចម្ងាយនៅ M1 — scalping M1 មាសបង់ចំណាយច្រើនពេក; 2) សញ្ញា SMC sweep/BOS នៅ M1 មាស **គ្មាន edge** បន្ទាប់ពីនិយាយតាមពេលវេលាពិត (look-ahead បានកំណត់វា); 3) ML (RandomForest កែលម្អ ៣០–៥០០ trade) **មិនអាចទស្សន៍ព្យាយាមឈ្នះ** (AUC 0.51 = coin flip) — «continuous learning» មិនបង្កើត edge ទេ។

## Bug ដែលបានរកឃើញ និងជួសសម្រួល (v1.8.0)

1. **Look-ahead pivot** — `smc_engine.py`: pivot ត្រូវបានប្រើ ៥ bars មុនពេលវាបានបញ្ជាក់។ បាន `shift(SWING_LEN)` មុន `ffill()` (backtest == live ឥឡូវ)។
2. **Look-ahead entry** — `backtester.py`: ចូលនៅ open នៃ bar ដែលសញ្ញាកើត (សញ្ញាត្រូវការ close របស់វា!) → ឥឡូវចូលនៅ open នៃ **bar បន្ទាប់** + ត្រួតពិនិត្យ TP/SL នៅ bar ចូល។
3. **`get_closed_trades` បំបែក** — `group="*XAUUSD*"` មិនមែន parametrization ត្រឹមត្រូវ (វាជា company name) → deals មិនរកឃើញ ឡើយ → outcome មិន log · ML មិន retrain · position tracking បាត់។ ឥឡូវ filter symbol ដោយខ្លួនឯង។
4. **Order id ≠ Position ticket** — bot បានរក្សា `result.order` ប៉ុន្តែ `positions_get()` ដឹងតែ position ticket → position អាចខ្វះការគ្រប់គ្រង។ ឥឡូវ resolve ticket ពិតប្រាកដ។
5. **Cooldown គ្មាននៅ live** — `COOLDOWN_BARS=5` ប្រើតែនៅ backtest; live បានចូលវិញនៅ bar បន្ទាប់។ ឥឡូវមាន gate ដដែល។
6. **Signed progress** — `trade_manager.py`: `abs()` ធ្វើឱ្យ trade បាត់ ហាក់ដូចជា «50–90% ទៅ TP» → អាច trigger BE/trailing ខាងឆ្ងាយ។ ឥឡូវមានសញ្ញា។
7. **ML `predict`** — index `[0][1]` មិនស្ថិតស្ថេរ (បើ retrain លើ 1 class តែ → ខុស/ឆ្លង) → ឥឡូវ lookup តាម `classes_`។
8. **🔐 ប្រសិទ្ធភាព** — លេខ MT5 + ពាក្យសម្ងាត់ + Telegram token បានបញ្ចូលក្នុង code រួចហើយ (នៅក្នុង git history ផងដែរ!) → បានលុប; ឥឡូវ `.env` តែមួយគត់។ **⚠️ ត្រូវកែ password MT5 ឡើងវិញ + revoke/rotate Telegram token នៅ BotFather ឥឡូវនេះ!**
9. **«Font upgrade»** — bot គ្មាន GUI; «font» របស់វាគឺ Telegram alerts → បានរៀបចំ layout ឡើងវិញ (card ថ្មី, alignment, ខ្លី, ប្រណីជាងមុន)។

## បើចង់ឱ្យវា «អាចស៊ីបាន» ក្នុងពិត — ការណែនាំ

1. **ឡើង H1/M15 ចូល** (ចំណែក M15 ជិតស្មើសូន្យ; cost/SL តូចជាងខ្លះ) — bot មាសដែលមាន live record ពិតប្រាកដ (MyFxBook) គ្រប់គ្រងទាំងអស់ធ្វើលើ H1។
2. **ប្តូរគណនា Raw/ECN** — spread មាស 0.02–0.10 ជំនួស 0.25+ (បើនៅ M1 វាអាចផ្លាស់ប្តូរលទ្ធផលពី -0.34R ទៅ ~-0.15R ដដែល តែមិនដល់ positive ទេ)។
3. **ដក ML gate ចេញ ឬកែ** — បង្រៀនលើទិន្នន័យខ្លាំង (≥ ម៉ឺន trade, feature កាន់តែច្រើន, walk-forward ពិតប្រាកដ)។ បច្ចុប្បន្នវាជា noise។
4. **បែបនេះដឹងថាត្រូវគ្រប់គ្រង risk ប្រកបដោយកម្លាំង** (daily loss, drawdown, withdrawal ladder) — តែវាមិនអាចបង្កើត edge ពីសូន្យទេ។
5. **មុនពេលវិនិយោគលុយពិត:** demo ≥ 2–៣ ខែ + មើល stats ពិត (មិនមែន backtest) — ត្រឹមត្រូវដូចគ្នានឹងអ្វីដែល bot ផ្សេងៗផ្តល់នូវ MyFxBook verified track record។

### Bot ផ្សេងៗដែលអ្នកពិតប្រើក្នុងពិត (ការស្រាវជ្រាវរហ័ស)

| Bot | ប្រភេទ | ការពិត |
|---|---|---|
| **Gold Reaper EA** ($599, MT5) | មាស H1 breakout, 9 ម៉ូដ, SL/TP គ្រប់ត្រង់, គ្មាន martingale/grid | MyFxBook ពិតប្រាកដ · MQL5 4.46/5 (92 reviews) · ប៉ុន្តែ live DD ៤១% ធៀប backtest ១២% — «ឥតវិញ្ញា» live ក៏មិនស្មើ backtest ដែរ |
| **Mad Turtle EA** (MT5) | មាស H1 · ONNX អ៊ីចឹងបណ្ណាស (MT5 ពិតប្រាកដ) · 1 trade/ពេល · SL មុនរាល់ត្រង់ | ប្រើ ML ពិត (ONNX) ប៉ុន្តែនៅ H1 មិនមែន M1 ទេ |
| **3Commas / Cryptohopper** (crypto) | ក្រឡា/DCA bots · $19–75/ខែ | គ្រប់គ្រងបង្ហាញនូវតម្លៃតែមកពី**ប្រព័ន្ធភាពរបស់អ្នក** មិនមែនពី platform ទេ; ប្រាក់ពិតប្រើប្រាស់ ០.១–០.៥%/ដង + slippage កាត់ផ្តាច់ |
| **Freqtrade** (open-source, Python) | ក្រុម crypto ពេញលេញ | ឥតគិតថ្លៃ, អ្នកប្រាស្រាក់ strategy របស់ខ្លួន, backtesting + walk-forward អនុគមន៍ពិតប្រាកដ — ចង់ឯកជនភាព/គ្រប់គ្រង = ប្រើនេះ |

**ត្រលប់មកគោលការណ៍ដដែល:** bot ដែលមាន live record ពិតប្រាកដគ្រប់គ្រង = ធ្វើលើ TF ធំ (H1), SL ច្បាស់, គ្មាន grid/martingale, មានវគ្គពិនិត្យ MyFxBook។ មិនមាន M1 scalping មាស តាម spread ធម្មតាដដែលដែលមើលឃើញដាច់។

---

# 🏆 «Bot ល្អបំផុតពិភពលោក» vs BOT របស់អ្នក (2026)

> **ពិតគត់:** គ្មាន «bot ល្អបំផុតគ្រប់ពេលវេលា» ទេ — មានតែ bot ដែលមាន **live record ពិត វាយត្រឹមត្រូវ (MyFxBook verified)** យូរជាងគេប៉ុណ្ណោះ។ មាស (XAUUSD) ជាក្រុមដែលមាន record ពិតច្រើនជាងគេ។

## Bot កំពូលដែលមាន live ពិត (ទិន្នន័យ MyFxBook, រាយការណ៍ 2025–2026)

| Bot | Live ពិត | ប្រាក់ប្រយោជន៍/ខែ | Max DD | យុទ្ធសាស្ត្រ | តម្លៃ |
|---|---|---|---|---|---|
| **Waka Waka EA** (MT5) | **7+ ឆ្នាំ, +3,088%**, 70+ ខែឈ្នះជាប់, **ដកលុយ $4,352 ពី $500** (មូលដ្ឋានមិនបង្អាក់) | ~5% | 14.4% (ប៉ុន្តែ account មួយបាត់ ៩០%! = grid risk) | ប្លង់ពហុប៉ារ + grid | ~$1,960 |
| **Quantum Queen** (MT5, មាស) | 92 សប្តាហ៍ verified, **+1,221%** (2024: +141%, 2025: +354%, 2026 YTD +20.5%), MQL5 4.98/5 (400+ reviews) | 15–20% | 21.2% | មាស M1, 6 ម៉ូដ, grid+ត្រងត្រង | ~$1,300 |
| **Gold Trade Pro** (MT4/5) | 55+ សប្តាហ៍ verified, **+169%**, PF 1.47, 700 trades | ~5.4% | 14.8–32% | M15–H1 breakout, **គ្មាន grid/martingale**, ត្រងត្រង ML ខ្លះ | $539 |
| **Perceptrader AI** (MT5) | **207 សប្តាហ៍ (~4 ឆ្នាំ)**, +221%, **ដកលុយ $8,000 ពី $5,000** | ~1.8% | 17.1% | AI ប្លង់ | — |
| **Beetle EA** (MT4) | **5 ឆ្នាំ**, ដកលុយ **$30,000**, PF 2.60 | ~2.4% | 17.3% | ប្លង់ប្រកបដោយគុណភាព | — |
| **Forex Fury GOLD** (មាស) | ~2024 → 2026, WR 93% | ~6.3% | 19.8% | ប្លង់មាស ECN scalping | ត្រងត្រង |
| Golden Pickaxe | +1,435% ពី 2022 | ~11.5% | **50.9%** | ML + grid (ប្រមូល) | $2,520 |

⚠️ **ត្រង្គគ្មាន:** (a) «verified» មានន័យថា broker បញ្ជាក់លេខ — ប៉ុន្តែ account ដែលបាត់បង់ មិនត្រូវបានផ្សព្វផ្សាយ (survivorship bias) · (b) account តូច + leverage ខ្ពស់ = +10,000% មិនអាចចម្លងបាន · (c) គោលការណ៍ពិត: **២–៥%/ខែ ជាមួយ DD <20% គឺ «ល្អខ្លាំង»** — 20%+/ខែ ជាមួយ DD តូច ជាង ៤ ឆ្នាំ = ត្រូវបណ្ដេញ។

## 🥊 ផ្ទាល់គ្នា: BOT របស់អ្នក (v1.8.0, បាន fix) vs ក្រុមល្អបំផុត

| វិធីវាស់ | **BOT របស់អ្នក** | Waka Waka | Quantum Queen | Gold Trade Pro |
|---|---|---|---|---|
| Live ពិត (MyFxBook) | ❌ គ្មាន (trade_log 0 ដង) | ✅ 7+ ឆ្នាំ | ✅ 92 សប្តាហ៍ | ✅ 55+ សប្តាហ៍ |
| ចំណូល/ខែ (ពិត) | ❌ បាត់ ~30%/ខែ (E[R] -0.165) | +5% | +15–20% | +5.4% |
| Max DD | — (បាត់ជាប់) | 14.4% (tail: ៩០%!) | 21.2% | 14.8–32% |
| Profit factor (with cost) | **0.93** | 74% WR | — | **1.47** |
| យុទ្ធសាស្ត្រ | SMC M1–M15 + RF | ប្លង់+grid | មាស M1 grid+ត្រង | មាស M15–H1 breakout |
| Grid/Martingale | ✅ **គ្មាន — ត្រងត្រង SL គ្រប់ trade** | ❌ grid | ❌ grid | ✅ គ្មាន |
| News filter | ត្រងត្រង (SESSION_FILTER, បើកពេលនេះ) | — | — | ✅ NFP/crash filter |
| ML ពិតប្រាកដ | RF (AUC 0.51 = noise) | គ្មាន | គ្មាន | ត្រងត្រង (2025 ឡើង) |
| តម្លៃ | **$0** (របស់អ្នក) | ~$1,960 | ~$1,300 | $539 |
| អាច audit បាន | ✅ code បើក (អ្នកមើលអ្វីដែលវាធ្វើ) | ❌ បិទ | ❌ បិទ | ❌ បិទ |
| ដកលុយដែលបានវាយត្រឹមត្រូវ | $0 | $4,352 | — | — |

## 🏁 រូបភាព: អ្នកណាឈ្នះ?

**លើលទ្ធផល: Bot ដែលមាន record ពិត ឈ្នះ** — ពួកវាមាន expectancy វិជ្ជមានក្នុង live ពិត; bot របស់អ្នក (ក្រោម spread ពិត) មាន expectancy **អវិជ្ជមាន** (-0.165R/ដង)។ នេះជាចម្លើយសាមញ្ញ។

**ប៉ុន្តែ — 2 ចំណុចដែល bot របស់អ្នក ល្អជាងពួកវា:**
1. **សុវត្ថិភាពរចនា៖** bot ល្អបំផុតភាគច្រើន = grid/martingale (Waka Waka មាន account បាត់ ៩០%, Golden Pickaxe DD 51%) — ប្រាក់ប្រយោជន៍របស់ពួកវា «លាក់» drawdown រហូតដល់វាផ្ទុះ។ Bot របស់អ្នក: ត្រងត្រង SL គ្រប់ trade, daily stop, drawdown stop — បើវាបាត់ វាបាត់ **ចេញស្អាត មិនផ្ទុះ**។ ប្រសិនបើអ្នកសុំសុវត្ថិភាព ការរចនារបស់អ្នក កាន់តែធន់ ជាង «bot ល្អបំផុត»ភាគច្រើន។
2. **តម្លៃ $0 + បើក:** អ្នកមើលជម្រក code របស់ពួកវាបាន; bot ទាំងនោះ បិទជាមុខងារ។

## 📋 ផែនការដែល bot របស់អ្នក អាច «ប្រកួត» បាន
1. ប្តូរចូល **M15/H1** (ត្រងត្រង: ផ្នែក M15 របស់អ្នក E[R] ≈ 0 ហើយ; Gold Trade Pro ពិតប្រើ M15–H1)
2. បើក **SESSION_FILTER** (7–20 UTC) + បន្ថែម news/NFP filter (ដដែល Gold Reaper ធ្វើ)
3. ប្តូរគណនា **Raw/ECN** (spread មាស 0.02–0.10)
4. ដក ML RF ចេញ ជាមុនសិន (AUC 0.51 = ល្មម) ឬបង្រៀនឡើងវិញលើទិន្នន័យ ១០,០០០+
5. **Demo/MyFxBook 6–12 ខែ** → ប្រសិនបើវាមាន expectancy វិជ្ជមានពិត → ឯកសាររបស់អ្នក ក្លាយជា bot ដែលមាន record ពិត តែមួយគត់ ដែលអ្នកអាចគ្រប់គ្រងអ្វីវាធ្វើបាន

**សេចក្តីសន្និដ្ឋាន:** ពេលនេះ «bot ពិភពលោក» ដែលមាន record ពិត ឈ្នះលើលទ្ធផល។ ប៉ុន្តែ bot របស់អ្នក មានមូលដ្ឋានដែលល្អជាង (risk ច្បាស់ + បើក + ឥតគិតថ្លៃ) — បើស្នើ ៥ ជំហានខាងលើ + ប្រើពេលវេលា H1, វាអាចក្លាយជា bot ដែលអ្នក «ជឿ» បានក្នុង 6–12 ខែ ទោះបីមិនមែន «ល្អបំផុតពិភពលោក» ក៏ដោយ។

---

# ⚔️ ENGINE vs ENGINE — ប្រកួតផ្ទាល់ (គ្មាន log, គ្មាន history)

**កំណត់ស្តង់ដារដូចគ្នាសម្រាប់គ្រប់ engine:** ទីផ្សារមាសពិត 2024–2025 ដូចគ្នា · spread Exness 0.30 (stress 0.45) · 1 position · SL-first intra-bar · គណនាតាម R (1R = SL ចម្ងាយ) · មិនប្រើ history/vendor ទេ — តែ **code logic**។

| | **A — CORE របស់អ្នក ដើម** | **B — CODE របស់អ្នក v1.9.0 ថ្មី (បាន commit)** | **C — BUILD ល្អបំផុតពិភពលោក** (S/R breakout ដដែល EA មាសកំពូលបង្កើត) |
|---|---|---|---|
| តម្លាភាព logic | SMC sweep/BOS M1/M5/M15 | ដដែល + **H1 EMA50 trend gate + session 7–20 UTC + M15 priority** | ប្លង់ H1 បិទភ្នែកកាត់ S/R ថ្ងៃ/សប្តាហ៍ + momentum (body≥0.5 ATR, បិទភ្នែកក្នុង 40% ខាងលើ) + false-breakout filter + H4 trend + news window 12:30–14:30 |
| ចូល | M1 (bar បន្ទាប់) | M1/M5/M15 (M15 ជាអាទិភាព) | H1 (bar បន្ទាប់) |
| SL/TP | ATR 1.5–2.0 / 3.0–4.0 | ATR 2.0 / 4.0 (M15 ជាអាទិភាព) | ក្រោម level ±0.25 ATR (cap 2.2 ATR) / 2R |
| **trades/2 ឆ្នាំ** | 10,745 | 4,128 | **430** |
| Win rate | 35.9% | 37.9% | 35.8% |
| **PF (with spread)** | **0.93** | **1.00** | **1.15** ✅ |
| **E[R]/trade** | **-0.168** | **-0.082** | **+0.015** ✅ |
| Max DD | 37.3R | 16.8R | **4.0R** ✅ |
| ខែឈ្នះ | 3/24 | 10/24 | 11/24 |
| **cost/SL ចម្ងាយ** | 11% | 8% | **4%** ✅ |
| stress cost 0.45 | E[R] -0.288 | E[R] -0.189 | E[R] -0.015 (នៅជិតសូន្យ) |

## 🔍 តើអ្វីដែលធ្វើឱ្យ C ឈ្នះ? (មិនមែន win rate ទេ!)

Win rate ទាំង ៣ គឺ **៣៥–៣៨% ដូចគ្នាបែបៗ** — ឈ្នះ/ចាញ់ «សូន្យ» ដូចគ្នា។ អ្វីដែលខុសគ្នា ១០០% គឺ **ស្ថាបត្យកម្មសេដ្ឋកិច្ច**:

1. **cost/SL ratio (សំខាន់បំផុត):** spread $0.30 ដដែល — ប៉ុន្តែ SL របស់ A តូច (M1 ATR) → spread = 11% នៃហានិភ័យ · SL របស់ C ធំ (H1) → spread = 4% ប៉ុណ្ណោះ។ នៅ RR 2:1, breakeven គឺ WR 33.3% — cost 11% បាត់ edge ចាស់ទាំងអស់; cost 4% នៅសល់ edge តិចតួច → PF 1.15។
2. **Frequency:** A លេង 25 ដងច្រើនជាង C — រាល់ trade បង់ spread + ឱកាស whipsaw បន្ថែម។
3. **Drawdown:** A = 37R (= 37% នៃគណនា ១% risk → បើក drawdown stop 30% របស់អ្នក កណ្ដាលឆ្នាំ) · C = 4R → រស់រានមានជីវិតបន្តលេង។
4. **Trend alignment:** C លេងតែ breakout តាមត្រង H4 + momentum; A ដើម លេងប្រឆាំងត្រង H1 ច្រើន (v1.9 បានដកចេញហើយ)។

## 🏁 រូបភាព (បើ build ទាំង ២ យកទៅប្រើក្នុងជីវិតពិត រួមគ្នា)

**ENGINE C (build ល្អបំផុត) ឈ្នះ** — វាជា engine តែមួយគត់ដែលមាន expectancy វិជ្ជមានបន្ទាប់ពី spread ពិត, DD តូច, ខែឈ្នះច្រើន។
**ENGINE B (code របស់អ្នក v1.9.0 ថ្មី)** បានកាត់ការបាត់បង់ **63%** ពី A (-0.168 → -0.082R), DD ពី 37R → 17R, ខែឈ្នះពី 3/24 → 10/24 — ប៉ុន្តែនៅតែអវិជ្ជមាន: ផ្នែក M1/M5 នៅតែបង់ cost/SL 8% + ច្រើនពេក។
**ជម្រុះចុងក្រោយដែល B ត្រូវការដើម្បីឈានដល់ C:** បិទ M1 ទាំងអស់ (trade M15 តែប៉ុណ្ណោះ ដូច C ធ្វើលើ H1) — នោះ cost/SL មុខរបស់ B ចូល ៤–៥% ដូច C។ ប្រសិនបើសញ្ញា SMC របស់អ្នកនៅមាន WR ~36% នៅ M15 ដូចឥឡូវ → PF ~1.10+ → **ប្រកួតស្មើគ្នាបានហើយ**។ (តេស្តបន្ទាប់: បិទ M1/M5, M15 តែម្ដង មើលលទ្ធផល)

---

# 🏆 ENGINE C បានដាក់ចូលក្នុង BOT របស់អ្នកហើយ (v1.9.1)

**`ENTRY_ENGINE = "SR_H1"` គឺជា default ឥឡូវនេះ** — bot របស់អ្នក ឥឡូវនេះ លេងជាមួយ engine ដែលឈ្នះក្នុងការប្រកួត:

## អ្វីបានបន្ថែម
- **`core/sr_engine.py`** (ថ្មី) — ENGINE C: កម្រិត S/R ថ្ងៃ (previous-day high/low) + H1 swing pivots ដែលបានបញ្ជាក់ (10 ថ្ងៃ, ≥6h stale, cluster 0.25 ATR) · H1 close កាត់កម្រិត = breakout · momentum (body ≥ 0.5 ATR, បិទភ្នែកក្នុង 40% ខាងលើ/ក្រោម) · H4 EMA50 trend · session 7–20 UTC · news window 12:30–14:30 UTC · កម្រិតមួយ ប្រើបាន ១ ដង/ថ្ងៃ · SL = level ∓ 0.25 ATR (cap 2.2 ATR) · TP = 2R · 1 position · គ្មាន grid
- **`main.py`** — engine selector: `SR_H1` (default) ឬ `SMC_MTF` (engine ចាស់ v1.9 នៅតែមាន) · scan ១ ដង/ម៉ោង (H1 bar) · state consumed-levels រៀងរាល់ថ្ងៃ · SL/TP level-based · ML gate បំលាញ់សម្រាប់ SR (ស្របពី build ដែលបានតេស្ត)
- **`config.py`** — `ENTRY_ENGINE` + ប៉ារ៉ាម៉ែត្រ SR ទាំងអស់ (កុំប្តូរ បើចង់ឱ្យលទ្ធផលដូចដែលបានតេស្ត)

## ✅ ការបញ្ជាក់ (parity test)
Code ក្នុង repo បានរត់ឡើងវិញលើទិន្នន័យពិត 2024–2025: **426 trades · WR 36.2% · PF 1.15 · E[R] +0.020 · maxDD 4.0R** — ស្មើនឹង Engine C ក្នុងការប្រកួត (430 trades · PF 1.15 · E[R] +0.015 · DD 4.0R)។ អត្ថន័យ: **អ្វីដែលអ្នកឃើញក្នុង code = អ្វីដែលបានតេស្ត ពិតប្រាកដ**។

## របៀបប្រើ
- **Default = SR_H1** (engine ឈ្នះ) — បើក bot ដដែល `run_bot_forever.bat`
- ចង់ប្រើ engine SMC ចាស់: កែ `config.py` → `ENTRY_ENGINE = "SMC_MTF"`
- ការគ្រប់គ្រង risk ដដែល (1% risk, daily stop 20%, drawdown stop 30%, withdrawal ladder) ដំណើរការលើ engine ទាំង ២
- Telegram alerts ដំណើរការដដែល (layout ថ្មី)

## ⚠️ ការពេល
- លទ្ធផលតេស្ត = +0.02R/trade ជាមធ្យម (PF 1.15) — នេះជា edge **តូច តែវិជ្ជមាន** ជាមួយ DD តូច (4R) — មិនមែនម៉ាស៊ីនបោះពុម្ពលុយទេ; គោលការណ៍ទីផ្សារពិត គឺ ២–៥%/ខែ ជាមួយ DD <20% គឺល្អខ្លាំង
- មុនពេលលុយពិត: **demo 2–3 ខែ** មើលថា live = backtest (spread/slippage ពិត) · ប្រើ broker Raw/ECN បើអាច
- ប្រសិនបើ live ខុសពី backtest ច្រើន → បិទ SR (ENTRY_ENGINE = "SMC_MTF") ឬបិទ bot — stop បានដាក់រួច

# 🧪 Sandbox Self-Test — Paper Auto-Trade ពេញលក្ខណៈ (v1.9.2)

> រត់ដោយ **ខ្លួនឯង** ក្នុង sandbox (គ្មាន MT5 terminal, គ្មាន live feed) — ប្រើ **candle ពិត XAUUSD** + **broker សាក (fake MT5)** ដែលមាន API ដូច module `MetaTrader5` ពិត។ Bot ដែលរត់ = **`main.py` ពិត** របស់អ្នក (មិនមែន replica)។

## របៀបធ្វើ
1. **ទិន្នន័យ** — free live APIs ទាំងអស់ (stooq, binance, twelvedata) ប្រព្រឹកក្នុង sandbox → ប្រើ M1 XAUUSD ពិត 2009–2025 (GitHub, រសាត់ពី MT5)។ Window paper: **2025-09-01 → 2025-12-31** (85,680 M1 bars)។
2. **Fake broker** — spread 0.25 (Exness-like) + slippage 0.05 · SL/TP ដកធានាដោយ broker **រាល់ tick ក្នុង M1 bar** (SL ត្រួតពិនិត្យមុន TP, តាម bid/ask ពិត) · min-stop validation (reject បើ SL ខ្ចាត់) · position ticket ពិត · deals history · account/margin ពិត។
3. **Clock សាក** — bot មើល `datetime.now()` = ពេល replay · loop រត់ជាជំហាន 15 វិនាទី (management ក្រោយជាត្រឹម 15 នាទី; SL/TP ពិត ១០០% លើ tick គ្រប់ម៉ោង)។

## ✅ Unit self-test: 27/27 PASS
config គ្មាន secret · SMC pivot/sweep/BOS lag ត្រឹមត្រូវ · MTF decision + M15 priority + H1 gate · SR trigger + consumed per day · backtester next-bar-open · risk manager (lot sizing, 1-pos តាម balance, daily −20% halt, drawdown −30% halt, margin guard, withdrawal ladder) · ML train/predict + **single-class fail-closed** · state logger change-only · trade manager break-even 60% + signed progress (មិន trail លើ adverse) · connector (credentials required, forming bar skip, place→position ticket, min-stop, closed trades group fix, 7-day window)។

## 📊 លទ្ធផល Paper (luc $1,000, real candles 4.5 ខែ)
| ម៉ូត | signals | ដាក់ | បិទ | WR | PF | E[R] | PnL | maxDD |
|---|---|---|---|---|---|---|---|---|
| **1 position (config ដែលបានតេស្ត)** | 83 | 79 | 78 | **43.6%** | **1.43** | **+0.25** | **+$198.43 (+19.8%)** | $133 (13.3%) |
| 3 positions (default `MAX_POSITIONS=3`) | 93 | 91 | 90 | 42.2% | 1.32 | +0.19 | +$183.11 (+18.3%) | $181 (18.1%) |

- ខែដោយខែ (1-pos): Sep **+$64** · Oct **+$69** · Nov **−$45** · Dec **+$110** (3/4 ខែវិជ្ជមាន — ស្របពី backtest ឆ្នាំ 2024–2025 ដែល Nov–Dec 2025 ក៏ខ្លាំង)
- ML retrain ដើរពិត (rolling 30→90 trades) · withdrawal milestone $100/$200 ដើរ · day-roll risk state ដើរ · balance accounting ត្រឹមត្រូវ (ត្រួតពិនិត្យ ដល់ 1e-6)
- 2–3 signals ត្រូវបាន SKIP ដោយ SL-distance guard + 1 REJECT ដោយ min-stop (broker) — bot ចាប់បានល្អ មិន crash

## 🆚 Paper vs Backtest Parity (window ដូចគ្នា)
| | backtest parity | paper (main.py ពិត) |
|---|---|---|
| trades (បិទ) | 68 | 78 |
| WR | 41.2% | 43.6% |
| PF | 1.27 | 1.43 |
| E[R] | +0.21 | +0.25 |
| maxDD | 2.6R | 6.1R |
| ខែវិជ្ជមាន | 3/4 | 3/4 |

**68/79 (86%) trades paper = trades parity ត្រូវគ្នាត្រង់នាទី** — ភាពខុស 11 trades ជា **cascade ពី execution គ្រាប់**: backtest រក SL/TP លើ high/low របស់ H1 bar (SL មុន TP) · paper រកលើ tick M1 ពិត + spread/slippage + entry នៅ ask → កម្រិត SL/TP ធ្លាក់ ±0.1–0.3 → ពេលបិទខុស ១ម៉ោង → ខុស។ នេះជា **backtest↔live gap ពិតប្រាកដ** (មធ្យមប្រភេទ bot ពិភពលោក ខុសធំជាងនេះច្រើន — ឧ. Gold Reaper: backtest PF 2.72 → live PF 1.08)។

**ការរកឃើញ (honest):** parity backtest មាន H4 lookahead រមៀង — វាប្រើ H4 bar កំពុងបង្កើត ជាមួយតម្លៃចប់របស់វា (មិនអាចមានក្នុង live)។ ក្នុង window នេះ: parity មាន 102 signals ប៉ុន្តែ **9 ក្នុងនោះ មិនអាចកើតឡើងក្នុង bot ពិត** (ពឹងផ្អែកលើ lookahead) → **bot ពិត = 93 signals (stringent ជាង)**។ លទ្ធផល paper ខាងលើ = លទ្ធផលដែល live នឹងឃើញ។

## 🐛 Bugs រកឃើញក្នុងការ self-test នេះ — បាន FIX រួច (v1.9.2)
| កន្លែង | បញ្ហា | Fix |
|---|---|---|
| `main.py` | `datetime.utcnow()` — deprecated + crash loop ពេល datetime គ្មាន `utcnow` | `datetime.now(timezone.utc)` |
| `ml/model.py` | model មើលឃើញ LOSS មិនឃើញ WIN ម្តង → `predict` **fail-open** (ចាយទាំងអស់!) | **fail-closed**: `return False, 0.0` រហូតដល់មាន WIN ក្នុង data |
| `core/mt5_connector.py` | `place_buy/sell` ត្រឡប់ **order id** មិនមែន **position ticket** → open trade មិនត្រូវបានគ្រប់គ្រង (breakeven/trailing មិនដំណើរការ) | ត្រឡប់ position ticket ពិត (positions_get, fallback order id) |
| `core/backtester.py` | crash បើ index name មិនមែន `time` | rename មុខងារដើម្បី defensive |

## ⚠️ ការពេល (honest)
- **Management ១៥ នាទី** — breakeven/trailing/SRTP-modify ក្រោយជាត្រឹម 15 នាទី (SL/TP មុនដើររាល់ tick មិនបាត់)។ Live ពិត loop រាប់វិនាទី → ល្អជាង paper
- **ទិន្នន័យហ្វុំដល់ 2025-12-31** — sandbox គ្មាន live feed; ខែកញ្ញា–ធ្នូ 2025 ជា **period ល្អ** របស់ engine (24 ខែពេញ = +0.02R/trade, មិនមែន +0.25R ទេ)
- **`MAX_POSITIONS=3` (default) = stacking មិនទាន់បានតេស្ត** — build ដែលបានតេស្ត (duel + parity) គឺ 1 position; លទ្ធផល 1-pos ខាងលើ = អ្វីដែលបានបញ្ជាក់
- Paper ល្អ ≠ live ធានា — ដំណាក់កាលបន្ទាប់: **demo 2–3 ខែ** ប្រៀបធៀប live vs paper នេះ (spread/slippage ពិត, execution រាប់វិនាទី)
