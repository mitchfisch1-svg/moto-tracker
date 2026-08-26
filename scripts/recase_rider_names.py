"""Re-run the current name rules over rider names already in the database.

The fifth and last place the old spelling was hiding. `riders.full_name` is
written once, when a rider is first seen on a results sheet, and never
rewritten — so every rider first ingested before `titlecase_name` learned about
suffixes and Mc- prefixes kept the old rendering forever. This is the source of
truth behind standings, rider pages and /events/{id} results, which is why
"Will Canaguier Iii" survived on the phone after the scrape cache, the server's
memory and the handset's own copy had all been repaired.

Safe to run: rider MATCHING goes through `normalize_name`, which upper-cases
and strips punctuation, so "Will Canaguier Iii" and "Will Canaguier III"
already resolve to the same key. Changing the stored display name cannot split
a rider in two or orphan an alias.

Companion to scripts/recase_cached_names.py, which does the same for the
scraped-results cache.

    python scripts/recase_rider_names.py --dry-run
    python scripts/recase_rider_names.py
"""

import argparse
import os
import pathlib
import sys

import psycopg
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.names import titlecase_name  # noqa: E402
from src.resolve.riders import normalize_name  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, full_name FROM riders ORDER BY full_name")
            fixes = []
            for rid, name in cur.fetchall():
                if not name:
                    continue
                # Results sheets shout, so re-deriving from .upper() reproduces
                # exactly what the current code would store today.
                fixed = titlecase_name(name.upper())
                if fixed == name:
                    continue
                # Paranoia, cheap: never let a display fix change who this is.
                assert normalize_name(fixed) == normalize_name(name), rid
                fixes.append((rid, name, fixed))

            for rid, was, now in fixes:
                print(f"  {was:26} -> {now}")
            if not fixes:
                print("nothing to fix — every rider name matches the current rules.")
                return
            if args.dry_run:
                print(f"\n{len(fixes)} would be updated (dry run).")
                return
            cur.executemany("UPDATE riders SET full_name = %s WHERE id = %s",
                            [(now, rid) for rid, _was, now in fixes])
            print(f"\nupdated {len(fixes)} rider names.")


if __name__ == "__main__":
    main()
