#!/usr/bin/env python3
"""Fresh start: back up, then reset the bot's runtime data.  NEVER touches .env,
code, or your MT5 account.  Safe by default: with no flag it only SHOWS what it
would do.

  python tools/fresh_start.py              # dry run: list what would be reset
  python tools/fresh_start.py --yes        # back up, then reset
  python tools/fresh_start.py --yes --all  # also reset ML model, patterns, trade log
  python tools/fresh_start.py --yes --force  # even if the bot looks like it is running

Reset by default (all re-created by the bot on start):
  logs/*                      bot.log, crash.log ...
  bot_forever_log.txt, install_log.txt
  risk_state.json             day-start / peak equity -> re-read from the account
  ai_directives.json          regime directives (back to your baseline settings)
  ai_decisions.csv            AI journal
  news_seen.json              already-seen headlines
Kept unless --all: smc_model.pkl, smc_scaler.pkl, patterns.json, trade_log.csv

Everything removed is first copied to backups/fresh_start_<timestamp>/ (git-ignored).
"""
import os
import shutil
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)                      # config uses relative paths

import config  # noqa: E402

RUNNING_IF_LOG_NEWER_THAN_S = 20


def _data(name):
    return os.path.join(config.DATA_ROOT_PATH, "data", name)


def targets(include_all):
    t = []
    logs = os.path.dirname(config.LOG_PATH) or "logs"
    if os.path.isdir(logs):
        t += [os.path.join(logs, n) for n in sorted(os.listdir(logs))
              if os.path.isfile(os.path.join(logs, n))]
    t += ["bot_forever_log.txt", "install_log.txt"]
    t += [_data("risk_state.json"),
          getattr(config, "AI_REGIME_STATE_PATH", _data("ai_directives.json")),
          config.AI_JOURNAL_PATH, config.NEWS_SEEN_PATH]
    if include_all:
        t += [config.MODEL_PATH, config.SCALER_PATH, config.PATTERN_DATA_PATH,
              config.TRADE_LOG_PATH]
    seen, out = set(), []
    for p in t:
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            out.append(p)
    return out


def bot_seems_running():
    p = config.LOG_PATH
    return os.path.isfile(p) and (time.time() - os.path.getmtime(p)) < RUNNING_IF_LOG_NEWER_THAN_S


def main(argv):
    yes, include_all, force = "--yes" in argv, "--all" in argv, "--force" in argv
    files = targets(include_all)
    print("Fresh start" + ("" if yes else "  (DRY RUN - nothing is changed; add --yes)"))
    if not files:
        print("  nothing to reset - already fresh.")
        return 0
    total = 0
    for p in files:
        size = os.path.getsize(p)
        total += size
        print(f"  reset  {p}  ({size:,} bytes)")
    print(f"  {len(files)} file(s), {total:,} bytes."
          + ("" if include_all else "  Kept: ML model, patterns, trade log (use --all to reset them too)."))
    if not yes:
        return 0
    if bot_seems_running() and not force:
        print("\nSTOP: logs/bot.log was written in the last "
              f"{RUNNING_IF_LOG_NEWER_THAN_S}s - the bot looks like it is RUNNING.\n"
              "Close run_bot_forever.bat first (Ctrl+C), then run this again. "
              "(--force overrides.)")
        return 2
    dest = os.path.join("backups", "fresh_start_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    for p in files:
        d = os.path.join(dest, p)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(p, d)
    removed = 0
    for p in files:
        try:
            os.remove(p)
            removed += 1
        except OSError as e:
            print(f"  could not remove {p}: {type(e).__name__} (file in use? close the bot)")
    print(f"\nDone. {removed}/{len(files)} removed. Backup: {dest}")
    print("Next: run  venv\\Scripts\\python.exe preflight.py  then  run_bot_forever.bat")
    return 0 if removed == len(files) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
