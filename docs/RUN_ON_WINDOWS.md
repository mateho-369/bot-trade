# របៀបដំឡើង និងរត់នៅ Windows (Runbook v2.1)

> ខ្លីបំផុត៖ **pull → install → .env → preflight → run_bot_forever.bat** គ្រប់ជំហាន
> មានការត្រួតពិនិត្យរបស់វា។ បើអ្វីមួយខ្វះ អ្នកឃើញ `[FIX]` ច្បាស់ៗ មុនពេល bot រត់
> (មិនមែនរត់រហូតដល់ crash កណ្តំាឆាកដូច v2.0 ទេ)។

## ១) ត្រូវការ

| របស់ | សេចក្តីលម្អិត |
|---|---|
| Windows 10/11 64-bit | `MetaTrader5` ដំណើរការតែ Windows (Linux/macOS ត្រូវ Wine) |
| Python **3.10–3.12** 64-bit | ឆែក «Add Python to PATH»។ 3.13+ ធ្វើឱ្យ `pip install` បរាជ័យ (numpy/MetaTrader5 wheels) |
| MetaTrader 5 terminal | ចូលគណនី **Demo** ហើយបើកទុក |
| «Allow algorithmic trading» | Tools → Options → Expert Advisors → ឆែក «Allow algorithmic trading» (បើមិនឆែក ការដាក់ order អាចត្រូវបដិសេធ) |
| API key សម្រាប់ AI (ជ្រើស) | OpenAI / Groq / OpenRouter / local vLLM — គ្មានវា ក៏ bot នៅរត់ តែគ្មាន AI |

## ២) ៥ ជំហាន

```bat
git pull
install.bat
notepad .env
venv\Scripts\python.exe preflight.py
run_bot_forever.bat
```

1. **`install.bat`** — បង្កើត `venv`, ដំឡើង `requirements.txt`, រក្សាទុកការ import ពិតប្រាកដ, ហើយ **ចម្លង `.env.example` → `.env`** បើវាមិនទាន់មាន។
2. **កែ `.env`** — មានតែ ៣ បន្ទាត់ដែល **ចាំបាច់**: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`។
   សម្រាប់ AI: `AI_PROVIDER=openai_compat` + `AI_API_KEYS=sk-...`។
3. **`preflight.py`** — រត់រាល់ ៧ ផ្នែក (packages → .env → គណនី → ហានិភ័យ → AI → ផុលឌ័រ)។
   ចង់ឱ្យវាសាកល្បងជាមួយ terminal ពិត: `venv\Scripts\python.exe preflight.py --connect`
   (វា **មិនដាក់ order អ្វីទាំងអស់** — គ្រាន់ត symbol info + ticks)។
4. **`run_bot_forever.bat`** — preflight ម្តងទៀត រួចរត់ bot; បើ bot ដួល → បោះពុម្ព 12 បន្ទាត់ចុងក្រោយ
   នៃ `logs\bot.log` ហើយចាប់ឡើងវិញក្នុង 5s (error ⇒ 20s ដើម្បីកុំឱ្យ crash-loop)។
5. **Ctrl+C ពីរដង** ដើម្បីឈប់។ បិទ terminal MT5 = ឈប់ទាំងស្រុង។

## ៣) រាល់ពេលចាប់ផ្តើម៖ អាន ៦ បន្ទាត់នេះ

```
BOSS SMC BOT  v2.1  —  AI ⇄ Bot flow  [balanced]
FLOW | AI entry gate: ON          ← បើឃើញ "requested ON but OFFLINE" = គ្មាន key
FLOW | AI position manager: ON
FLOW | AI directives (regime): ON
♥ ALIVE | XAUUSDm 4000.25 | equity $... | open positions: 0 |
          AI-entry:ON | AI-positions:ON | AI-directives:ON | queue=0 | engine SR_H1 | profile balanced
```

| អ្វីដែលមើល | អត្ថនាម |
|---|---|
| `queue=0` | AI worker ទាន់ពេល។ `queue` កើនជាប់ = provider យឺត (ការពារដោយ auto-drop + backoff) |
| `AI-entry:OFF` | មិនមាន key ឬ flag បិទ → bot នៅដាក់ជួលដោយគ្មានការរាំង (fail-closed មានន័យតែពេល AI ON) |
| `● SKIP … AI reject cooldown` | AI បាន REJECT កន្លងមក 45s → សំណួរថ្មីនៅមិនទាន់ |
| `● AI verdict STALE` | លទ្ធផលចាស់ពេក (bar ផ្លាស់ប្តូរ/ផ្លាស់ទី >0.5 ATR) — ត្រឹមត្រូវហើយ |
| `● AI EXTEND refused … already extended once` | ការពារម្តង/ទីតាំង — ត្រឹមត្រូវហើយ |

## ៤) បញ្ហាទូទៅ (v2.1 បង្ហាញភាគច្រើនដោយស្វ័យប្រវត្តិ)

| សញ្ញា | មូលហេតុ + ដំណោះស្រាយ |
|---|---|
| `[FIX] MT5_LOGIN is empty/0` | `.env` មិនទាន់លេខគណនី ឬ bot មិនអាច login ខ្លួនឯងបាន |
| `mt5.initialize() failed` | terminal MT5 មិនបើក ឬផ្លូវខុស — បើក terminal ហើយសាក `--connect` ម្តងទៀត |
| `symbol 'XAUUSDm' not found` | suffix ខុសពី broker → កែ `SYMBOLS=` (ឧ. `XAUUSD`, `XAUUSDm`, `GOLD`) |
| គ្មានជួលទេ រាប់ម៉ោង | ធម្មតាសម្រាប់ `SR_H1` (សញ្ញាតិច: bar H1 នីមួយៗ + 4 symbols)។ ស្វែងរកពាក្យ `● SKIP` ក្នុង log ដើម្បីដឹងថាអ្វីជារាំង |
| ជួលទាំងអស់ត្រូវរាំងដោយ `spread cost …% of stop` | spread ធំពេលនេះ (ECN/Raw account ជួយ 11%→4%) |
| `AI … OFFLINE` | ដាក់ `AI_API_KEYS` នៅក្នុង `.env` រួច start ឡើងវិញ |

## ៥) មុនលុយពិត — ៣ យ៉ាងប៉ុណ្ណោះ

1. **demo 2–3 ខែ** ហើយប្រៀបធៀប `trade_log.csv` vs backtest (សំខាន់ជាង «រត់មួយសប្តាហ៍ហើយល្អ»)
2. គណនី **Raw/ECN** (spread 0.02–0.10) — ការស្រាវជ្រាវរបស់ repo បង្ហាញថា spread គឺជាមូលហេតុ #1
3. `RISK_PERCENT_PER_TRADE=0.5` និងកុំបិទ `DAILY_MAX_LOSS_PERCENT` / `MAX_DRAWDOWN_PERCENT` —
   ទាំងនេះជាអ្វីដែល AI មិនអាចប៉ះទាំងអស់ (by design)

ឯកសារលំហូរពេញលេញ (រូបភាព + តារាងការកែ 17 ចំណុច): `docs/FLOW_AI_BOT.md`
