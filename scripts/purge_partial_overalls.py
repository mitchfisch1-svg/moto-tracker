"""Drop cached Overalls that were scraped between the motos.

`scraped_session_cache` has no expiry by design — a finished session's order
never changes, so the DB copy is authoritative. An Overall scraped before the
second moto breaks that assumption: it is a table of the same shape, the same
riders and half the race, and once written it is the answer forever. Budds
Creek's WMX board read "1---- 25 pts" for two days; Unadilla's 250 and 450 read
that way for nine, through a one-point title fight.

The API no longer writes these (see _overall_is_settled). This clears the ones
already stored, so the next request re-scrapes the finished board. Deleting is
safe: every key here is a cache of a public page.

    python scripts/purge_partial_overalls.py --dry-run
    python scripts/purge_partial_overalls.py
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

from src.api.main import _overall_block_is_settled  # noqa: E402


def poisoned(cur):
    """Cache keys holding an Overall that is a moto short."""
    cur.execute(
        """
        SELECT cache_key, payload, updated_at FROM scraped_session_cache
        WHERE cache_key LIKE 'overall:%'
           OR cache_key LIKE 'view_multi_main_result:%'
        ORDER BY cache_key
        """
    )
    for key, payload, updated in cur.fetchall():
        # 'overall:{smx}' holds one block per class; the session key holds one
        # board. A single unsettled class spoils the whole event key.
        blocks = (payload if key.startswith("overall:")
                  else [{"label": key, "rows": payload.get("results") or [],
                         **({"settled": payload["settled"]}
                            if "settled" in payload else {})}])
        bad = [b["label"] for b in blocks
               if not _overall_block_is_settled(b)]
        if bad:
            yield key, updated, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            hits = list(poisoned(cur))
            for key, updated, bad in hits:
                print(f"  {key:40} cached {updated:%Y-%m-%d %H:%M} -- {', '.join(bad)}")
            if not hits:
                print("nothing to purge — every cached Overall has both motos.")
                return
            if args.dry_run:
                print(f"\n{len(hits)} key(s) would be deleted (dry run).")
                return
            cur.execute(
                "DELETE FROM scraped_session_cache WHERE cache_key = ANY(%s)",
                ([k for k, _, _ in hits],),
            )
            print(f"\ndeleted {cur.rowcount} key(s).")


if __name__ == "__main__":
    main()
