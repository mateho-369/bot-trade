"""Offline test setup: isolate .env BEFORE any repo module reads config.

Same pattern as the repo's verify_*.py suite (_isolate_env.py), plus
sys.path so `core.*` / `tools` imports work from the repo root.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _isolate_env  # noqa: F401,E402  (must run before `import config`)
