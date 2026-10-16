# P1 — Windows Runbook (សម្រាប់អ្នករត់នៅ MT5 Demo Terminal)

> គោលដៅ: ទាញ history ពិតរបស់ XAUUSDm → រត់ SR_H1 baseline backtest លើ data ពិត →
> ទទួល `research/P1_report.md` ដែលប្រៀបធៀបនឹងលេខឯកសារ (426 trades · PF 1.15 · E[R] +0.020R)។
> ទាំងអស់នេះ **read-only**: គ្មាន order ណាមួយត្រូវបានដាក់ទេ។

## ០) មុនចាប់ផ្តើម (5 នាទី)

1. បើក **MetaTrader 5** (Exness) → login គណនី **DEMO** (`Exness-MT5Trial6`) → ធីក
   `Algo Trading` ឱ្យបើក។ ឆែកថា `XAUUSDm` មានក្នុង Market Watch (បើគ្មាន: right-click → Show All)។
2. **សំខាន់ — បើក history ឱ្យអស់សិន (បើរំលងជំហាននេះ អ្នកនឹងទាញបានតែប៉ុន្មានខែ):**
   - ទៅ `Tools → Options → Charts` → ដាក់ **Max bars in chart = Unlimited** (និង
     Max bars in history បើមាន) → ចុច OK → **បិទ MT5 ហើយបើកវិញ (restart terminal)**។
   - បើក **chart XAUUSDm timeframe M1** ទុកចោលម្ដង ខ្លាច់ terminal ទាញ/load history
     របស់ symbol នេះសិន (រង់ចាំដល់ chart លែងឃើញ "Waiting for update")។
3. បើក terminal ក្នុង VS Code នៅ folder repo នេះ ហើយ `git pull` ឱ្យបាន code P1 ចុងក្រោយ។
4. Secrets នៅ `~/.boss-bot/.env` ដដែល (MT5_LOGIN / MT5_PASSWORD / MT5_SERVER) — **កុំ paste
   password ទៅកន្លែងណាផ្សេង, កុំផ្ញើមកខ្ញុំ**។

## ១) ដំឡើង packages ម្ដង (2-10 នាទី តាម internet)

```bat
install.bat
venv\Scripts\python.exe -m pip install pyarrow==17.0.0 pytest
```

Expected: `install.bat` ចប់ដោយគ្មាន `[ERROR]`; command ទី២ បង្ហាញ `Successfully installed ... pyarrow-17.0.0 ... pytest-...`។
(`requirements.txt` មាន `pyarrow==17.0.0` រួចហើយ ដូច្នេះ install.bat លើកក្រោយនឹងដំឡើងវាដោយខ្លួនឯង។)

## ២) រត់ offline tests មុន (ក្រោម 1 នាទី) — ផ្ទៀងផ្ទាត់ថា code មិនខូច

```bat
venv\Scripts\python.exe -m pytest tests\ -q
venv\Scripts\python.exe tools\scan_secrets.py --all
```

Expected:
```
.................................                                        [100%]
33 passed in 0.xx s
secret scan: clean
```
បើឃើញ `failed` → **ឈប់ត្រឹមនេះ** ហើយផ្ញើ output មកខ្ញុំ (កុំរត់ជំហានបន្ទាប់)។

## ៣) រត់ ONE command សម្រាប់ P1 ទាំងមូល (នេះជា command ចម្បង)

```bat
venv\Scripts\python.exe tools\run_phase.py P1
```

វាធ្វើតាមលំដាប់: (ក) បើ `data\XAUUSDm_H1.parquet` មិនទាន់មាន → ហៅ
`tools\download_history.py` ដោយខ្លួនឯង; (ខ) backtest SR_H1; (គ) សរសេរ `research\P1_report.md`។

**ពេលវេលាប្រមាណ:** download លើកដំបូង **10-60 នាទី** (M1 ច្រើនឆ្នាំ + ticks 30 ថ្ងៃ, តាម terminal/internet);
backtest **1-15 នាទី** តាមចំនួន H1 bars។ លើកក្រោយៗ (data មានរួច) ចប់ក្នុងប៉ុន្មាននាទី។

Expected output (លេខប្រែប្រួលតាម data ពិតរបស់អ្នក):
```
account server: Exness-MT5Trial6 (DEMO ok)
symbol_info -> data\XAUUSDm_symbol_info.json (digits=2 point=0.01 ...)
M1: ....... bars .... -> data\XAUUSDm_M1.parquet
M5: ....... bars .... -> data\XAUUSDm_M5.parquet
M15: ...... bars .... -> data\XAUUSDm_M15.parquet
H1: ...... bars .... -> data\XAUUSDm_H1.parquet
spread-by-hour -> data\XAUUSDm_spread_by_hour.csv (overall median $0.2xx)
ticks: ....... rows -> data\ticks\ (1 file/day)     <- ឬ "broker returned none -- skipped gracefully"
MANIFEST.json: 4 parquet files indexed
source: REAL MT5 DATA
H1 bars: ..... · signals: ... · trades: ...
net: WR ...% · PF ... · E[R] ... · maxDD ...R · cost ...R
stress x1.5: PF ... · E[R] ...
report -> research/P1_report.md
```

## ៤) អានលទ្ធផល ហើយផ្ញើមកខ្ញុំ

បើក `research\P1_report.md` — មានតារាង "Reproduction check vs documented duel"។
**ផ្ញើមកខ្ញុំ:** (1) file `research\P1_report.md` ទាំងមូល, (2) file `data\MANIFEST.json`
(តូច, គ្មាន secret, commit បាន), (3) ប្រាប់ខ្ញុំថា data range ចាប់ពីឆ្នាំណាដល់ឆ្នាំណា។

កុំផ្ញើ: `.env`, password, API keys។ Parquet files ធំៗ **មិនបាច់ commit** (`.gitignore` បានបិទរួច) —
ខ្ញុំត្រូវការតែ report + manifest ដើម្បីវិនិច្ឆ័យថា backtester reproduce បានត្រឹមត្រូវឬអត់។

Tolerance ដែលបានប្រកាសមុន (ក្នុង report): បើ data window = 2024-2025 ដូចឯកសារ →
trades ±15%, PF ±0.10, E[R] ±0.01R ទើបហៅថា "reproduced"។ បើ window ខុសគ្នា លេខគ្រាន់តែ indicative —
ខ្ញុំនឹងប្រាប់ត្រង់ៗ។

## ៥) បញ្ហាដែលអាចជួប

| ឃើញ | មូលហេតុ/ដំណោះស្រាយ |
|-----|---------------------|
| `[FIX] mt5.initialize() failed` | MT5 terminal មិនទាន់បើក ឬមិនទាន់ login → បើក terminal, login demo, រត់ម្ដងទៀត |
| `[FIX] symbol XAUUSDm not found` | XAUUSDm មិននៅក្នុង Market Watch → right-click Market Watch → Show All |
| `ticks: broker returned none` | មិនមែន error ទេ — broker មិនឱ្យ tick history; bot ប្រើ bar spread ជំនួស (បានកត់ក្នុង report) |
| Download យឺតខ្លាំងនៅ M1 | ធម្មតាលើកដំបូង (chunk 30 ថ្ងៃម្ដងៗ)។ ទុកឱ្យរត់ចប់; លើកក្រោយលឿន |
| tests ក្លាយជា failed | ឈប់ ហើយផ្ញើ output មកខ្ញុំ — មានន័យថា code មានបញ្ហាពិត |
| Server មិនបង្ហាញ `(DEMO ok)` | អ្នកមិននៅលើ demo ទេ — download នៅតែ read-only ប៉ុន្តែ **កុំ** រត់ bot រហូតដល់ដូរទៅ demo |

## ៦) បន្ទាប់ពីជំហាននេះ

ខ្ញុំទទួល report របស់អ្នក → វិនិច្ឆ័យ reproduction → សរសេររបាយការណ៍ P1 (section-8 format) →
ឈប់រង់ចាំ "OK" សម្រាប់ P2។ **គ្មាន demo order ណាមួយនៅក្នុង P1 ទេ។**
