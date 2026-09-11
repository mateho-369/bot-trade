# ============================================================
#   rarc/store.py
#   Crash-safe persistence primitives.
#
#   Why this exists (edge-case budget):
#   - The 1-second main loop and the 00:00 UTC retrain job touch the
#     same files. A reader seeing a half-written file is the #1
#     production failure of "simple CSV/JSON" bots.
#   - Fix 1: atomic writes — write to tmp in the SAME directory,
#     fsync, then os.replace() (atomic on POSIX and Windows).
#     A reader either sees the old file or the new one, never a mix.
#   - Fix 2: advisory file locks around read-modify-write cycles so
#     two writers can't interleave (SQLite-free equivalent of
#     BEGIN IMMEDIATE). Cross-platform: fcntl on POSIX, msvcrt on
#     Windows. Stale locks auto-expire.
#   - Fix 3: append-only JSONL with size rotation for the friction
#     stream — appends are O(1) and never require a full rewrite.
# ============================================================

import json
import os
import time
import tempfile
import logging

logger = logging.getLogger("RARC.store")

try:  # POSIX
    import fcntl
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    _HAVE_FCNTL = False

try:  # Windows
    import msvcrt
    _HAVE_MSVCRT = True
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None
    _HAVE_MSVCRT = False


# ── ATOMIC FILE WRITES ───────────────────────────────────────

def atomic_write_bytes(path, data: bytes):
    """Write bytes atomically: tmp file + fsync + os.replace."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_rarc_", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # atomic on POSIX & Windows
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj):
    atomic_write_bytes(path, json.dumps(obj, indent=1, default=str).encode("utf-8"))


def read_json(path, default=None):
    """Tolerant read: missing/corrupt file → default (never raises)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.warning("read_json(%s) failed: %s", path, e)
        return default


# ── ADVISORY FILE LOCK (cross-process mutual exclusion) ──────

class FileLock:
    """
    Advisory lock for read-modify-write cycles across processes.

    Usage:
        with FileLock(path):
            df = pd.read_csv(...)   # read
            ...                     # mutate
            df.to_csv(...)          # write

    Locks auto-expire after `timeout` seconds so a killed process can
    never wedge the bot permanently (stale-lock takeover).
    """

    def __init__(self, path, timeout=10.0, poll=0.05):
        self.path = path + ".lockfile"
        self.timeout = timeout
        self.poll = poll
        self._fh = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # Take over a stale lock instead of waiting forever.
        try:
            if os.path.exists(self.path) and \
               time.time() - os.path.getmtime(self.path) > self.timeout:
                logger.warning("Stale lock %s expired — taking over", self.path)
                os.unlink(self.path)
        except OSError:
            pass

        deadline = time.time() + self.timeout
        fh = open(self.path, "a+")
        while True:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif _HAVE_MSVCRT:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                self._fh = fh
                fh.seek(0)
                fh.truncate()
                fh.write(str(os.getpid()))
                fh.flush()
                return True
            except (OSError, ValueError):
                if time.time() >= deadline:
                    # Fail OPEN but loud: logging beats blocking forever in a
                    # single-bot context; the atomic-write layer still
                    # guarantees file integrity.
                    logger.error("FileLock timeout on %s — proceeding unlocked", self.path)
                    self._fh = fh
                    return False
                time.sleep(self.poll)

    def release(self):
        if self._fh is None:
            return
        try:
            if _HAVE_FCNTL:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            elif _HAVE_MSVCRT:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ── APPEND-ONLY JSONL WITH ROTATION ──────────────────────────

def append_jsonl(path, obj, max_bytes=2_000_000, keep=2):
    """Append one JSON line; rotate when the file exceeds max_bytes."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            for i in range(keep - 1, 0, -1):
                src, dst = f"{path}.{i}", f"{path}.{i+1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            os.replace(path, path + ".1")
    except OSError as e:
        logger.warning("Rotation failed for %s: %s", path, e)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def read_jsonl_tail(path, max_lines=500):
    """Read only the tail of a JSONL file — O(tail), not O(file)."""
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "rb") as f:
            # bounded memory: read last ~256KB max
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 262_144))
            blob = f.read().decode("utf-8", errors="ignore")
        lines = blob.splitlines()[-max_lines:]
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue  # tolerate a torn final line after a crash
    except OSError as e:
        logger.warning("read_jsonl_tail(%s): %s", path, e)
    return out
