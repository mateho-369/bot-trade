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
