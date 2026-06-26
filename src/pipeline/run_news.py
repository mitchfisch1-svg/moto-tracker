"""Run the news RSS scraper and print the latest headlines.

From the project root:
    python -m src.pipeline.run_news
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.adapters.news_rss import NewsRSSAdapter  # noqa: E402
from src.db import get_connection  # noqa: E402


def main() -> None:
    NewsRSSAdapter().run()  # fetch -> normalize -> upsert (prints per-feed counts)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM news_articles")
            total = cur.fetchone()[0]
            cur.execute(
                """
                SELECT a.published_at, s.name, a.title
                FROM news_articles a
                LEFT JOIN sources s ON s.id = a.source_id
                ORDER BY a.published_at DESC NULLS LAST
                LIMIT 10
                """
            )
            rows = cur.fetchall()

    print(f"\nTotal articles in the database: {total}")
    print("Latest 10 headlines:\n")
    for published_at, source, title in rows:
        when = published_at.strftime("%Y-%m-%d %H:%M") if published_at else "    (no date)   "
        print(f"  {when}  [{source or '?':<13}]  {title}")


if __name__ == "__main__":
    main()
