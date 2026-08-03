"""Fill headshot gaps from Racer X, where Feld has no photo at all.

Feld's bucket (sync_headshots.py) stays the primary source, but it only covers
70 of 261 riders and — the reason this exists — it has ZERO WMX riders, so the
entire women's field rendered as faceless number plates. Racer X publishes a
rider page per athlete whose og:image is a transparent-background cutout on a
CDN that crops on demand, which is the same visual style as Feld's headshots.

We hotlink, exactly as we do with Feld — nothing is re-hosted.

    python scripts/sync_racerx_headshots.py --dry-run   # report, write nothing
    python scripts/sync_racerx_headshots.py             # fill the gaps
    python scripts/sync_racerx_headshots.py --all       # also riders Feld covers

The API serves COALESCE(headshot_override, headshot_url, headshot_fallback), so
a manual override still wins, Feld is still preferred, and this only ever shows
up where there was nothing before.

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

# The CDN crops on request. Feld's headshots are square, so ask for the same
# shape and let it centre on the face rather than the 1200x630 social card.
CROP = "?w=700&h=700&fit=crop&crop=faces"

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
    ap.add_argument("--all", action="store_true",
                    help="also sync riders who already have a Feld headshot")
    args = ap.parse_args()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE riders ADD COLUMN IF NOT EXISTS "
                        "headshot_fallback TEXT")
            conn.commit()

            sql = "SELECT id, full_name FROM riders"
            if not args.all:
                sql += " WHERE headshot_url IS NULL"
            sql += " ORDER BY full_name"
            cur.execute(sql)
            riders = cur.fetchall()

        scope = "all riders" if args.all else "riders with no Feld headshot"
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
                        cur.execute("UPDATE riders SET headshot_fallback = %s "
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
