# ============================================================
#   core/news.py
#   Free news feed — FXStreet RSS (no API key, no new deps).
#
#   • refresh_if_due(): poll the feed every NEWS_POLL_SECONDS.
#   • New headlines matching NEWS_ALERT_KEYWORDS are returned so
#     main.py can log + Telegram-alert them (rate-limited).
#   • recent_titles(n): the newest n headlines (≤ NEWS_MAX_AGE_HOURS
#     old) for the AI decision snapshots — the AI treats them as
#     UNTRUSTED DATA (see ai_review/ai_position policies).
#   • Seen titles persist to NEWS_SEEN_PATH so a restart never
#     re-alerts the same headline.
#   • Every failure is contained: on network errors the watcher
#     serves its cached list and never crashes the trading loop.
# ============================================================

import json
import logging
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone

import config

logger = logging.getLogger("News")

_UA = "boss-smc-bot/1.9.4 (news-feed)"


class NewsWatcher:
    def __init__(self):
        raw = str(getattr(config, "NEWS_RSS_URL", "") or "")
        self.urls = [u.strip() for u in raw.split(",") if u.strip()]
        self.url = self.urls[0] if self.urls else ""
        self._good = 0                       # index of the feed that last worked
        self.poll = float(getattr(config, "NEWS_POLL_SECONDS", 600))
        self.max_age_h = float(getattr(config, "NEWS_MAX_AGE_HOURS", 12))
        self.keywords = [k.lower() for k in
                         getattr(config, "NEWS_ALERT_KEYWORDS", [])]
        self.max_alerts_h = int(getattr(config, "NEWS_MAX_ALERTS_PER_HOUR", 6))
        self.seen_path = getattr(config, "NEWS_SEEN_PATH",
                                 "SMC_Bot_Data/data/news_seen.json")
        self.items = deque(maxlen=40)        # (epoch, title) newest last
        self._last_fetch = 0.0
        self._alert_times = deque(maxlen=self.max_alerts_h)
        self._seen = self._load_seen()
        self.enabled = bool(getattr(config, "NEWS_ENABLED", True)) and bool(self.url)

    # ── persistence ──────────────────────────────────────────
    def _load_seen(self):
        try:
            with open(self.seen_path, encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("seen", [])) if isinstance(data, dict) else set()
        except Exception:
            return set()

    def _save_seen(self):
        try:
            os.makedirs(os.path.dirname(self.seen_path), exist_ok=True)
            keep = list(self._seen)[-400:]  # bounded file
            with open(self.seen_path, "w", encoding="utf-8") as f:
                json.dump({"updated": datetime.now(timezone.utc).isoformat(),
                           "seen": keep}, f)
        except Exception as e:
            logger.error(f"news seen-file write failed: {type(e).__name__}")

    # ── fetching ─────────────────────────────────────────────
    def _fetch(self):
        """Try the feeds in order, starting with the one that last worked; the
        first that parses wins. Raises the last error when all of them fail."""
        last = None
        n = len(self.urls)
        for k in range(n):
            i = (self._good + k) % n
            try:
                out = self._fetch_one(self.urls[i])
            except Exception as e:
                last = e
                continue
            if i != self._good:
                logger.info(f"news feed switched to {self.urls[i]}")
            self._good = i
            return out
        raise last if last else RuntimeError("no news feed configured")

    def _fetch_one(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                        "Accept": "application/rss+xml, application/xml, text/xml"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(262144)
        root = ET.fromstring(raw)
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if not title:
                continue
            ts = time.time()
            if pub:
                try:
                    from email.utils import parsedate_to_datetime
                    ts = parsedate_to_datetime(pub).timestamp()
                except Exception:
                    # investing.com writes "2026-10-14 20:04:18" (not RFC 822): read it
                    # as UTC instead of calling an old headline "just now"
                    try:
                        ts = datetime.strptime(pub[:19], "%Y-%m-%d %H:%M:%S") \
                            .replace(tzinfo=timezone.utc).timestamp()
                    except Exception:
                        pass
            out.append((ts, title))
        return out

    def refresh_if_due(self):
        """Poll the feed if due. Returns the list of NEW major headlines
        (keyword-matched, never seen before) for Telegram alerts."""
        if not self.enabled:
            return []
        now = time.time()
        if now - self._last_fetch < self.poll:
            return []
        self._last_fetch = now
        try:
            fetched = self._fetch()
        except Exception as e:
            logger.warning(f"news fetch failed ({type(e).__name__}); "
                           f"serving cached headlines")
            return []

        fresh_alerts = []
        known = {t for _, t in self.items}
        for ts, title in fetched:
            if title in known or title in self._seen:
                continue
            self.items.append((ts, title))
            self._seen.add(title)
            if self._is_major(title) and self._alert_budget_ok():
                fresh_alerts.append(title)
                self._alert_times.append(now)
        if fresh_alerts:
            self._save_seen()
        elif len(self._seen) % 25 == 0:
            self._save_seen()  # periodic persistence, bounded churn
        return fresh_alerts

    def _is_major(self, title):
        low = title.lower()
        return any(k in low for k in self.keywords)

    def _alert_budget_ok(self):
        hour_ago = time.time() - 3600
        return sum(1 for t in self._alert_times if t >= hour_ago) < self.max_alerts_h

    # ── AI context ───────────────────────────────────────────
    def recent_titles(self, limit=5):
        """Newest headlines within the max-age window, oldest first, each
        prefixed with its age so the AI can weigh recency. Bounded strings —
        for the AI decision snapshots only."""
        cutoff = time.time() - self.max_age_h * 3600
        now = time.time()
        out = []
        for ts, t in self.items:
            if ts >= cutoff:
                age_h = max(0.0, (now - ts) / 3600.0)
                label = ("%dh ago" % int(age_h)) if age_h >= 1 \
                    else ("%dm ago" % int(age_h * 60))
                out.append(f"({label}) {t[:110]}")
        return out[-limit:]
