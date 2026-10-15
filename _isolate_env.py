"""Import this FIRST in every verify_*.py (before `import config`).

config.py reads the owner's real .env (BOT_PROFILE=scalper, SCALP_*, keys...).
Tests that assert profile-specific defaults must not depend on it, so this
points BOT_ENV_FILE at an empty file and drops every setting that config reads
from the environment. A test that needs a setting passes it explicitly.
"""
import os
import tempfile

_f = tempfile.NamedTemporaryFile(prefix="empty_bot_env_", suffix=".env", delete=False)
_f.close()
os.environ["BOT_ENV_FILE"] = _f.name
for _k in list(os.environ):
    if _k.startswith(("AI_", "SCALP_", "MT5_", "TELEGRAM_", "NEWS_")) or _k in (
            "BOT_PROFILE", "ENTRY_ENGINE", "SYMBOLS", "LOT_SIZE", "MAX_POSITIONS",
            "RISK_PERCENT_PER_TRADE", "ENABLE_BUY", "ENABLE_SELL"):
        del os.environ[_k]
