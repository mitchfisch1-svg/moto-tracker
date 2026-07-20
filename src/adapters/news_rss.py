"""News adapter: pull headlines from the active RSS sources.

Loops every active source of type 'rss' in the `sources` table, parses its feed
with feedparser, and upserts each entry into `news_articles` keyed on the unique
url so re-runs never duplicate.
"""

import datetime
import time

import feedparser
import requests
from bs4 import BeautifulSoup

from ..db import get_connection, upsert
from .base import BaseAdapter

# Identifies us honestly (name + contact URL) but keeps the conventional
# "Mozilla/5.0 (compatible; ...)" shape that most CDNs require — the bare
# "MotoTracker/0.1" form was getting 403'd by several publishers.
# Sites that block this too (MXA, Direct Motocross) are deliberately refusing
# crawlers, so we leave them alone rather than spoofing a full browser.
USER_AGENT = "Mozilla/5.0 (compatible; MotoTracker/1.0; +https://motoxtracker.com)"

# Be polite: pause between feed fetches.
REQUEST_DELAY_SECONDS = 1.0

# Keep summaries reasonable — strip HTML and cap length.
SUMMARY_MAX_CHARS = 1000


class NewsRSSAdapter(BaseAdapter):
    name = "news_rss"

    # --- fetch -------------------------------------------------------------
    def fetch(self):
        """Read active RSS sources from the DB and fetch each feed.

        Returns a list of (source_id, source_name, parsed_feed) tuples.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, feed_url
                    FROM sources
                    WHERE type = 'rss' AND active = TRUE AND feed_url IS NOT NULL
                    ORDER BY name
                    """
                )
                sources = cur.fetchall()

        results = []
        for i, (sid, name, feed_url) in enumerate(sources):
            try:
                resp = requests.get(
                    feed_url, headers={"User-Agent": USER_AGENT}, timeout=30
                )
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
                print(f"[{self.name}] {name}: {len(parsed.entries)} entries")
                results.append((sid, name, parsed))
            except Exception as exc:  # one bad feed shouldn't stop the rest
                print(f"[{self.name}] {name}: ERROR {exc}")
            if i < len(sources) - 1:
                time.sleep(REQUEST_DELAY_SECONDS)

        return results

    # --- normalize ---------------------------------------------------------
    def normalize(self, raw) -> list[dict]:
        rows = []
        for sid, _name, parsed in raw:
            for entry in parsed.entries:
                url = entry.get("link")
                if not url:
                    continue
                rows.append(
                    {
                        "source_id": sid,
                        "title": (entry.get("title") or "(untitled)").strip(),
                        "url": url.strip(),
                        "summary": self._clean_summary(entry.get("summary")),
                        "author": entry.get("author"),
                        "published_at": self._published(entry),
                    }
                )
        return rows

    # --- upsert ------------------------------------------------------------
    def upsert(self, conn, rows: list[dict]) -> int:
        # news_articles.url is unique; the same url can appear twice in a batch
        # (e.g. cross-posted). Dedupe within the batch — Postgres rejects a
        # single INSERT ... ON CONFLICT that touches the same key twice.
        deduped = {}
        for row in rows:
            deduped.setdefault(row["url"], row)

        return upsert(
            conn,
            "news_articles",
            list(deduped.values()),
            conflict_cols=["url"],
            update_cols=["source_id", "title", "summary", "author", "published_at"],
        )

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _clean_summary(summary):
        if not summary:
            return None
        text = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
        if not text:
            return None
        return text[:SUMMARY_MAX_CHARS]

    @staticmethod
    def _published(entry):
        # feedparser exposes parsed time tuples (UTC) when it can read a date.
        tm = entry.get("published_parsed") or entry.get("updated_parsed")
        if not tm:
            return None
        return datetime.datetime(*tm[:6], tzinfo=datetime.timezone.utc)
