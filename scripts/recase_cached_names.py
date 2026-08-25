"""Re-run the current name rules over already-cached session results.

`scraped_session_cache` has no expiry, by design: a finished session's order
never changes. Its SPELLING does. When `titlecase_name` learned about suffixes
and Mc- prefixes (576284b), every board cached before that kept serving the old
rendering — "Will Canaguier Iii" was still on the Program screen days after the
fix shipped, because nothing had asked the site for that session since.

Session payloads are built from the results table's SHOUTED cells, so
upper-casing a stored name and re-titlecasing it reproduces exactly what the
current code would emit. That makes this a pure, idempotent repair rather than
a guess — and it does not depend on the results site still serving the event,
which for older rounds it does not.

Covers the session boards (`view_*`) and the entry lists (`entries:*`), which
are built from the same shouted tables. NOT `wmx:standings`, which comes from a
page publishing mixed case where `titlecase_name` leaves the source alone on
purpose — re-deriving there would fight the live code rather than agree with
it. And not `team`, which is passed through exactly as the series spells it;
"Mcgrath Powersports" is their rendering to get wrong, not ours.

    python scripts/recase_cached_names.py --dry-run
    python scripts/recase_cached_names.py
"""

import argparse
import json
import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.names import titlecase_name  # noqa: E402


def recase(node, fixes):
    """Rewrite every name field in place; collect what changed."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("name", "full_name") and isinstance(v, str) and v:
                fixed = titlecase_name(v.upper())
                if fixed != v:
                    fixes.append((v, fixed))
                    node[k] = fixed
            else:
                recase(v, fixes)
    elif isinstance(node, list):
        for v in node:
            recase(v, fixes)
    return node


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                r"SELECT cache_key, payload FROM scraped_session_cache "
                r"WHERE cache_key LIKE 'view\_%' OR cache_key LIKE 'entries:%' "
                r"ORDER BY cache_key"
            )
            rows = cur.fetchall()

            touched = names = 0
            seen = set()
            for key, payload in rows:
                fixes = []
                fixed_payload = recase(payload, fixes)
                if not fixes:
                    continue
                touched += 1
                names += len(fixes)
                seen.update(fixes)
                if not args.dry_run:
                    cur.execute(
                        "UPDATE scraped_session_cache SET payload = %s "
                        "WHERE cache_key = %s",
                        (json.dumps(fixed_payload), key),
                    )
            for was, now in sorted(seen):
                print(f"  {was:26} -> {now}")
            verb = "would fix" if args.dry_run else "fixed"
            print(f"\n{verb} {names} names across {touched} cached boards "
                  f"({len(seen)} distinct riders).")


if __name__ == "__main__":
    main()
