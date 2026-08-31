"""Assert what must be TRUE of production, not what a function returns.

Every bug that has cost this app a race weekend passed the unit suite. None of
them were logic errors - the logic was fine, and tested. They were data errors:

    Ironman ingested zero results         a crash on a path only new data reaches
    Combined Qualifying showed July       a cache key naming a class, not a session
    A half-scored Overall pinned forever  a cache with no completeness gate
    "Will Canaguier Iii" in five places   a presentation change that never migrated
    SMX standings blank                   an official table fetched and discarded
    "Can't reach MXT" on the home screen  an endpoint too slow for a widget

A pure function cannot see any of that. These checks look at what the app is
actually serving right now, and each is named after the bug that motivated it,
so the suite grows by exactly one check every time something gets past us.

    python scripts/audit.py            # against production
    python scripts/audit.py --db-only  # skip the HTTP checks

Exit code is the number of failing invariants, so cron and CI can gate on it.
"""

import argparse
import datetime
import os
import pathlib
import re
import sys
import time

import psycopg
import requests
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.names import titlecase_name                    # noqa: E402
from src.api.main import _overall_block_is_settled      # noqa: E402

API = os.environ.get("MXT_API", "https://moto-tracker-api.onrender.com")
UA = {"User-Agent": "MotoTracker-audit/1.0"}

_checks = []


def check(name, motive):
    """Register one invariant. `motive` is the bug it exists because of."""
    def wrap(fn):
        _checks.append((name, motive, fn))
        return fn
    return wrap


# --- what the database must hold --------------------------------------------
@check("recent final rounds have results", "Ironman ingested zero results")
def _results_exist(cur, _http):
    cur.execute(
        """
        SELECT e.venue, e.event_date,
               (SELECT count(*) FROM results r JOIN sessions s ON s.id = r.session_id
                WHERE s.event_id = e.id) AS n
        FROM events e
        WHERE e.status = 'final'
          AND e.event_date >= (now() - interval '30 days')::date
        ORDER BY e.event_date DESC
        """)
    return [f"{v} ({d}) has no results" for v, d, n in cur.fetchall() if n == 0]


@check("stored rider names match the current rules",
       "Will Canaguier Iii survived in five separate places")
def _rider_names(cur, _http):
    cur.execute("SELECT full_name FROM riders WHERE full_name IS NOT NULL")
    return [f"{n} would render as {titlecase_name(n.upper())}"
            for (n,) in cur.fetchall() if titlecase_name(n.upper()) != n]


@check("cached names match the current rules",
       "the scrape cache kept an old spelling long after the fix shipped")
def _cached_names(cur, _http):
    cur.execute("SELECT cache_key, payload::text FROM scraped_session_cache "
                "WHERE cache_key LIKE 'view_%' OR cache_key LIKE 'entries:%'")
    bad = []
    for key, txt in cur.fetchall():
        for name in sorted(set(re.findall(r'"(?:name|full_name)": "([^"]+)"', txt))):
            if titlecase_name(name.upper()) != name:
                bad.append(f"{key}: {name}")
    return bad


@check("every cached Overall is a finished one",
       "a board scraped between the motos was pinned forever")
def _overalls_settled(cur, _http):
    cur.execute("SELECT cache_key, payload FROM scraped_session_cache "
                "WHERE cache_key LIKE 'overall:%'")
    bad = []
    for key, payload in cur.fetchall():
        for b in payload or []:
            if not _overall_block_is_settled(b):
                bad.append(f"{key}: {b.get('label')} is part-scored")
    return bad


@check("no cache key names a class instead of a session",
       "Combined Qualifying served Southwick's board at every round after it")
def _cache_keys(cur, _http):
    cur.execute(
        "SELECT cache_key FROM scraped_session_cache "
        "WHERE cache_key LIKE 'view_combined_round_ranking:%' "
        "AND cache_key NOT LIKE 'view_combined_round_ranking:%:%:%'")
    return [f"{k} is keyed by class id alone" for (k,) in cur.fetchall()]


@check("no finished round is still offered as the next one",
       "Gate drop any moment, five hours after the gate dropped")
def _next_race_sane(cur, _http):
    cur.execute(
        """
        SELECT venue, start_time_utc FROM events
        WHERE status <> 'final' AND start_time_utc IS NOT NULL
          AND start_time_utc < now() - interval '6 hours'
        """)
    return [f"{v} started {t:%m-%d %H:%M} and is still not final"
            for v, t in cur.fetchall()]


@check("a championship the series publishes has rows",
       "the SMX tab read No standings yet while the official table existed")
def _standings_present(cur, _http):
    cur.execute(
        """
        SELECT s.abbrev, se.year, count(st.id)
        FROM series s
        JOIN seasons se ON se.series_id = s.id AND se.year = %s
        LEFT JOIN standings st ON st.season_id = se.id
        GROUP BY 1, 2
        """, (datetime.date.today().year,))
    return [f"{a} {y} has no standings at all"
            for a, y, n in cur.fetchall() if n == 0]


# --- what the API must serve ------------------------------------------------
@check("the widget endpoint answers fast enough for a widget",
       "the home screen read Can't reach MXT after the race")
def _widget_fast(_cur, http):
    if not http:
        return []
    t0 = time.time()
    try:
        r = requests.get(API + "/widget/standings", headers=UA, timeout=30)
    except Exception as e:
        return ["request failed: " + type(e).__name__]
    ms = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        return ["HTTP " + str(r.status_code)]
    # A widget that does not get an answer quickly renders its failure state
    # and sits on it until the next timeline reload, which may be an hour.
    return [f"took {ms} ms"] if ms > 5000 else []


@check("the widget payload has the keys the widget decodes",
       "the client draws what the server sends; a rename is a blank widget")
def _widget_shape(_cur, http):
    if not http:
        return []
    try:
        d = requests.get(API + "/widget/standings", headers=UA, timeout=30).json()
    except Exception as e:
        return ["request failed: " + type(e).__name__]
    missing = [k for k in ("live", "series_long", "classes") if k not in d]
    cls = (d.get("classes") or [{}])[0]
    missing += ["classes[0]." + k for k in ("class", "top5") if k not in cls]
    if not d.get("live"):
        missing += [k for k in ("next_gate_utc", "next_venue", "next_start_et")
                    if k not in d]
    return ["missing " + k for k in missing]


@check("the newest final round serves a full results page",
       "the round that decided the title had an empty page")
def _event_page(cur, http):
    cur.execute("SELECT id, venue FROM events WHERE status = 'final' "
                "ORDER BY event_date DESC LIMIT 1")
    row = cur.fetchone()
    if not row or not http:
        return []
    eid, venue = row
    try:
        d = requests.get(f"{API}/events/{eid}", headers=UA, timeout=90).json()
    except Exception as e:
        return ["request failed: " + type(e).__name__]
    ev = d.get("event") or {}
    want = {"sessions": d.get("sessions"), "results": d.get("results"),
            "Overall": ev.get("overall"), "qualifying": ev.get("qualifying")}
    return [f"{venue}: no {k}" for k, v in want.items() if not v]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-only", action="store_true", help="skip HTTP checks")
    args = ap.parse_args()
    http = not args.db_only

    failures = 0
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            for name, motive, fn in _checks:
                try:
                    bad = fn(cur, http)
                except Exception as e:          # a check that breaks is a failure
                    bad = [f"the check itself raised {type(e).__name__}: {e}"]
                if bad:
                    failures += 1
                    print("FAIL  " + name)
                    print("      (exists because: " + motive + ")")
                    for b in bad[:6]:
                        print("        - " + str(b))
                    if len(bad) > 6:
                        print(f"        ... and {len(bad) - 6} more")
                else:
                    print("ok    " + name)
    print(f"\n{len(_checks) - failures}/{len(_checks)} invariants hold.")
    return failures


if __name__ == "__main__":
    sys.exit(main())
