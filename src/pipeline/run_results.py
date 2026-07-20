"""Ingest race results and recompute standings.

From the project root:
    python -m src.pipeline.run_results --smx-id 508725   # one event (verify)
    python -m src.pipeline.run_results                    # all completed rounds
    python -m src.pipeline.run_results --limit 3          # first 3 completed rounds

Each completed event's `source_url` carries its SuperMotocross event id
(view_event&id=...), which is how we reach its results.
"""

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.adapters.results_html import ResultsHTMLAdapter  # noqa: E402
from src.db import get_connection  # noqa: E402
from src.resolve.riders import RiderResolver  # noqa: E402
from src.standings import recompute_standings  # noqa: E402

_SMX_ID_RE = re.compile(r"view_event&id=(\d+)")

# Recovering rounds the schedule page never linked ---------------------------
# supermotocross.com/schedule/ often never publishes a round's results link, so
# events.source_url stays the generic schedule URL, select_events() skips the
# event, and the round SILENTLY never ingests. That has now cost us RedBud,
# Denver, Southwick and Spring Creek — the last one left live App Store users on
# stale standings (and the wrong 250 championship leader) for two days.
#
# The results homepage always shows the current/most recent event and carries
# event_files/{lrm}/{smx} in its asset paths, so the id can be recovered from
# there. Venue-matched against the page title so we can never staple one round's
# id onto another.
_RESULTS_HOME = "https://results.supermotocross.com/results/"
_ASSET_RE = re.compile(r"event_files/(\d+)/(\d+)")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)


def resolve_missing_event_ids(conn):
    """Backfill source_url/lrm_id for rounds whose results link was never posted.

    Returns a list of (event_id, venue, smx_id, lrm_id) recovered. Safe to call
    every run: events that already carry a view_event id are ignored.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, venue, event_date FROM events
            WHERE COALESCE(source_url, '') NOT LIKE '%%view_event%%'
              AND venue IS NOT NULL
            ORDER BY event_date DESC
            """
        )
        candidates = cur.fetchall()
    if not candidates:
        return []

    try:
        import requests
        html = requests.get(_RESULTS_HOME, timeout=20).text
    except Exception as exc:                      # network hiccup — try next run
        print(f"  results homepage unreachable ({exc}); skipping id recovery")
        return []

    asset = _ASSET_RE.search(html)
    if not asset:
        return []
    lrm_id, smx_id = asset.group(1), asset.group(2)
    title_m = _TITLE_RE.search(html)
    title = (title_m.group(1) if title_m else "").lower()
    if not title:
        return []

    # Only the newest venue match wins, so a same-venue round from a previous
    # season can't hijack the current event's id.
    for eid, venue, _date in candidates:
        if venue.lower() in title:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE events SET source_url = %s, lrm_id = %s WHERE id = %s",
                    (f"{_RESULTS_HOME}?p=view_event&id={smx_id}", lrm_id, eid),
                )
            conn.commit()
            print(f"  recovered results id for {venue}: "
                  f"smx={smx_id} lrm={lrm_id} (schedule page never linked it)")
            return [(eid, venue, smx_id, lrm_id)]
    return []


def select_events(conn, smx_id=None, limit=None, target_status="final"):
    """Return event dicts to ingest (only those with a results event id).

    With smx_id set, returns just that event regardless of status. Otherwise
    returns events whose status == target_status ('final' to backfill completed
    rounds, 'live' for the scheduler's during-event polling).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.season_id, s.abbrev, e.source_url, e.round_label,
                   e.round_number, e.status
            FROM events e
            JOIN seasons se ON se.id = e.season_id
            JOIN series  s  ON s.id  = se.series_id
            ORDER BY se.id, e.round_number
            """
        )
        rows = cur.fetchall()

    events = []
    for eid, season_id, abbrev, source_url, label, rnd, status in rows:
        m = _SMX_ID_RE.search(source_url or "")
        if not m:
            continue  # no results page linked (e.g. an upcoming round)
        this_smx = m.group(1)
        if smx_id is not None:
            if this_smx != str(smx_id):
                continue
        elif status != target_status:
            continue
        events.append(
            {
                "event_id": eid,
                "season_id": season_id,
                "series_abbrev": abbrev,
                "smx_id": this_smx,
                "label": f"{abbrev} R{rnd} {label or ''}".strip(),
            }
        )
    return events[:limit] if limit else events


def print_standings(conn, season_ids):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.abbrev, st.class, st.position, r.full_name,
                   st.points, st.wins, st.podiums
            FROM standings st
            JOIN seasons se ON se.id = st.season_id
            JOIN series  s  ON s.id  = se.series_id
            JOIN riders  r  ON r.id  = st.rider_id
            WHERE st.season_id = ANY(%s)
            ORDER BY se.id, st.class, st.position
            """,
            (list(season_ids),),
        )
        rows = cur.fetchall()

    current = None
    for abbrev, cls, pos, name, points, wins, podiums in rows:
        key = (abbrev, cls)
        if key != current:
            current = key
            print(f"\n--- {abbrev} {cls} standings ---")
        if pos and pos <= 10:
            print(f"  {pos:>2}. {name:<24} {points:>3} pts  "
                  f"({wins}W {podiums}P)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smx-id", help="ingest only this SuperMotocross event id")
    ap.add_argument("--limit", type=int, help="cap the number of events")
    ap.add_argument("--recompute-only", action="store_true",
                    help="rebuild standings from existing results (no scraping)")
    args = ap.parse_args()

    if args.recompute_only:
        with get_connection() as conn:
            recompute_standings(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT season_id FROM standings")
                season_ids = [row[0] for row in cur.fetchall()]
            print("Standings recomputed.")
            print_standings(conn, season_ids)
        return

    adapter = ResultsHTMLAdapter()
    with get_connection() as conn:
        # Recover any round the schedule page never linked, so it stops being
        # silently skipped below.
        if not args.smx_id:
            resolve_missing_event_ids(conn)
        events = select_events(conn, smx_id=args.smx_id, limit=args.limit)
        if not events:
            print("No matching events to ingest.")
            return

        print(f"Ingesting results for {len(events)} event(s)...\n")
        resolver = RiderResolver(conn)
        season_ids = set()
        for ev in events:
            self_total = adapter.ingest_event(conn, ev, resolver=resolver)
            season_ids.add(ev["season_id"])

        print(
            f"\nRiders — linked: {resolver.linked}, created: {resolver.created}, "
            f"flagged for review: {resolver.flagged}"
        )

        for sid in season_ids:
            recompute_standings(conn, season_id=sid)

        print_standings(conn, season_ids)


if __name__ == "__main__":
    main()
