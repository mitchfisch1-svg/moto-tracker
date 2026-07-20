"""Seed the RSS news sources into the `sources` table.

Run from the project root (after init_db.py):
    python scripts/seed_sources.py

Idempotent: upserts on the unique source name, so re-running won't duplicate.

Feed URLs were verified by fetching each one and parsing it with feedparser.
Vital MX serves a valid RSS feed that is currently empty (no <item>s); it's
included with its real URL in case it starts publishing items later.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import get_connection, upsert  # noqa: E402

SOURCES = [
    {
        "name": "Gate Drop",
        "url": "https://gatedrop.com",
        "feed_url": "https://gatedrop.com/feed/",
        "type": "rss",
        "active": True,
    },
    {
        "name": "Vurb Moto",
        "url": "https://vurbmoto.com",
        "feed_url": "https://vurbmoto.com/feed/",
        "type": "rss",
        "active": True,
    },
    {
        # International (MXGP) coverage — broadens us past the AMA paddock.
        "name": "MXGP",
        "url": "https://www.mxgp.com",
        "feed_url": "https://www.mxgp.com/rss.xml",
        "type": "rss",
        "active": True,
    },
    {
        "name": "Racer X",
        "url": "https://racerxonline.com",
        "feed_url": "https://racerxonline.com/feeds/rss/posts",
        "type": "rss",
        "active": True,
    },
    {
        "name": "Vital MX",
        "url": "https://www.vitalmx.com",
        "feed_url": "https://www.vitalmx.com/rss.xml",
        "type": "rss",
        "active": True,
    },
    {
        "name": "PulpMX",
        "url": "https://pulpmx.com",
        "feed_url": "https://pulpmx.com/feed/",
        "type": "rss",
        "active": True,
    },
    {
        "name": "MX Vice",
        "url": "https://mxvice.com",
        "feed_url": "https://mxvice.com/feed/",
        "type": "rss",
        "active": True,
    },
    {
        "name": "Swapmoto Live",
        "url": "https://swapmotolive.com",
        "feed_url": "https://swapmotolive.com/feed/",
        "type": "rss",
        "active": True,
    },
]


def main() -> None:
    with get_connection() as conn:
        upsert(
            conn,
            "sources",
            SOURCES,
            conflict_cols=["name"],
            update_cols=["url", "feed_url", "type", "active"],
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, feed_url, active FROM sources "
                "WHERE type = 'rss' ORDER BY name"
            )
            rows = cur.fetchall()

    print(f"Seeded {len(rows)} RSS source(s):")
    for name, feed_url, active in rows:
        flag = "active" if active else "inactive"
        print(f"  - {name:<14} [{flag}] {feed_url}")


if __name__ == "__main__":
    main()
