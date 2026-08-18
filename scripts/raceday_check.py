"""Race-day health check — one command, one plain-English verdict.

    python scripts/raceday_check.py

Reads the public API only: no database, no secrets, runs from anywhere. Safe to
run repeatedly during a race.

It exists because on a two-day round the CORRECT Friday-morning behaviour and a
BROKEN one look identical from the app — both show a countdown and no timing.
The difference is *why*, and that is only visible by comparing what the schedule
says against which round the results site is actually serving. This prints that
comparison instead of making you infer it.

Exit code 0 = nothing wrong, 1 = something needs a human.
"""

import argparse
import datetime
import sys

import requests

API = "https://moto-tracker-api.onrender.com"
TIMEOUT = 30

# Windows hands you a cp1252 stdout the moment output is redirected to a file or
# piped, and this prints RIDER NAMES straight off the feed — a single "Tøndel"
# would otherwise kill the check mid-race with a UnicodeEncodeError. Degrade the
# character rather than the tool.
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


def get(path):
    r = requests.get(f"{API}{path}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def check_health():
    print("\n== server ==")
    h = get("/health")
    db, apns = h.get("db"), h.get("apns")
    say(OK if h.get("status") == "ok" else BAD, f"status: {h.get('status')}")
    say(OK if db else BAD, f"database: {db}")
    # No APNs credentials means no lock-screen card and no push, silently.
    say(OK if apns else BAD, f"APNs (lock screen + push): {apns}")


def check_live():
    """What the app's Race Day tab is showing right now."""
    print("\n== live ==")
    live = get("/live")
    if not live.get("live"):
        nxt = live.get("next_event") or {}
        if live.get("day_complete"):
            say(OK, "racing is DONE for the day (day_complete)")
        else:
            say(INFO, "not live")
        if nxt:
            say(INFO, f"next: {nxt.get('venue')} — {nxt.get('start_time_et')}")
        return live, None

    ev = live.get("event") or {}
    t = live.get("timing") or {}
    clock = t.get("clock") or {}
    riders = t.get("riders") or []
    say(OK, f"LIVE: {ev.get('venue')} (event {ev.get('event_id')})")
    say(INFO, f"on track: {t.get('race_name')!r}  state={t.get('race_state')!r}")
    if clock.get("remaining") is not None:
        say(INFO, f"clock remaining: {clock.get('remaining')}s  flag={clock.get('flag')!r}")
    if riders:
        lead = riders[0]
        say(INFO, f"leader: P{lead.get('position')} {lead.get('name')} "
                  f"#{lead.get('number')} — {lead.get('laps')} laps")
    else:
        say(WARN, "live but the running order is EMPTY — feed may not be flowing yet")
    return live, ev


def check_sessions(live, live_ev):
    """Which round is the results site actually serving?

    This is the check that matters. The results homepage keeps the PREVIOUS
    round up until the next one goes on track, and a round has no results id of
    its own until race morning — so 'the site has sessions' is NOT evidence that
    today's round is under way.
    """
    print("\n== results site ==")
    s = get("/live/sessions")
    name = s.get("event_name")
    sessions = s.get("sessions") or []
    smx = None
    for e in s.get("entry_lists") or []:
        if e.get("event_id"):
            smx = str(e["event_id"])
            break
    say(INFO, f"serving: {name!r}  (smx id {smx})  {len(sessions)} sessions")

    nxt = (live.get("next_event") or {})
    expected_venue = (live_ev or nxt).get("venue")
    if not expected_venue:
        return
    matches = expected_venue.lower() in (name or "").lower()
    if matches:
        say(OK, f"the site is serving THIS round ({expected_venue})")
    elif live.get("live"):
        # Live while the site still shows a different round is the exact
        # false-LIVE failure: a convincing screen full of last week's results.
        say(BAD, f"LIVE says {expected_venue!r} but the site is serving {name!r} "
                 f"— stale program, this is the false-LIVE bug")
    else:
        say(OK, f"not live, and the site is still on {name!r} — expected until "
                f"{expected_venue} goes on track")

    if sessions:
        done = [x for x in sessions if x.get("status") == "complete"]
        say(INFO, f"posted sessions: {len(done)} complete")
        say(INFO, "latest: " + ", ".join(x.get("label", "?") for x in sessions[:3]))


def check_next():
    """What the Next Race widget will be showing."""
    print("\n== next race (widget) ==")
    rows = get("/schedule/next")
    if not rows:
        say(WARN, "no upcoming events")
        return
    n = rows[0]
    say(INFO, f"{n.get('venue')} — {n.get('start_time_et')}  status={n.get('status')}")
    tm = n.get("track_map")
    say(INFO, "track map: " + ("published" if tm and any(tm.values())
                               else "not published yet (normal until race weekend)"))
    w = n.get("weather")
    if w:
        say(INFO, f"weather: {w.get('summary')} {w.get('high_f')}/{w.get('low_f')}°F "
                  f"· {w.get('rain_chance')}% rain")


def main():
    global API
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default=API, help="override the API base url")
    args = ap.parse_args()
    API = args.api.rstrip("/")

    now = datetime.datetime.now(datetime.timezone.utc)
    print(f"MXT race-day check — {now:%Y-%m-%d %H:%M} UTC  ({API})")
    try:
        check_health()
        live, ev = check_live()
        check_sessions(live, ev)
        check_next()
    except requests.RequestException as e:
        say(BAD, f"could not reach the API: {e}")

    # Plain ASCII: this is the line you read at a glance on a phone-tethered
    # laptop at a racetrack, and it must survive any terminal.
    print("\n== verdict ==")
    if _problems:
        for p in _problems:
            print(f"  PROBLEM: {p}")
        return 1
    print("  ALL CLEAR - nothing looks wrong")
    return 0


if __name__ == "__main__":
    sys.exit(main())
