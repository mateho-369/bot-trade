# ============================================================
#   core/ai_bus.py
#   The AI ⇄ bot transport lane (v2.1).
#
#   WHY THIS EXISTS
#   The bot's hot loop (0.5-1.0s per cycle) is what keeps open trades safe:
#   break-even, trailing stop, TP reconciliation, close detection. In v2.0 the
#   AI was called SYNCHRONOUSLY inside that loop — one entry review (up to 2
#   votes with the appeal) and one review per open position (2 votes each, up
#   to 12 positions), each with an 8s timeout. A slow or rate-limited provider
#   could therefore freeze the safety machinery for minutes while the market
#   moved. The "hero" was accidentally disarming the seatbelts.
#
#   HOW IT WORKS
#   Provider calls run on a small bounded worker thread. The loop:
#       submit(kind, key, fn)   -> non-blocking, returns False when saturated
#       collect(kind)           -> drains finished verdicts (never blocks)
#   The worker NEVER touches MT5, never places/closes/modifies anything and
#   never mutates config: it only computes a verdict. Every action is applied
#   later, on the main thread, after a freshness re-check — so a verdict about
#   a trade that no longer exists (or has moved) is simply discarded.
#
#   Safety properties:
#     • bounded queue (no unbounded backlog) + bounded result inbox
#     • per-key dedupe: the same candidate/ticket is never reviewed twice at
#       once, so a hammering loop can't burn the rate limit
#     • TTL: a verdict older than result_ttl is dropped as STALE (fail-closed
#       for entries, fail-safe HOLD for open positions)
#     • any exception inside a job becomes an error verdict — the loop never
#       inherits an exception from the AI thread
# ============================================================

import logging
import queue
import threading
import time
from collections import deque

logger = logging.getLogger("AIBus")


class AIBus:
    """One bounded producer/consumer lane between the trading loop and the AI."""

    def __init__(self, workers: int = 1, queue_max: int = 12,
                 result_ttl: float = 120.0, inbox_max: int = 64):
        self._q = queue.Queue(maxsize=max(1, int(queue_max)))
        self._workers = max(1, int(workers))
        self._result_ttl = float(result_ttl)
        self._inbox_max = max(4, int(inbox_max))
        self._inbox = deque()            # (finished_ts, kind, key, ok, payload)
        self._lock = threading.Lock()
        self._inflight = {}              # key -> True (dedupe)
        self._threads = []
        self._stop = threading.Event()
        self.stats = {"submitted": 0, "done": 0, "failed": 0, "dropped_full": 0,
                      "dropped_dupe": 0, "expired": 0}

    # ── lifecycle ────────────────────────────────────────────
    def start(self):
        if self._threads:
            return self
        for i in range(self._workers):
            t = threading.Thread(target=self._run, name=f"ai-bus-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)
        logger.info(f"AI bus started ({self._workers} worker(s), "
                    f"queue<={self._q.maxsize}, ttl={self._result_ttl:.0f}s)")
        return self

    def shutdown(self, timeout: float = 2.0):
        self._stop.set()
        try:
            self._q.put_nowait(None)      # wake a blocked worker
        except queue.Full:
            pass
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []

    def _run(self):
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            key, fn = job
            ok, payload = True, None
            try:
                payload = fn()
            except Exception as e:            # never let an AI error escape
                ok, payload = False, e
                logger.debug(f"AI job {key} failed: {type(e).__name__}: {e}")
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                if len(self._inbox) >= self._inbox_max:   # bounded inbox
                    self._inbox.popleft()
                self._inbox.append((time.monotonic(), key, ok, payload))

    # ── producer side ────────────────────────────────────────
    def submit(self, kind: str, key: str, fn) -> bool:
        """Queue one AI job. Returns False if the key is already in flight or
        the queue is saturated (the caller must then treat it as 'no verdict',
        which for an entry means fail-closed = no trade)."""
        if self._stop.is_set():
            return False
        full_key = f"{kind}:{key}"
        with self._lock:
            if full_key in self._inflight:
                self.stats["dropped_dupe"] += 1
                return False
            self._inflight[full_key] = time.monotonic()
        try:
            self._q.put_nowait((full_key, fn))
            self.stats["submitted"] += 1
            return True
        except queue.Full:
            with self._lock:
                self._inflight.pop(full_key, None)
            self.stats["dropped_full"] += 1
            logger.warning(f"AI queue full — {kind} review dropped "
                           f"(entries stay fail-closed)")
            return False

    def inflight(self, kind: str = None) -> int:
        with self._lock:
            if kind is None:
                return len(self._inflight)
            p = f"{kind}:"
            return sum(1 for k in self._inflight if k.startswith(p))

    def waiting(self, kind: str, key: str) -> bool:
        return f"{kind}:{key}" in self._inflight

    def drop(self, kind: str, key: str) -> int:
        """Discard pending results for a key that no longer matters (e.g. the
        position was closed while the AI was thinking). A job already running
        cannot be cancelled — its answer will simply find no owner and be
        ignored by the collector. Returns how many results were dropped."""
        full_key = f"{kind}:{key}"
        n = 0
        with self._lock:
            self._inflight.pop(full_key, None)
            items, self._inbox = list(self._inbox), deque()
            for ts, k, ok, payload in items:
                if k == full_key:
                    n += 1
                else:
                    self._inbox.append((ts, k, ok, payload))
        return n

    # ── consumer side (main loop) ────────────────────────────
    def collect(self, kind: str = None, max_items: int = 16):
        """Drain finished verdicts. Never blocks. Items are (key, ok, payload)
        where key is 'kind:id'. Results older than result_ttl are dropped and
        counted as expired."""
        out = []
        keep = []
        now = time.monotonic()
        cutoff = now - self._result_ttl
        with self._lock:
            items, self._inbox = list(self._inbox), deque()
            for ts, full_key, ok, payload in items:
                if ts < cutoff:                 # verdict too old to act on
                    self.stats["expired"] += 1
                    continue
                k = full_key.split(":", 1)[0]
                if (kind is not None and k != kind) or len(out) >= max_items:
                    keep.append((ts, full_key, ok, payload))   # someone else's
                    continue
                self.stats["done" if ok else "failed"] += 1
                out.append((full_key, ok, payload))
            self._inbox.extend(keep)
            # TTL-prune stale dedupe markers so a key can be retried later
            for kk in [kk for kk, t0 in self._inflight.items()
                       if now - t0 > max(60.0, self._result_ttl * 2)]:
                self._inflight.pop(kk, None)
        return out
