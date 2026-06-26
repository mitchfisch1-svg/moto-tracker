"""Database helpers: a connection context manager and a generic upsert().

Everything that touches Postgres goes through here so connection handling and
the INSERT ... ON CONFLICT pattern live in one place.
"""

from contextlib import contextmanager
from typing import Iterable, Mapping, Sequence

import psycopg

from .config import get_database_url


@contextmanager
def get_connection():
    """Yield a psycopg connection, committing on success and rolling back on error.

    Usage:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    """
    conn = psycopg.connect(get_database_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert(
    conn,
    table: str,
    rows: Iterable[Mapping[str, object]],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
) -> int:
    """Insert rows, updating on conflict. Returns the number of rows processed.

    Args:
        conn:           an open psycopg connection.
        table:          target table name.
        rows:           an iterable of dicts mapping column -> value. All dicts
                        must share the same set of keys.
        conflict_cols:  the columns that form the unique/PK conflict target.
        update_cols:    columns to overwrite on conflict. Defaults to every
                        inserted column that isn't part of conflict_cols. Pass
                        an empty list to make conflicts a no-op (DO NOTHING).

    Example:
        upsert(conn, "events", rows,
               conflict_cols=["season_id", "round_number"])
    """
    rows = list(rows)
    if not rows:
        return 0

    columns = list(rows[0].keys())
    if update_cols is None:
        update_cols = [c for c in columns if c not in conflict_cols]

    col_list = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    conflict_list = ", ".join(conflict_cols)

    if update_cols:
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        conflict_action = f"DO UPDATE SET {set_clause}"
    else:
        conflict_action = "DO NOTHING"

    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_list}) {conflict_action}"
    )

    with conn.cursor() as cur:
        cur.executemany(sql, [tuple(row[c] for c in columns) for row in rows])

    return len(rows)
