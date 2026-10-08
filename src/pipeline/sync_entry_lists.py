"""Next season's riders, from the series' own entry lists.

Over the winter riders change teams, step up from 250 to 450, change numbers,
retire, and rookies arrive. None of that reaches us through results until they
race, so until then MXT shows last season's team on a rider page. The series
publishes an entry list for every class a few days before each round: number,
rider, hometown, team. That is the first official word on who is riding what,
and this reads it.

For every round in the next ENTRY_WINDOW_DAYS days:
  - find its event on the timing site (our results id, or the site's own
    event directory, matched by date and city: an upcoming round is usually
    not adopted yet)
  - read each 450 / 250 / WMX entry list
  - resolve each rider (a rookie becomes a new rider, exactly as a result
    would make one) and record number, team, bike and class for that season
    as source 'entry_list' (see src/rider_seasons.py: results outrank it, a
    hand-checked announcement does not)

Riders who retire simply stop appearing; nothing is deleted.

    python -m src.pipeline.sync_entry_lists              # the next 10 days
    python -m src.pipeline.sync_entry_lists --smx-id 518988 --year 2026 --dry-run
"""

import argparse
import datetime
import logging
import re
import sys

import requests
from bs4 import BeautifulSoup

from ..adapters.results_html import manufacturer_from_team
from ..db import get_connection
from ..resolve.riders import RiderResolver
from .. import rider_seasons

log = logging.getLogger("moto.entries")

HOME = "https://results.supermotocross.com/results/"
EVENTS_DIR = "https://results.supermotocross.com/events/"
UA = {"User-Agent": "MotoTracker/0.1 (personal project)"}
ENTRY_WINDOW_DAYS = 10
_ENTRY_LINK = re.compile(r"view_entry_list&(?:amp;)?id=(\d+)&(?:amp;)?class_id=(\d+)")


def _get(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.text


def class_of(label):
    """'450 Entry List' -> '450'. Only the championship classes; support
    classes (SMX Next, KTM Jr.) are not riders the app follows."""
    m = re.match(r"\s*(450|250|WMX)\b", label or "", re.I)
    return m.group(1).upper() if m else None


def parse_entry_list(html):
    """[(number, name, hometown, team)] off one entry list page."""
    soup = BeautifulSoup(html, "html.parser")
    for tb in soup.find_all("table"):
        out = []
        for tr in tb.find_all("tr"):
            cells = [c.get_text(" ", strip=True)
                     for c in tr.find_all(["th", "td"], recursive=False)]
            # '# | BRAND | RIDER | HOMETOWN | TEAM'; a number may carry a
            # letter ("1W"), so test the first character, not the whole cell.
            if len(cells) < 5 or not (cells[0][:1] or "").isdigit():
                continue
            out.append((cells[0].strip(), cells[2].strip(),
                        cells[3].strip() or None, cells[4].strip() or None))
        if out:
            return out
    return []


def directory():
    """[(date, name, smx_id)] for every event the timing site lists."""
    soup = BeautifulSoup(_get(EVENTS_DIR), "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=re.compile(r"view_event"))
        tds = tr.find_all("td")
        if not a or len(tds) < 2:
            continue
        m = re.search(r"id=(\d+)", a["href"])
        try:
            when = datetime.datetime.strptime(tds[-1].get_text(strip=True),
                                              "%b %d, %Y").date()
        except ValueError:
            continue
        if m:
            out.append((when, a.get_text(" ", strip=True), m.group(1)))
    return out


def find_smx_id(source_url, event_date, city, listed):
    m = re.search(r"view_event&(?:amp;)?id=(\d+)", source_url or "")
    if m:
        return m.group(1)
    # Not adopted yet: the directory entry on the same date naming the city.
    for when, name, smx in listed:
        if when == event_date and city and city.lower() in name.lower():
            return smx
    return None


def sync_event(conn, smx_id, year, label, resolver, dry_run=False):
    page = _get(f"{HOME}?p=view_event&id={smx_id}")
    seen, written = set(), 0
    for a in BeautifulSoup(page, "html.parser").find_all("a", href=True):
        m = _ENTRY_LINK.search(a["href"])
        cls = class_of(a.get_text(" ", strip=True))
        if not m or not cls or "pdf" in a["href"] or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        entries = parse_entry_list(_get(
            f"{HOME}?p=view_entry_list&id={m.group(1)}&class_id={m.group(2)}"))
        rows = []
        for number, name, _home, team in entries:
            rider_id, _ = resolver.resolve(name, number,
                                           context=f"{label} {cls} entry list")
            if rider_id is not None:
                rows.append((rider_id, number, team,
                             manufacturer_from_team(team) if team else None, cls))
        log.info("entries: %s %s — %s riders", label, cls, len(rows))
        if not dry_run:
            written += rider_seasons.record(conn, year, rows, "entry_list",
                                            note=f"{label} entry list")
    return written


def sync(conn, days=ENTRY_WINDOW_DAYS, dry_run=False):
    """Every round in the next `days` days. Cheap when there is none."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.event_date, e.city, e.venue, e.source_url, se.year
            FROM events e JOIN seasons se ON se.id = e.season_id
            WHERE e.event_date BETWEEN current_date
                                   AND current_date + make_interval(days => %s)
            ORDER BY e.event_date
            """, (days,))
        upcoming = cur.fetchall()
    if not upcoming:
        return 0
    listed = directory()
    resolver = RiderResolver(conn)
    total = 0
    for _eid, when, city, venue, url, year in upcoming:
        smx = find_smx_id(url, when, city, listed)
        if not smx:
            log.info("entries: %s (%s) is not on the timing site yet", venue, when)
            continue
        total += sync_event(conn, smx, year, venue, resolver, dry_run=dry_run)
    if not dry_run:
        conn.commit()
    return total


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--smx-id", help="one event's timing-site id, regardless of date")
    ap.add_argument("--year", type=int, help="season to record it under (with --smx-id)")
    ap.add_argument("--days", type=int, default=ENTRY_WINDOW_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    with get_connection() as conn:
        if args.smx_id:
            n = sync_event(conn, args.smx_id, args.year or datetime.date.today().year,
                           f"event {args.smx_id}", RiderResolver(conn), args.dry_run)
            if not args.dry_run:
                conn.commit()
        else:
            n = sync(conn, args.days, args.dry_run)
    print(f"entry lists: {n} rider-season row(s) written or changed")


if __name__ == "__main__":
    main()
