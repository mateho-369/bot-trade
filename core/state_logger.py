# ============================================================
#   core/state_logger.py
#   "Life-cycle" logging — change-only, no spam.
#
#   Reading the log should feel like watching the bot's life:
#   it stays quiet while nothing changes, and speaks the instant
#   something actually happens (new signal, SL moved, trade closed).
#
#   Two tools:
#     • ChangeLogger.log(key, msg)  — logs only when msg changed for
#       that key (optional heartbeat re-log after N seconds).
#     • event(msg)                  — always logs a one-line life event
#       (OPEN / CLOSE / MODIFY / HALT ...), prefixed for easy scanning.
# ============================================================

import time
import logging

logger = logging.getLogger("LifeCycle")


class ChangeLogger:
    """Emits a log line only when the message for a key changes."""

    def __init__(self, heartbeat_seconds=0):
        # heartbeat_seconds > 0 → re-log an unchanged message after that long,
        # so a long-lived state still shows a periodic "still alive" line.
        self._last_msg  = {}
        self._last_time = {}
        self._heartbeat = heartbeat_seconds

    def log(self, key, msg, level=logging.INFO):
        now      = time.time()
        prev     = self._last_msg.get(key)
        prev_t   = self._last_time.get(key, 0)
        changed  = (msg != prev)
        stale    = self._heartbeat > 0 and (now - prev_t) >= self._heartbeat

        if changed or stale:
            logger.log(level, msg)
            self._last_msg[key]  = msg
            self._last_time[key] = now
            return True
        return False

    def forget(self, key):
        """Drop a key (e.g. when a trade closes) so it can log fresh later."""
        self._last_msg.pop(key, None)
        self._last_time.pop(key, None)


# ── Life events — always logged, clearly marked ──────────────

def event(msg, level=logging.INFO):
    """A discrete life event. Always logged, never deduped."""
    logger.log(level, f"● {msg}")


# Shared instances for the whole bot.
signal_log = ChangeLogger()                 # per-symbol/TF signal state
trade_log  = ChangeLogger(heartbeat_seconds=60)  # open-trade progress (slow heartbeat)
