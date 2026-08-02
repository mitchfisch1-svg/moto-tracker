"""Check rider headshots for staleness and report the ones worth overriding.

    python scripts/audit_headshots.py            # report only
    python scripts/audit_headshots.py --set 7 https://example.com/x.png
    python scripts/audit_headshots.py --clear 7

Feld stays the source of truth: headshot URLs are name-slugged
(feld-smx-rider-headshots/firstname-lastname.png) so they survive team changes,
and when Feld re-shoots a rider we pick the new image up for free.

What that DOESN'T cover is Feld simply not re-shooting. DiFrancesco moved off
GasGas but his photo is still the Oct-2025 one, so he's in last season's kit.
This asks each image for its Last-Modified and flags any rider whose photo
predates their team change — that's the automatic signal that a manual override
is warranted, rather than anyone eyeballing 260 riders.
"""

import argparse
import datetime
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import get_connection  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (compatible; MotoTracker/1.0; +https://motoxtracker.com)"}


def head_mtime(url):
    """Last-Modified for an image, or None if it can't be read."""
    try:
        r = requests.head(url, headers=UA, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            return None
        lm = r.headers.get("Last-Modified")
        return parsedate_to_datetime(lm) if lm else None
    except Exception:
        return None


def audit(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, full_name, team, headshot_url, headshot_override,
                   team_changed_at
            FROM riders
            WHERE headshot_url IS NOT NULL
            ORDER BY full_name
            """
        )
        riders = cur.fetchall()

    urls = [r[3] for r in riders]
    with ThreadPoolExecutor(max_workers=8) as ex:
        mtimes = list(ex.map(head_mtime, urls))

    now = datetime.datetime.now(datetime.timezone.utc)
    stale, unreadable = [], []
    with conn.cursor() as cur:
        for (rid, name, team, url, override, changed), mtime in zip(riders, mtimes):
            cur.execute(
                "UPDATE riders SET headshot_source_mtime = %s, "
                "headshot_checked_at = %s WHERE id = %s",
                (mtime, now, rid),
            )
            if mtime is None:
                unreadable.append((rid, name))
            elif changed and mtime < changed and not override:
                # The photo is older than the team switch — it shows the old kit.
                stale.append((rid, name, team, mtime, changed))
    conn.commit()
    return stale, unreadable, len(riders)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", nargs=2, metavar=("RIDER_ID", "URL"),
                    help="pin a rider's headshot to a specific image")
    ap.add_argument("--clear", metavar="RIDER_ID",
                    help="drop the override and go back to the Feld image")
    args = ap.parse_args()

    with get_connection() as conn:
        if args.set:
            rid, url = args.set
            with conn.cursor() as cur:
                cur.execute("UPDATE riders SET headshot_override = %s WHERE id = %s",
                            (url, int(rid)))
                conn.commit()
                cur.execute("SELECT full_name FROM riders WHERE id = %s", (int(rid),))
                row = cur.fetchone()
            print(f"override set for {row[0] if row else rid}: {url}")
            return
        if args.clear:
            with conn.cursor() as cur:
                cur.execute("UPDATE riders SET headshot_override = NULL WHERE id = %s",
                            (int(args.clear),))
                conn.commit()
            print(f"override cleared for rider {args.clear}")
            return

        stale, unreadable, total = audit(conn)

    print(f"checked {total} headshots\n")
    if stale:
        print("STALE — photo predates the rider's team change:")
        for rid, name, team, mtime, changed in stale:
            print(f"  [{rid}] {name} — now {team}")
            print(f"        photo {mtime:%Y-%m-%d}, team changed {changed:%Y-%m-%d}")
        print("\n  Fix one with:")
        print(f"  python scripts/audit_headshots.py --set {stale[0][0]} <image-url>")
    else:
        print("no stale headshots — every photo is newer than its team change")
    if unreadable:
        print(f"\nunreadable ({len(unreadable)}): "
              + ", ".join(n for _, n in unreadable[:8])
              + (" ..." if len(unreadable) > 8 else ""))


if __name__ == "__main__":
    main()
