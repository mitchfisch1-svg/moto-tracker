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
    args = ap.parse_args()

    adapter = ResultsHTMLAdapter()
    with get_connection() as conn:
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
