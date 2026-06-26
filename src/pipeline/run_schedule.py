"""Run the schedule scraper and print what landed in the database.

From the project root:
    python -m src.pipeline.run_schedule
or:
    python src/pipeline/run_schedule.py
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.adapters.schedule_smx import ScheduleSMXAdapter  # noqa: E402
from src.db import get_connection  # noqa: E402


def main() -> None:
    adapter = ScheduleSMXAdapter()
    adapter.run()  # fetch -> normalize -> upsert (prints the row count)

    # Show the schedule we now have, grouped by series.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.abbrev, e.round_number, e.round_label, e.venue,
                       e.city, e.state, e.event_date, e.status
                FROM events e
                JOIN seasons se ON se.id = e.season_id
                JOIN series s   ON s.id  = se.series_id
                ORDER BY s.id, e.round_number
                """
            )
            rows = cur.fetchall()

    print(f"\nEvents in the database: {len(rows)}\n")
    current = None
    for abbrev, rnd, label, venue, city, state, date, status in rows:
        if abbrev != current:
            current = abbrev
            print(f"--- {abbrev} ---")
        loc = ", ".join(p for p in (city, state) if p)
        print(f"  R{rnd:<2} {str(date):<10} {venue or '?':<32} {loc:<22} [{status}]")


if __name__ == "__main__":
    main()
