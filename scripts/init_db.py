"""Apply schema.sql to the database in DATABASE_URL.

Run from the project root:
    python scripts/init_db.py

Idempotent: schema.sql uses CREATE TABLE IF NOT EXISTS, so re-running is safe.
"""

import pathlib
import sys

# Make the project root importable so `from src...` works when run as a script.
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import get_connection  # noqa: E402

SCHEMA_PATH = ROOT / "schema.sql"


def main() -> None:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)

        # Report which tables now exist so you can confirm at a glance.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
                """
            )
            tables = [row[0] for row in cur.fetchall()]

    print("Schema applied. Tables in the database:")
    for t in tables:
        print(f"  - {t}")


if __name__ == "__main__":
    main()
