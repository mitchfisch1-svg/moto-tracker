"""Rider headshots from Racer X — now the PREFERRED source, not the fallback.

Racer X publishes a rider page per athlete whose og:image is a
transparent-background cutout on a CDN that crops on demand. It started as a
gap-filler because Feld's bucket has ZERO WMX riders, but measuring the two
made it the better primary outright:

    Feld     ~500KB per photo, Cache-Control max-age=3600  (ONE HOUR)
    Racer X   ~55KB at w=320,  Cache-Control max-age=9331200 (108 DAYS)

That is ~9x smaller and cached for months instead of an hour, which is what
makes a cold launch show faces instead of number plates — iOS still has the
images on disk. Feld now only fills gaps Racer X can't.

We hotlink, exactly as we did with Feld — nothing is re-hosted.

    python scripts/sync_racerx_headshots.py --dry-run    # report, write nothing
    python scripts/sync_racerx_headshots.py             # every rider
    python scripts/sync_racerx_headshots.py --gaps-only # skip riders Feld covers

The API serves COALESCE(headshot_override, headshot_racerx, headshot_url), so a
manual override still wins and Feld still covers anyone Racer X lacks.

Three ways this could put the WRONG face on a rider, all guarded:
  * a slug that isn't a rider -> Racer X 404s, no og:image (verified)
  * a rider with no photo     -> og:image is the i/logos/post_thumb.png
                                 placeholder, which we skip (verified)
  * a slug resolving to some other rider -> the page <title> carries the rider's
                                 name, so we require it to match before storing
"""

import argparse
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import get_connection  # noqa: E402
from src.names import fold, slug_variants  # noqa: E402

RIDER_URL = "https://racerxonline.com/rider/{slug}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; MotoTracker/1.0; +https://motoxtracker.com)"}

# Racer X serves a generic post thumbnail for riders it has no photo of.
PLACEHOLDER = "i/logos/post_thumb.png"

# The CDN crops on request, so ask for the size we actually draw rather than
# the 1200x630 social card. The largest use in the app is the recap-story
# avatar at 92pt = 276px on a 3x screen, so 320 covers every surface with
# headroom — and costs 55KB instead of 197KB at w=700.
CROP = "?w=320&h=320&fit=crop&crop=faces"

_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)', re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _name_matches(full_name: str, title: str) -> bool:
    """The page title reads '<Rider Name> Information and Statistics - Racer X'.

    Compare on last name + first initial rather than the whole string: Racer X
    uses legal names where results use nicknames often enough that an exact
    match would throw away good photos.
    """
    want = re.sub(r"[^a-z ]", "", fold(full_name)).split()
    got = re.sub(r"[^a-z ]", "", fold(title))
    if not want:
        return False
    if want[-1] not in got:                      # surname must appear
        return False
    return want[0][:1] in {w[:1] for w in got.split()}


def fetch_headshot(rider):
    """(rider_id, url_or_None, note) — never raises, so one bad page can't
    abort a 200-rider sweep."""
    rid, full_name = rider
    for slug in slug_variants(full_name):
        try:
            resp = requests.get(RIDER_URL.format(slug=slug), headers=UA,
                                timeout=20, allow_redirects=True)
        except requests.RequestException as exc:
            return rid, None, f"error: {type(exc).__name__}"
        if resp.status_code == 404:
            continue
        if resp.status_code != 200:
            return rid, None, f"http {resp.status_code}"

        m = _OG_IMAGE_RE.search(resp.text)
        if not m:
            continue
        url = m.group(1)
        if PLACEHOLDER in url:
            return rid, None, "no photo (placeholder)"

        title = _TITLE_RE.search(resp.text)
        if not title or not _name_matches(full_name, title.group(1)):
            got = title.group(1).strip()[:40] if title else "?"
            return rid, None, f"name mismatch (page: {got!r})"

        return rid, url.split("?")[0] + CROP, "ok"
    return rid, None, "no rider page"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    ap.add_argument("--gaps-only", action="store_true",
                    help="only riders with no Feld photo (pre-2026-08 behaviour)")
    args = ap.parse_args()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE riders ADD COLUMN IF NOT EXISTS "
                        "headshot_racerx TEXT")
            conn.commit()

            sql = "SELECT id, full_name FROM riders"
            if args.gaps_only:
                sql += " WHERE headshot_url IS NULL"
            sql += " ORDER BY full_name"
            cur.execute(sql)
            riders = cur.fetchall()

        scope = "riders with no Feld headshot" if args.gaps_only else "riders"
        print(f"checking {len(riders)} {scope} against Racer X\n")

        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(fetch_headshot, riders))

        names = {rid: name for rid, name in riders}
        found = [r for r in results if r[1]]
        if not args.dry_run:
            with conn.cursor() as cur:
                for rid, url, _ in results:
                    # Only ever write a hit. A transient network failure must not
                    # wipe a fallback we already had.
                    if url:
                        cur.execute("UPDATE riders SET headshot_racerx = %s "
                                    "WHERE id = %s", (url, rid))
            conn.commit()

    for rid, url, note in sorted(results, key=lambda r: names[r[0]]):
        if url:
            print(f"  + {names[rid]}")
    reasons = {}
    for rid, url, note in results:
        if not url:
            reasons[note] = reasons.get(note, 0) + 1

    verb = "would fill" if args.dry_run else "filled"
    print(f"\n{verb} {len(found)} of {len(riders)} ({len(riders) - len(found)} without):")
    for note, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"    {n:4}  {note}")


if __name__ == "__main__":
    main()
