"""Attach the round the results site just started serving to our event row.

    python scripts/adopt_round.py            # dry run - says what it WOULD do
    python scripts/adopt_round.py --write    # actually writes it

Race-morning tool. Safe to run repeatedly, including mid-race.

WHY THIS EXISTS
---------------
A round's own results id does not exist until the results site puts that round
on track. Until then `events.source_url` holds the generic schedule-page URL and
`events.lrm_id` is NULL, and two things silently degrade:

  * results ingest has no `view_event` id to pull, and
  * `/live` cannot derive a Live Race Media id, so it falls back to the most
    recently cached one - `ORDER BY event_date DESC`, which today is Ironman's
    MX feed. The fallback is documented as safe because the LRM feed is
    series-wide, but it has NEVER been exercised across a series boundary, and
    the SMX playoffs are a different series from the MX round it would inherit
    from.

This has happened at RedBud, Southwick and Denver, and each time it was fixed by
hand: open the results homepage, dig an id out of an asset path, write it into
the database. That is a twenty-minute job with a race starting. This makes it one
command.

THE GUARD THAT MATTERS
----------------------
The results homepage keeps serving the PREVIOUS round until the next one goes on
track - four days after Unadilla it was still listing all 25 of its sessions. So
the dangerous failure is not "no id", it is adopting the WRONG id and publishing
last month's race under this weekend's name. `_site_shows_this_round` already
encodes that rule and is used by `/live`; this script imports it rather than
keeping a second copy, because two copies of that decision is how they drift.

Exit code 0 = nothing wrong (including "the site hasn't switched yet", which is
the normal state before a round starts), 1 = something needs a human.
"""

import argparse
import pathlib
import re
import sys

import requests
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

# The site's own pages and the pure decision about whether it has moved on. All
# imported, never re-implemented - see the module docstring.
from src.api.main import (                               # noqa: E402
    _LRM_ID_RE,
    _RESULTS_HOME,
    _event_smx_id,
    _sessions_smx_id,
    _site_shows_this_round,
)
from src.db import get_connection                        # noqa: E402

API = "https://api.motoxtracker.com"
TIMEOUT = 30
HEADERS = {"User-Agent": "MotoTracker/0.1 (personal project)"}

# This prints venue and event names straight off the feed, and Windows hands you
# a cp1252 stdout the moment output is redirected. A single "Tondel" with the
# real o-slash would otherwise kill the tool mid-race. Degrade the character,
# never the tool.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):      # pragma: no cover - very old Pythons
    pass

OK, WARN, BAD, INFO = "OK  ", "WARN", "BAD ", "    "
_problems = []


def say(tag, msg):
    print(f"[{tag}] {msg}")
    if tag is BAD:
        _problems.append(msg)


# --- reading what is out there ----------------------------------------------

def site_payload():
    """What the results site is serving right now, via our own public API."""
    r = requests.get(f"{API}/live/sessions", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def lrm_from_payload(payload):
    """Pull the Live Race Media id out of a track-map asset path.

    Asset paths are shaped event_files/{lrm_id}/{smx_id}/..., so when the site
    has published a sector map both ids are already in hand.
    """
    for url in ((payload or {}).get("track_map") or {}).values():
        m = re.search(r"/event_files/(\d+)/\d+/", url or "")
        if m:
            return m.group(1)
    return None


def lrm_from_event_page(smx_id):
    """Fall back to the round's own results page, the way /live does."""
    try:
        resp = requests.get(f"{_RESULTS_HOME}?p=view_event&id={smx_id}",
                            headers=HEADERS, timeout=15)
        found = _LRM_ID_RE.search(resp.text)
    except requests.RequestException:
        return None
    return found.group(1) if found else None


def lrm_feed_answers(lrm_id):
    """Does the S3 feed actually serve this id yet?"""
    url = ("https://s3.amazonaws.com/assets.liveracemedia.com/event_files"
           f"/{lrm_id}/race.json")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        return False


# --- reading our side --------------------------------------------------------

def load_events(cur):
    cur.execute(
        """SELECT e.id, e.round_label, e.venue, e.status, e.start_time_utc,
                  e.source_url, e.lrm_id, s.abbrev AS series
             FROM events e
             JOIN seasons sn ON sn.id = e.season_id
             JOIN series s ON s.id = sn.series_id
            WHERE sn.year = 2026
            ORDER BY e.start_time_utc"""
    )
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def finished_ids(events):
    """Results-site ids for rounds we have already ingested and closed."""
    return {i for i in (_event_smx_id(e["source_url"])
                        for e in events if e["status"] == "final") if i}


def pick_target(events, explicit_id):
    if explicit_id:
        for e in events:
            if e["id"] == explicit_id:
                return e
        return None
    # The soonest round that has not been closed out - on race morning that is
    # the one on track.
    pending = [e for e in events if e["status"] != "final"]
    return pending[0] if pending else None


# --- the work ----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="commit the change (default is a dry run)")
    ap.add_argument("--event-id", type=int,
                    help="target this event instead of the next unfinished one")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a source_url that already names a round")
    args = ap.parse_args()

    print("\n== what the results site is serving ==")
    try:
        payload = site_payload()
    except requests.RequestException as exc:
        say(BAD, f"could not reach {API}/live/sessions: {exc}")
        return finish()

    theirs = _sessions_smx_id(payload)
    name = payload.get("event_name") or "(unnamed)"
    count = len(payload.get("sessions") or [])
    say(INFO, f"event name: {name}")
    say(INFO, f"sessions posted: {count}")
    if not theirs:
        say(WARN, "no results id derivable yet - the site has posted no entry "
                  "lists and no track map. Nothing to adopt; try again later.")
        return finish()
    say(OK, f"results id: {theirs}")

    print("\n== our side ==")
    with get_connection() as conn:
        with conn.cursor() as cur:
            events = load_events(cur)
            target = pick_target(events, args.event_id)
            if target is None:
                say(BAD, "no unfinished 2026 event to attach this to")
                return finish()

            done = finished_ids(events)
            ours = _event_smx_id(target["source_url"])
            say(INFO, f"target: event {target['id']} - {target['series']} "
                      f"{target['round_label']} at {target['venue']}")
            say(INFO, f"  status {target['status']}, "
                      f"gate {target['start_time_utc']:%Y-%m-%d %H:%M} UTC")
            say(INFO, f"  source_url id: {ours or 'none yet'}   "
                      f"lrm_id: {target['lrm_id'] or 'none yet'}")

            # The whole point of the script: refuse last week's race.
            if not _site_shows_this_round(ours, theirs, done):
                if theirs in done:
                    prev = next((e for e in events
                                 if _event_smx_id(e["source_url"]) == theirs),
                                None)
                    where = f" ({prev['venue']})" if prev else ""
                    say(OK, f"the site is still serving {theirs}{where}, a round "
                            "we have already closed out.")
                    say(INFO, "That is the NORMAL state until this round goes on "
                              "track. Nothing to do - run it again later.")
                else:
                    say(BAD, f"the site is serving {theirs} but this event "
                             f"already names {ours}. Refusing to guess.")
                return finish()

            if ours == theirs:
                say(OK, "already adopted - source_url already names this round.")
                if target["lrm_id"]:
                    say(OK, f"lrm_id already set: {target['lrm_id']}")
                    return finish()
                say(INFO, "but lrm_id is still empty; filling it in below.")
            elif ours and not args.force:
                say(BAD, f"event {target['id']} already names round {ours}. "
                         "Pass --force to overwrite.")
                return finish()

            print("\n== the round's Live Race Media id ==")
            lrm = lrm_from_payload(payload) or lrm_from_event_page(theirs)
            if lrm:
                say(OK, f"lrm_id: {lrm}")
                if lrm_feed_answers(lrm):
                    say(OK, "the S3 feed answers at that id")
                else:
                    say(WARN, "the S3 feed does not answer at that id yet - "
                              "normal before cars are on track, but check "
                              "/live once qualifying starts")
            else:
                inherited = next((e["lrm_id"] for e in reversed(events)
                                  if e["lrm_id"]), None)
                say(WARN, "no lrm_id derivable yet. Until one appears /live "
                          f"falls back to the most recent cached id "
                          f"({inherited}) - which belongs to a different "
                          "series and has never been tested across that "
                          "boundary. Re-run this once the site posts a track "
                          "map or an entry list.")

            new_url = f"{_RESULTS_HOME}?p=view_event&id={theirs}"
            print("\n== the change ==")
            say(INFO, f"events.source_url -> {new_url}")
            say(INFO, f"events.lrm_id     -> {lrm or '(left as is)'}")

            if not args.write:
                print("\nDry run. Nothing was written. Re-run with --write to "
                      "commit.\n")
                return finish()

            if lrm:
                cur.execute(
                    "UPDATE events SET source_url = %s, lrm_id = %s WHERE id = %s",
                    (new_url, lrm, target["id"]))
            else:
                cur.execute(
                    "UPDATE events SET source_url = %s WHERE id = %s",
                    (new_url, target["id"]))
            say(OK, f"written to event {target['id']}")

    print("\nNext: ingest results as they post with")
    print(f"    python -m src.pipeline.run_results --smx-id {theirs}")
    return finish()


def finish():
    print()
    if _problems:
        print(f"NEEDS A HUMAN - {len(_problems)} problem(s):")
        for p in _problems:
            print(f"  - {p}")
        return 1
    print("ALL CLEAR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
