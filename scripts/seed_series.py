"""Seed the three AMA series and a current-year season for each.

Run from the project root (after init_db.py):
    python scripts/seed_series.py

Idempotent: re-running won't create duplicates.
"""

import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import get_connection, upsert  # noqa: E402

SERIES = [
    {"name": "Supercross", "abbrev": "SX", "governing_body": "AMA"},
    {"name": "Pro Motocross", "abbrev": "MX", "governing_body": "AMA"},
    {"name": "SuperMotocross", "abbrev": "SMX", "governing_body": "AMA"},
]


def main() -> None:
    year = datetime.date.today().year

    with get_connection() as conn:
        # Upsert the three series (conflict on the unique abbrev).
        upsert(conn, "series", SERIES, conflict_cols=["abbrev"])

        # Look up their ids, then upsert a season row per series for this year.
        with conn.cursor() as cur:
            cur.execute("SELECT id, abbrev FROM series")
            series_ids = {abbrev: sid for (sid, abbrev) in cur.fetchall()}

        season_rows = [
            {"series_id": series_ids[s["abbrev"]], "year": year} for s in SERIES
        ]
        upsert(conn, "seasons", season_rows, conflict_cols=["series_id", "year"])

        # Print what we ended up with.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.name, s.abbrev, se.year
                FROM seasons se
                JOIN series s ON s.id = se.series_id
                ORDER BY s.id
                """
            )
            rows = cur.fetchall()

    print(f"Seeded series + {year} seasons:")
    for name, abbrev, yr in rows:
        print(f"  - {name} ({abbrev}) — {yr}")


if __name__ == "__main__":
    main()
