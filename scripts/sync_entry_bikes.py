"""Fill riders.manufacturer from Racer X entry lists, which state the bike.

We derive a rider's bike from their TEAM name ("Rockstar Energy Husqvarna" ->
Husqvarna), which works for factory riders and fails completely for privateers:
"Jordan Jarvis Racing", "Spiker Racing", "Moto Fit Club" carry no brand at all.
That's most of the WMX field — 7 of 40 had a bike.

The live LRM feed does know every rider's bike, and _backfill_manufacturers in
the API captures it during a race, but WMX only runs a handful of rounds and
its three 2026 rounds all predate that backfill, so nothing filled them in.

Racer X publishes a per-round entry list with an explicit Bike column
("Honda CRF250R Works Edition", "GasGas MC 250"), which covers riders BEFORE
they've turned a wheel — the gap neither of the other two sources can close.

    python scripts/sync_entry_bikes.py --dry-run
    python scripts/sync_entry_bikes.py

Only fills riders whose manufacturer is NULL; it never overwrites a make we
already derived, so this can't fight the live feed or the team parser.
"""

import argparse
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.results_html import manufacturer_from_team  # noqa: E402
from src.db import get_connection  # noqa: E402

BASE = "https://racerxonline.com"
# Entry-list links are discovered from these rather than guessing venue slugs,
# which differ from our venue names ("High Point" vs "high-point-raceway").
SCHEDULES = [f"{BASE}/mx/schedule", f"{BASE}/wmx/schedule"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; MotoTracker/1.0; +https://motoxtracker.com)"}

_ENTRY_HREF_RE = re.compile(r'href="([^"]*entry-list[^"]*)"')


def _entry_hrefs(url):
    try:
        resp = requests.get(url, headers=UA, timeout=25)
        resp.raise_for_status()
    except requests.RequestException:
        return set()
    return {h if h.startswith("http") else BASE + h
            for h in _ENTRY_HREF_RE.findall(resp.text)}


def _venue_slug(venue: str) -> str:
    return re.sub(r"\s+", "-", re.sub(r"[^a-z0-9 ]", "", venue.lower().strip()))


def season_rounds():
    """(year, [venue]) for the current Pro Motocross season, from our schedule."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT s.year, e.venue FROM events e
            JOIN seasons s ON s.id = e.season_id
            JOIN series se ON se.id = s.series_id
            WHERE se.abbrev = 'MX' AND s.year = (SELECT max(year) FROM seasons)
            ORDER BY e.start_time_utc
        """)
        rows = cur.fetchall()
    return rows[0][0] if rows else None, [v for _, v in rows]


def entry_list_urls() -> list[str]:
    """Every round's list, not just the next one.

    Racer X keeps past rounds up, and that matters: only 15 riders entered the
    Unadilla WMX round while 33 have scored points this season, so the earlier
    rounds are where most of the field's bikes are. The schedule pages only ever
    link the upcoming round (and only one class of it), so build the URLs from
    our own schedule and keep whichever ones actually parse.
    """
    year, venues = season_rounds()
    urls = set()
    if year:
        for venue in venues:
            slug = _venue_slug(venue)
            urls.add(f"{BASE}/wmx/{year}/{slug}/wmx/entry-list")
            for cls in ("450", "250"):
                urls.add(f"{BASE}/mx/{year}/{slug}/{cls}/entry-list")
    # Plus whatever the schedule links directly, in case a venue slug differs
    # from our venue name.
    for sched in SCHEDULES:
        urls |= _entry_hrefs(sched)
    return sorted(urls)


def parse_entry_list(url):
    """[(rider_name, number, bike_text)] from one entry list page."""
    try:
        resp = requests.get(url, headers=UA, timeout=25)
        resp.raise_for_status()
    except requests.RequestException:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        header = [c.get_text(" ", strip=True).lower()
                  for c in rows[0].find_all(["th", "td"])]
        if "rider" not in header or "bike" not in header:
            continue
        ri, bi = header.index("rider"), header.index("bike")
        ni = header.index("number") if "number" in header else None
        out = []
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) <= max(ri, bi):
                continue
            name, bike = cells[ri].strip(), cells[bi].strip()
            num = cells[ni].strip() if ni is not None and ni < len(cells) else None
            if name and bike:
                out.append((name, num, bike))
        return out
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    urls = entry_list_urls()
    with ThreadPoolExecutor(max_workers=6) as ex:
        pages = list(ex.map(parse_entry_list, urls))
    live = [(u, p) for u, p in zip(urls, pages) if p]
    print(f"{len(live)} of {len(urls)} candidate entry lists exist")
    for u, entries in live:
        print(f"    {len(entries):4} riders  {u}")
    pages = [p for _, p in live]

    # name -> make. Entry lists overlap between rounds; last one wins, and they
    # agree in practice since a rider's bike doesn't change mid-season.
    makes = {}
    unparsed = set()
    for entries in pages:
        for name, _num, bike in entries:
            make = manufacturer_from_team(bike)
            if make:
                makes[name.strip().lower()] = make
            else:
                unparsed.add(bike)
    print(f"\n{len(makes)} riders with a readable bike across all entry lists")
    if unparsed:
        print(f"  unrecognised bike text: {sorted(unparsed)[:6]}")

    filled = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, full_name FROM riders WHERE manufacturer IS NULL")
            missing = cur.fetchall()
            for rid, full_name in missing:
                make = makes.get(full_name.strip().lower())
                if not make:
                    continue
                filled.append((full_name, make))
                if not args.dry_run:
                    cur.execute(
                        "UPDATE riders SET manufacturer = %s "
                        "WHERE id = %s AND manufacturer IS NULL", (make, rid))
        if not args.dry_run:
            conn.commit()

    for name, make in sorted(filled):
        print(f"  + {name:26} {make}")
    verb = "would fill" if args.dry_run else "filled"
    print(f"\n{verb} {len(filled)} of {len(missing)} riders missing a bike make")


if __name__ == "__main__":
    main()
