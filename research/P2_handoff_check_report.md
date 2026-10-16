# របាយការណ៍បន្ត Handoff — P2 checker (2026-10-16)

## ១. អ្វីដែលបានធ្វើ

- `git fetch origin arena/a951958a-bot-trade` និង `git rev-parse HEAD FETCH_HEAD`:
  ទាំងពីរ = `6a54fbbfef35d6eb8b131c915c411481107576a1`។ មិនបាន reset/merge។
- Branch បច្ចុប្បន្ន៖ **`arena/ba18f2e6-bot-trade`**។ មិនប៉ះ `main`, មិន force-push។
- អាន audit/decisions/P1 report/P2 plan/runbook និង research/backtester/cost/tools/tests។
- រក្សា full owner spec ក្នុង `docs/V3_SPEC.md`។
- បន្ថែម `tools/check_p2.py` និង `tests/test_check_p2.py`។
  ឯកសារប្រើប្រាស់ និងកម្រិតនៃភស្តុតាង៖ `docs/P2_CHECKER.md`។
- មិនប្តូរ engine, costs, backtester, research semantics, plan, runner,
  downloader ឬ fixture ចាស់។ មិនបន្ថែម `--workers`; មិនសុំ rerun P2។
- មិនអាន/កែ `.env`, មិនភ្ជាប់ MT5, មិនហៅ AI provider, មិនដាក់ orders។

## ២. លទ្ធផលពិតពី commands

Environment: Linux, Python **3.11.2** (មិនមែនការផ្ទៀងផ្ទាត់ Python 3.12/Windows/MT5 ទេ)។
ដំឡើង packages ក្នុង `.venv` ដោយរំលង MetaTrader5៖

```sh
python3 -m venv .venv
.venv/bin/python -m pip install pandas==2.1.4 numpy==1.26.4 scikit-learn==1.4.2 ta==0.11.0 joblib==1.3.2 python-dotenv==1.0.1 pyarrow==17.0.0 pytest
```

| Command | លទ្ធផល |
|---|---|
| `.venv/bin/python -m pytest tests -q` មុនបន្ថែម checker | **48 passed** |
| `.venv/bin/python -m pytest tests -q` ក្រោយបន្ថែម checker | **75 passed in 8.05s**, គ្មាន warning |
| `.venv/bin/python verify_ai_entry.py` | **43 tests, OK** |
| `.venv/bin/python verify_ai_flow.py` | **42 tests, OK** |
| `.venv/bin/python verify_fix.py` | **2 tests, OK** |
| `.venv/bin/python verify_scalper_flow.py` | **17 tests, OK** |
| `.venv/bin/python verify_secret_guard.py` | **18 tests, OK** |
| `.venv/bin/python verify_twin_brain.py` | **91 tests, OK** |
| `.venv/bin/python verify_v17.py` | **3 tests, OK** |
| `.venv/bin/python tools/scan_secrets.py --all` | **secret scan: clean** |

សរុប legacy៖ **7 scripts / 216 tests**, exit 0 ទាំងអស់។
Checker បន្ថែម **27 tests** រួមក្នុង pytest 75 ខាងលើ។
`verify_fix.py` បង្ហាញថា Telegram disabled ព្រោះមិនដំឡើង package `telegram`;
ដូច្នេះនេះមិនមែនជាការផ្ទៀងផ្ទាត់ Telegram integration ពិតទេ។

### Fixture parity (មិនមែនលទ្ធផល trading)

តេស្តក្នុង `tests/test_check_p2.py` ហៅ runner ដើមដោយមិនកែ៖

- Fixture ខ្លីដែលមានស្រាប់៖ engine ទាំង 5 លើ H1; គ្មាន OOS ព្រោះខ្លីជាង
  train window។ Gate failures និងលេខត្រូវនឹង runner។
- Fixture price ដដែល ប្តូរ timestamps ជា synthetic hourly bars ក្នុង temp directory
  ដើម្បីមានប្រវែងលើស train window៖ PULLBACK-H1 មាន OOS trades។
  E[R], PF, bootstrap CI95, DD, DSR និង profitable-fold fraction
  មាន **full-precision equality** ក្រោយ CSV round-trip; failure list ត្រូវគ្នា។
- Controls ក្នុង integration tests ប្រើ 2 seeds សម្រាប់តេស្ត plumbing តែប៉ុណ្ណោះ;
  **មិនជំនួស 200 seeds នៃ P2 ពិត**។ ទាំងនេះមិនមែនការស្រាវជ្រាវ real-data ថ្មី
  ហើយមិន reset trial counter នោះទេ។
- Checker ពិនិត្យ file bytes មុន/ក្រោយ ដើម្បីបញ្ជាក់ថាមិនសរសេរ outputs វិញ។

## ៣. PASS/FAIL និងដែនកំណត់

**Engineering checks: PASS**។ **P2 strategy gates: មិនទាន់វិនិច្ឆ័យ** —
មិនទាន់ទទួល real P2 outputs ពី Windows។ គ្មានការអះអាង profitability។
SR_H1 baseline FAIL នៅតែជាសេចក្តីសម្រេចចាស់ មិនបានកែឱ្យ PASS។

Checker បោះពុម្ព gate ទាំង 11 នៃ runner v1 រួមទាំង random និង long-only controls។
លេខពី trade CSV ត្រូវគណនាឡើងវិញ; stress/PBO/perturbation/control p95
មានតែក្នុង rounded leaderboard ដូច្នេះ label ថា runner-reported។
មិនអាចស្តារលេខ precision ដែលមិនបាន export ឡើយ; មិនប្រើលេខបង្គត់ដើម្បីប្ដូរ FAIL ទៅ PASS។

**ចំណុចសម្រាប់ reviewer មុនអះអាងថាឆ្លង section 6 ទាំងមូល**
(កត់ពី code ដែលបានអាន; មិនមែនកែ semantics ឬផែនការថ្មី)៖

1. Profitable-fold denominator របស់ v1 មិនរាប់ empty folds។
2. Perturbation PF ប្រើ full sample modal combo មិនមែន OOS។
3. Controls ប្រើ full bar frame និង modal settings; realised count មិនត្រូវបាន export។
4. DD formula ចាប់ peak ពី equity របស់ trade ទីមួយ មិនមែន zero។
5. Train selection ចែក trades តាម entry time ប៉ុន្តែមិន purge trades ដែល exit
   ក្រោយ cutoff។
6. Outputs ដើមមិនអាចបញ្ជាក់ seed count, cumulative prior trials ឬ code/data provenance
   បានពេញលេញ; ត្រូវរក្សា original command, revision និង manifest។

ហេតុនេះ checker PASS = **runner-v1 parity** មិនមែន independent certification
ឬការអនុញ្ញាត orders។ បើមាន real row PASS ត្រូវពិនិត្យបញ្ហាទាំងនេះជាមួយ reviewer
មុនសម្រេច P3។ ការកែ research semantics ត្រូវការការអនុម័ត និង plan version ថ្មី។

## ៤. ការសម្រេចចិត្តដែលត្រូវការពីម្ចាស់

មិនត្រូវការរត់ P2 ឡើងវិញ ឬផ្តល់ secrets ទេ។ សូមផ្ញើ original P2 leaderboard,
original command/revision និង manifest ពេលការរត់បច្ចុប្បន្នចប់។
មិនទាន់មានសំណើសុំចាប់ផ្តើម P3/P4/P5។

## ៥. ជំហានបន្ទាប់ — STOP

ទុក Windows job ឱ្យចប់សិន មុន pull branch។ បន្ទាប់មក local agent អាច pull
`arena/ba18f2e6-bot-trade` ហើយរត់ checker តែប៉ុណ្ណោះ៖

```bat
venv\Scripts\python.exe tools\check_p2.py --leaderboard research\P2_leaderboard.md --trades-dir data\research
```

Exit 0 = all v1 gates pass; 1 = gate failures; 2 = missing/inconsistent evidence។
រង់ចាំម្ចាស់ paste P2 output; មិនបន្ត phase ដោយខ្លួនឯង។
