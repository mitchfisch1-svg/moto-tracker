"""Moto Tracker REST API (read-only).

Serves the data your pipelines collect as JSON over HTTPS — this is what a web or
iPhone app would call. Interactive docs are auto-generated at /docs.

Run locally (from the project root, venv active):
    uvicorn src.api.main:app --reload
then open http://127.0.0.1:8000/docs
"""

import datetime
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from ..apns import apns_ready, send_live_activity
from ..config import get_database_url
from ..notify import notify_work

# A small connection pool so requests reuse connections instead of reconnecting.
#
# min_size is deliberately 0 and max_idle short: Neon's compute only scales to
# zero once NOTHING has held a connection for its idle timeout, so a pool that
# keeps one connection parked forever bills compute 24/7 and drains the monthly
# CU-hour allowance — at which point Neon suspends the project and every
# endpoint here answers `503 database unavailable`. An idle API must leave the
# database completely alone. See docs/db-budget.md.
_pool: ConnectionPool | None = None
_POOL_MAX_IDLE_S = 60


# --- race-window gate --------------------------------------------------------
# Both background loops below poll Postgres forever. Off race day that polling
# is what pins the compute on, so they consult this gate first and stay off the
# database entirely when there is nothing to push about.
#
# The gate itself is cached, because a bare "is anything live?" query is not
# free either: each one wakes a sleeping compute for a whole idle window. When
# the next race is days out we re-check in hours, not seconds.
_WINDOW_PRE_S = 6 * 3600     # window opens 6h before the gate drops
_WINDOW_POST_S = 9 * 3600    # ... and closes 9h after (matches /live/warm)
_window_lock = threading.Lock()
_window_cache: tuple[float, bool] = (0.0, False)   # (recheck_at, open?)


def _race_window_open() -> bool:
    """True while an event is inside its live window.

    One cached query serves both loops. The cache TTL scales with how far away
    the next event is: seconds-to-minutes near a gate drop, hours when the
    paddock is empty, so a quiet week costs a handful of queries rather than
    thousands.
    """
    global _window_cache
    with _window_lock:
        recheck_at, is_open = _window_cache
        now = time.time()
        if now < recheck_at:
            return is_open

        try:
            rows = query(
                """
                SELECT EXTRACT(EPOCH FROM (start_time_utc - now())) AS delta
                FROM events
                WHERE start_time_utc IS NOT NULL
                  AND start_time_utc > now() - make_interval(secs => %s)
                ORDER BY start_time_utc
                LIMIT 1
                """,
                (_WINDOW_POST_S,),
            )
        except Exception:
            # Don't let a database blip latch the gate open (which would put the
            # loops back to hammering it). Assume quiet and retry shortly.
            _window_cache = (now + 300, False)
            return False

        if not rows:
            # No upcoming event at all — off-season. Check back in six hours.
            _window_cache = (now + 6 * 3600, False)
            return False

        delta = float(rows[0]["delta"])          # seconds until the gate drops
        is_open = -_WINDOW_POST_S <= delta <= _WINDOW_PRE_S
        if is_open:
            ttl = 300                            # live: recheck every 5 min
        else:
            # Wake up a little before the window opens, but never sleep past 6h
            # so a schedule change can't strand us.
            ttl = max(300, min(delta - _WINDOW_PRE_S, 6 * 3600))
        _window_cache = (now + ttl, is_open)
        return is_open


# Push-notification checks run here, in the always-warm API process, every 60s
# on race day — far faster than the 5-min (often delayed) CI cron, which stays
# as a backup. An advisory lock inside notify_work() makes overlapping runners
# harmless. Off race day this backs right off: the only thing waiting is a news
# alert, which is not worth holding the database open around the clock for.
_NOTIFY_INTERVAL_S = 60
# The idle cadence has to be read against the 5-minute scale-to-zero timeout,
# not against "how stale may a news push be": notify_work() touches Postgres on
# every tick, so each tick also buys a full idle window behind it. At 15 min
# that is four wakes an hour — the compute is up ~37% of the time and the month
# lands near 67 CU-hours, most of the allowance, for an empty paddock. Hourly
# matches pulse.yml's own pass (offset from it, so between the two a news alert
# still moves inside ~30 min) and drops that to roughly 16.
_NOTIFY_IDLE_INTERVAL_S = 3600


def _notify_loop():
    while True:
        # News alerts still go out when the paddock is quiet, just on the slow
        # cadence — the longer sleep is what lets the compute idle out between
        # checks instead of being poked every minute all week.
        live_now = _race_window_open()
        try:
            notify_work()
        except Exception:
            pass   # never let a bad cycle kill the loop
        time.sleep(_NOTIFY_INTERVAL_S if live_now else _NOTIFY_IDLE_INTERVAL_S)


# Live Activity loop: while a race window is open, push the running order to
# every registered lock-screen activity every ~10s. That's the practical
# floor: the timing feed itself refreshes ~every 5-10s, and sustained faster
# pushes risk Apple's frequent-update budget deferring deliveries (which
# looks jerkier, not smoother). No-ops without APNs credentials or tokens.
_LA_INTERVAL_S = 10
# Off race day the loop just re-asks the (cached) race-window gate, so this
# interval costs nothing but a wake-up from sleep.
_LA_IDLE_INTERVAL_S = 300


def _la_content_state(payload):
    t = payload.get("timing") or {}
    state = t.get("race_state") or "racing"
    # During qualifying the lock screen should show the same class-wide best-lap
    # board the app and the broadcast show, not one group's running order.
    cq = t.get("combined_qualifying")
    if cq:
        riders = [
            {"p": r.get("position"), "n": (r.get("name") or "").split(" ")[-1],
             "num": str(r.get("number") or ""), "g": (r.get("best_lap") or "")[:12]}
            for r in (cq.get("riders") or [])[:5]
        ]
    else:
        riders = [
            {"p": r.get("position"), "n": (r.get("name") or "").split(" ")[-1],
             "num": str(r.get("number") or ""), "g": (r.get("gap") or "")[:12]}
            for r in (t.get("riders") or [])[:5]
        ]
    clock = t.get("clock") or {}
    remaining = clock.get("remaining")
    # The widget has no state field, so carry the status in the title it already
    # renders — otherwise a staged grid reads as a race in progress and the card
    # sits there looking live after the checkered.
    name = (t.get("race_name") or "On track")
    if state == "staged":
        name = f"{name} · on the gate"
    elif state == "finished":
        name = f"{name} · final"
    return {
        "race": name[:40],
        "venue": ((payload.get("event") or {}).get("venue") or "")[:28],
        "riders": riders,
        "flag": (clock.get("flag") or "")[:12],
        # A staged grid has a full clock that isn't counting down yet — showing it
        # made the lock screen look like a race was already running.
        "remaining": (int(remaining)
                      if state == "racing" and isinstance(remaining, (int, float))
                      else None),
    }


def _live_activity_loop():
    import httpx
    last_state = None
    last_push = 0.0
    while True:
        # There is nothing to put on a lock screen when no race is running, so
        # don't touch the database at all. This loop ticked every 10s around the
        # clock — ~8,600 queries a day — which on its own was enough to keep the
        # compute from ever idling out. See docs/db-budget.md.
        if not _race_window_open():
            time.sleep(_LA_IDLE_INTERVAL_S)
            continue
        try:
            if apns_ready():
                rows = query("SELECT token, kind FROM live_activity_tokens")
                if rows:
                    payload = live()
                    if payload.get("live") and payload.get("timing"):
                        state = _la_content_state(payload)
                        # iOS budgets frequent Live Activity updates and silently
                        # starts dropping them once you blow through it — which is
                        # how the lock screen ended up frozen mid-session. Only
                        # spend budget when something actually changed (with a
                        # heartbeat so a quiet session can't look abandoned).
                        changed = state != last_state
                        if not changed and (time.time() - last_push) < 120:
                            time.sleep(_LA_INTERVAL_S)
                            continue
                        last_state, last_push = state, time.time()
                        stale = []
                        # Once per event: remotely launch the activity on every
                        # phone that registered a push-to-start token (iOS 17.2+)
                        # — lock screens light up without the app being opened.
                        ev_id = (payload.get("event") or {}).get("event_id")
                        start_key = f"lastart:{ev_id}" if ev_id else None
                        started = True
                        if start_key:
                            with _pool.connection() as conn:
                                started = conn.execute(
                                    "SELECT 1 FROM push_sent WHERE key = %s",
                                    (start_key,)).fetchone() is not None
                        with httpx.Client(http2=True, timeout=15) as client:
                            for row in rows:
                                if row["kind"] == "start" and not started:
                                    send_live_activity(
                                        row["token"], "start", state, client=client)
                                if row["kind"] != "update":
                                    continue
                                ok, reason = send_live_activity(
                                    row["token"], "update", state, client=client)
                                if reason in ("BadDeviceToken", "Unregistered",
                                              "ExpiredToken"):
                                    stale.append(row["token"])
                        if start_key and not started:
                            with _pool.connection() as conn:
                                conn.execute(
                                    "INSERT INTO push_sent (key) VALUES (%s) "
                                    "ON CONFLICT (key) DO NOTHING", (start_key,))
                        if stale:
                            with _pool.connection() as conn:
                                conn.execute(
                                    "DELETE FROM live_activity_tokens "
                                    "WHERE token = ANY(%s)", (stale,))
                    elif not payload.get("live"):
                        # Race window closed: end every activity immediately so
                        # nothing lingers on lock screens/Dynamic Island, and
                        # forget the tokens (fresh ones register next race day).
                        upd = [r["token"] for r in rows if r["kind"] == "update"]
                        if upd:
                            with httpx.Client(http2=True, timeout=15) as client:
                                for t in upd:
                                    send_live_activity(
                                        t, "end",
                                        {"race": "Session complete", "venue": None,
                                         "riders": [], "flag": None,
                                         "remaining": None},
                                        client=client)
                            with _pool.connection() as conn:
                                conn.execute(
                                    "DELETE FROM live_activity_tokens "
                                    "WHERE token = ANY(%s)", (upd,))
        except Exception:
            pass   # next tick retries
        time.sleep(_LA_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = ConnectionPool(
        get_database_url(),
        min_size=0,             # park nothing: an idle API must let Neon sleep
        max_size=5,
        max_idle=_POOL_MAX_IDLE_S,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    _pool.open()
    threading.Thread(target=_notify_loop, daemon=True, name="notify-loop").start()
    threading.Thread(target=_live_activity_loop, daemon=True,
                     name="live-activity-loop").start()
    try:
        yield
    finally:
        _pool.close()


app = FastAPI(
    title="Moto Tracker API",
    version="0.1.0",
    description="Read-only API for AMA SX/MX/SMX schedule, standings, results, news.",
    lifespan=lifespan,
)

# Allow any origin — this is a public, read-only API (fine for web/Expo clients).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def query(sql: str, params=()):
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _current_year() -> int:
    return datetime.date.today().year


_EASTERN = ZoneInfo("America/New_York")


def _decorate_event(row: dict) -> dict:
    """Add start_time_et (display string) and parse the broadcast JSON."""
    utc = row.get("start_time_utc")
    if utc:
        et = utc.astimezone(_EASTERN)
        hour = et.hour % 12 or 12
        ampm = "AM" if et.hour < 12 else "PM"
        row["start_time_et"] = (
            f"{et.strftime('%a, %b')} {et.day} · {hour}:{et.minute:02d} {ampm} ET"
        )
    else:
        row["start_time_et"] = None
    if "broadcast" in row:
        try:
            row["broadcast"] = json.loads(row["broadcast"]) if row["broadcast"] else None
        except (TypeError, ValueError):
            row["broadcast"] = None
    return row


# --- meta ------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "name": "Moto Tracker API",
        "docs": "/docs",
        "endpoints": [
            "/series", "/schedule", "/schedule/next", "/standings", "/live",
            "/live/sessions", "/live/sessions/{race_id}", "/recap", "/rundown",
            "/news", "/riders", "/riders/{id}", "/events/{id}", "/health",
        ],
    }


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — MXT</title>
<style>body{{background:#0f1115;color:#f2f4f8;font-family:-apple-system,Segoe UI,
Roboto,sans-serif;max-width:640px;margin:0 auto;padding:32px 20px;line-height:1.6}}
h1{{font-style:italic}}h1 span{{color:#ff5a1f}}a{{color:#ff5a1f}}
p,li{{color:#c7cdd6}}</style></head>
<body><h1>M<span>X</span>T <small style="font-size:.45em;color:#9aa4b2">
MOTO X TRACKER</small></h1>{body}</body></html>"""


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return _PAGE.format(title="Privacy Policy", body="""
<h2>Privacy Policy</h2>
<p><em>Effective July 7, 2026</em></p>
<p>MXT (Moto X Tracker) does not collect, store, or share any personal
information.</p>
<ul>
<li><b>No accounts.</b> The app has no sign-up or login.</li>
<li><b>No tracking.</b> The app contains no analytics, advertising, or
third-party tracking SDKs.</li>
<li><b>On-device preferences.</b> Your favorite riders are stored only on your
device and never leave it.</li>
<li><b>Server logs.</b> When the app fetches schedules, results, and news from
our server, standard technical logs (such as IP address) may be processed
transiently to operate the service; they are not used to identify you.</li>
<li><b>External links.</b> Ticket, news, and video links open third-party
websites governed by their own privacy policies.</li>
</ul>
<p>Questions? Contact <a href="mailto:mitchfisch1@gmail.com">mitchfisch1@gmail.com</a>.</p>""")


@app.get("/support", response_class=HTMLResponse)
def support():
    return _PAGE.format(title="Support", body="""
<h2>Support</h2>
<p>MXT (Moto X Tracker) is an unofficial fan app showing AMA Supercross,
Pro Motocross, and SuperMotocross schedules, live timing, results, standings,
and news. It is not affiliated with or endorsed by AMA, Feld Motor Sports, or
MX Sports.</p>
<p>For help, feedback, or feature requests, email
<a href="mailto:mitchfisch1@gmail.com">mitchfisch1@gmail.com</a>.</p>
<p><a href="/privacy">Privacy policy</a></p>""")


@app.get("/health")
def health():
    try:
        query("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok", "db": True, "apns": apns_ready()}


# --- race-day weather (open-meteo, free, no key) -----------------------------
_WEATHER_CACHE: dict = {}   # cache key -> (expires_at, payload)
_WEATHER_TTL = 1800         # refresh at most every 30 min

_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

_WMO_CODES = [
    ({0}, "Clear", "☀️"), ({1, 2}, "Partly cloudy", "⛅"), ({3}, "Cloudy", "☁️"),
    ({45, 48}, "Fog", "🌫️"), ({51, 53, 55, 56, 57}, "Drizzle", "🌦️"),
    ({61, 63, 65, 66, 67}, "Rain", "🌧️"),
    ({71, 73, 75, 77, 85, 86}, "Snow", "❄️"),
    ({80, 81, 82}, "Showers", "🌦️"), ({95, 96, 99}, "Thunderstorms", "⛈️"),
]


def _wmo_label(code):
    for codes, label, icon in _WMO_CODES:
        if code in codes:
            return label, icon
    return "Mixed", "🌤️"


def _event_weather(city, state, event_date):
    """Race-day forecast at the venue's city, or None (fails soft, cached)."""
    if not city or not event_date:
        return None
    key = f"{city}|{state}|{event_date}"
    hit = _WEATHER_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    out = None
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 10, "country_code": "US"},
            timeout=8,
        ).json().get("results") or []
        want = _STATE_NAMES.get((state or "").upper())
        spot = next((g for g in geo if not want or g.get("admin1") == want),
                    geo[0] if geo else None)
        if spot:
            fc = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": spot["latitude"], "longitude": spot["longitude"],
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                             "precipitation_probability_max",
                    "temperature_unit": "fahrenheit", "timezone": "auto",
                    "start_date": str(event_date), "end_date": str(event_date),
                },
                timeout=8,
            ).json().get("daily") or {}
            if fc.get("time"):
                label, icon = _wmo_label((fc["weather_code"] or [None])[0])
                out = {
                    "summary": label,
                    "icon": icon,
                    "high_f": round(fc["temperature_2m_max"][0]),
                    "low_f": round(fc["temperature_2m_min"][0]),
                    "rain_chance": (fc.get("precipitation_probability_max")
                                    or [None])[0],
                }
    except Exception:
        out = None  # forecast horizon exceeded, network hiccup, etc.

    _WEATHER_CACHE[key] = (time.time() + _WEATHER_TTL, out)
    return out


# --- series + schedule -----------------------------------------------------
@app.get("/series")
def list_series():
    return query(
        """
        SELECT s.abbrev, s.name, s.governing_body, se.year
        FROM series s JOIN seasons se ON se.series_id = s.id
        ORDER BY s.id
        """
    )


@app.get("/schedule")
def schedule(
    series: str | None = None,
    year: int | None = None,
    status: str | None = None,
    limit: int = Query(100, le=500),
):
    year = year or _current_year()
    sql = """
        SELECT e.id AS event_id, s.abbrev AS series, e.round_number,
               e.round_label, e.region_250, e.venue, e.city, e.state,
               e.event_date, e.start_time_utc, e.status, e.broadcast,
               e.tickets_url
        FROM events e
        JOIN seasons se ON se.id = e.season_id
        JOIN series  s  ON s.id  = se.series_id
        WHERE se.year = %s
    """
    params = [year]
    if series:
        sql += " AND s.abbrev = %s"
        params.append(series.upper())
    if status:
        sql += " AND e.status = %s"
        params.append(status)
    sql += " ORDER BY s.id, e.round_number LIMIT %s"
    params.append(limit)
    return [_decorate_event(r) for r in query(sql, params)]


@app.get("/schedule/next")
def next_events(series: str | None = None, limit: int = Query(3, le=20)):
    sql = """
        SELECT e.id AS event_id, s.abbrev AS series, e.round_number,
               e.round_label, e.venue, e.city, e.state, e.event_date,
               e.start_time_utc, e.status, e.broadcast, e.tickets_url,
               e.source_url
        FROM events e
        JOIN seasons se ON se.id = e.season_id
        JOIN series  s  ON s.id  = se.series_id
        WHERE e.event_date >= CURRENT_DATE
    """
    params = []
    if series:
        sql += " AND s.abbrev = %s"
        params.append(series.upper())
    sql += " ORDER BY e.event_date LIMIT %s"
    params.append(limit)
    rows = [_decorate_event(r) for r in query(sql, params)]
    for r in rows:
        # Null until the round is on track — the results site only publishes a
        # layout on race weekend. The app says so rather than hiding the entry.
        r["track_map"] = _event_track_map(r.pop("source_url", None))
    if rows:  # race-day forecast for the very next event only
        rows[0]["weather"] = _event_weather(
            rows[0].get("city"), rows[0].get("state"), rows[0].get("event_date"))
    return rows


# --- standings -------------------------------------------------------------
@app.get("/standings")
def standings(
    series: str,
    klass: str | None = Query(None, alias="class"),
    year: int | None = None,
):
    if (klass or "").upper() == "WMX":
        return _wmx_standings()
    year = year or _current_year()
    sql = """
        SELECT st.class, st.position, r.id AS rider_id, r.full_name, r.number,
               r.team, r.manufacturer,
               COALESCE(r.headshot_override, r.headshot_racerx, r.headshot_url) AS headshot_url,
               st.points, st.wins,
               st.podiums
        FROM standings st
        JOIN seasons se ON se.id = st.season_id
        JOIN series  s  ON s.id  = se.series_id
        JOIN riders  r  ON r.id  = st.rider_id
        WHERE s.abbrev = %s AND se.year = %s
    """
    params = [series.upper(), year]
    if klass:
        sql += " AND st.class = %s"
        params.append(klass)
    sql += " ORDER BY st.class, st.position"
    rows = query(sql, params)
    # Points behind the class leader (0 for the leader).
    leader: dict[str, int] = {}
    for row in rows:
        leader.setdefault(row["class"], row["points"])
        row["gap"] = leader[row["class"]] - row["points"]
    return rows


@app.get("/standings/manufacturers")
def manufacturer_standings(series: str, year: int | None = None):
    """Manufacturers championship, official style: in each points-scoring
    session, a make scores its best finisher's points."""
    year = year or _current_year()
    labels = {"450": "450 — Men", "250": "250 — Men", "WMX": "WMX — Women"}
    out = []
    for cls in ("450", "250", "WMX"):
        # A makes championship is only honest if most of the field's bikes are
        # known. WMX bikes aren't in any results data — they backfill from the
        # live feed during WMX rounds — so the section appears once coverage
        # is real instead of showing standings built from 6 known bikes.
        if cls == "WMX":
            n = query(
                """
                SELECT COUNT(DISTINCT ri.id) AS c
                FROM riders ri
                JOIN results r ON r.rider_id = ri.id
                JOIN sessions s ON s.id = r.session_id
                WHERE s.class = 'WMX' AND ri.manufacturer IS NOT NULL
                """
            )[0]["c"]
            if n < 12:
                continue
        rows = query(
            """
            SELECT make AS manufacturer,
                   SUM(best_pts)::int AS points,
                   COUNT(*) FILTER (WHERE best_pos = 1) AS wins
            FROM (
                SELECT r.session_id, ri.manufacturer AS make,
                       MAX(r.points) AS best_pts, MIN(r.position) AS best_pos
                FROM results r
                JOIN riders   ri ON ri.id = r.rider_id
                JOIN sessions s  ON s.id  = r.session_id
                JOIN events   e  ON e.id  = s.event_id
                JOIN seasons  se ON se.id = e.season_id
                JOIN series   sr ON sr.id = se.series_id
                WHERE sr.abbrev = %s AND se.year = %s AND s.class = %s
                  AND s.type IN ('main', 'moto')
                  AND ri.manufacturer IS NOT NULL
                GROUP BY r.session_id, ri.manufacturer
            ) t
            GROUP BY make
            ORDER BY points DESC
            """,
            [series.upper(), year, cls],
        )
        for i, r in enumerate(rows, start=1):
            r["position"] = i
        out.append({"class": cls, "label": labels.get(cls, cls), "rows": rows})
    return out


# --- news ------------------------------------------------------------------
# Some feeds don't write a summary at all — they emit WordPress's syndication
# footer, "The post <headline> appeared first on <site>." (sometimes behind a
# sponsor tag). That's a third of everything we ingest, and shown as an article
# preview it reads like the app is broken, so strip it and let the caller treat
# the article as preview-less rather than print filler.
_FEED_BOILERPLATE_RE = re.compile(
    r"\s*The post\b.*?\bappeared first on\b.*?(?:\.|$)", re.I | re.S)
_MIN_SUMMARY_CHARS = 40


def _clean_summary(text):
    """A publisher's own blurb, or None when all they sent was boilerplate."""
    if not text:
        return None
    out = _FEED_BOILERPLATE_RE.sub(" ", text)
    out = re.sub(r"\s+", " ", out).strip(" -–—|·")
    return out if len(out) >= _MIN_SUMMARY_CHARS else None


def _news_rows(sql, params):
    rows = query(sql, params)
    for r in rows:
        r["summary"] = _clean_summary(r.get("summary"))
    return rows


@app.get("/news")
def news(limit: int = Query(20, le=100), source: str | None = None):
    sql = """
        SELECT a.id, a.title, a.url, a.summary, a.author, a.published_at,
               src.name AS source
        FROM news_articles a
        LEFT JOIN sources src ON src.id = a.source_id
        WHERE TRUE
    """
    params = []
    if source:
        sql += " AND src.name ILIKE %s"
        params.append(f"%{source}%")
    sql += " ORDER BY a.published_at DESC NULLS LAST LIMIT %s"
    params.append(limit)
    return _news_rows(sql, params)


@app.get("/news/article/{article_id}")
def news_article(article_id: int):
    """One story, so a notification tap can open it in-app without the app
    having to find it in a list it may not have loaded."""
    rows = _news_rows(
        """
        SELECT a.id, a.title, a.url, a.summary, a.author, a.published_at,
               src.name AS source
        FROM news_articles a
        LEFT JOIN sources src ON src.id = a.source_id
        WHERE a.id = %s
        """,
        [article_id],
    )
    if not rows:
        raise HTTPException(status_code=404, detail="article not found")
    return rows[0]


# Words that mark a headline as a big deal (wins, injuries, silly season…).
_BIG_NEWS_RE = re.compile(
    r"\b(wins?|victory|sweeps?|champion(ship)?|clinch\w*|injur\w*|surgery|"
    r"out for|sidelined|signs?|signing|re-signs?|breaking|retire\w*|"
    r"red plate|first career|penal\w*|suspend\w*|fined?)\b", re.I)


@app.get("/news/top")
def news_top(limit: int = Query(3, le=6)):
    """The big stories of the moment, for the app's Top Stories digest.

    Deterministic scoring over the last 48h: keyword hits in the headline,
    the same rider surname appearing across multiple outlets (everyone
    covers real news), and recency. Falls back to the newest items when
    nothing scores highly.
    """
    arts = query(
        """
        SELECT a.title, a.url, a.summary, a.published_at, src.name AS source
        FROM news_articles a
        LEFT JOIN sources src ON src.id = a.source_id
        WHERE COALESCE(a.published_at, a.fetched_at) >= now() - interval '48 hours'
        ORDER BY a.published_at DESC NULLS LAST
        LIMIT 60
        """
    )
    surnames = [r["s"] for r in query(
        """
        SELECT DISTINCT lower(split_part(full_name, ' ',
               array_length(string_to_array(full_name, ' '), 1))) AS s
        FROM riders WHERE length(full_name) > 0
        """
    ) if len(r["s"]) > 3]

    # How often each rider surname appears across DISTINCT outlets right now.
    mentions: dict[str, set] = {}
    for a in arts:
        hay = (a["title"] or "").lower()
        for sn in surnames:
            if sn in hay:
                mentions.setdefault(sn, set()).add(a["source"])

    now = datetime.datetime.now(datetime.timezone.utc)
    scored = []
    for a in arts:
        title = a["title"] or ""
        hay = title.lower()
        score = 3.0 * len(_BIG_NEWS_RE.findall(title))
        for sn, outlets in mentions.items():
            if sn in hay and len(outlets) > 1:
                score += 2.0 * len(outlets)   # cross-outlet story
        if a["published_at"]:
            hours = max(0.0, (now - a["published_at"]).total_seconds() / 3600)
            score += max(0.0, 2.0 - hours / 12)   # freshness nudge
        scored.append((score, a))

    scored.sort(key=lambda x: -x[0])
    out, seen_titles = [], set()
    for score, a in scored:
        key = (a["title"] or "").lower()[:60]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        summary = re.sub(r"<[^>]+>", "", a["summary"] or "")
        summary = re.sub(r"\s+", " ", summary).strip()
        # RSS boilerplate ("The post X appeared first on Y.") isn't a summary.
        if re.match(r"^The post .{0,140}appeared first on", summary):
            summary = ""
        out.append({
            "title": a["title"],
            "summary": summary[:280] + ("…" if len(summary) > 280 else ""),
            "source": a["source"],
            "url": a["url"],
            "published_at": a["published_at"],
            "big": score >= 3.0,
        })
        if len(out) >= limit:
            break
    return out


# --- riders ----------------------------------------------------------------
@app.get("/riders")
def riders(search: str | None = None, limit: int = Query(25, le=100)):
    sql = ("SELECT id, full_name, number, team, manufacturer, country "
           "FROM riders WHERE TRUE")
    params = []
    if search:
        sql += " AND full_name ILIKE %s"
        params.append(f"%{search}%")
    sql += " ORDER BY full_name LIMIT %s"
    params.append(limit)
    return query(sql, params)


@app.get("/riders/{rider_id}")
def rider(rider_id: int):
    info = query(
        "SELECT id, full_name, number, team, manufacturer, hometown, "
        "COALESCE(headshot_override, headshot_racerx, headshot_url) AS headshot_url, country "
        "FROM riders WHERE id = %s",
        [rider_id],
    )
    if not info:
        raise HTTPException(status_code=404, detail="rider not found")
    standings_rows = query(
        """
        SELECT s.abbrev AS series, st.class, st.position, st.points,
               st.wins, st.podiums, lead.max_points - st.points AS gap
        FROM standings st
        JOIN seasons se ON se.id = st.season_id
        JOIN series  s  ON s.id  = se.series_id
        JOIN (
            SELECT season_id, class, MAX(points) AS max_points
            FROM standings GROUP BY season_id, class
        ) lead ON lead.season_id = st.season_id AND lead.class = st.class
        WHERE st.rider_id = %s
        ORDER BY s.id, st.class
        """,
        [rider_id],
    )
    stats = query(
        """
        SELECT count(*)                                        AS races,
               MIN(position)                                   AS best_finish,
               ROUND(AVG(position)::numeric, 1)                AS avg_finish,
               COUNT(*) FILTER (WHERE position = 1)            AS wins,
               COUNT(*) FILTER (WHERE position <= 3)           AS podiums,
               COUNT(*) FILTER (WHERE status IN ('dnf','dns','dsq')) AS dnfs
        FROM results WHERE rider_id = %s
        """,
        [rider_id],
    )
    recent = query(
        """
        SELECT s.abbrev AS series, e.round_number, e.venue, sess.class,
               sess.label, res.position, res.points
        FROM results res
        JOIN sessions sess ON sess.id = res.session_id
        JOIN events   e    ON e.id    = sess.event_id
        JOIN seasons  se   ON se.id   = e.season_id
        JOIN series   s    ON s.id    = se.series_id
        WHERE res.rider_id = %s
        ORDER BY e.event_date DESC NULLS LAST, sess.id
        LIMIT 20
        """,
        [rider_id],
    )
    # WMX points live on the series-points page, not in our standings table, so
    # a WMX rider would otherwise show no championship at all — and the app
    # gates its "Compare head-to-head" button on having one.
    standings_rows = standings_rows + _wmx_standing_lines(rider_id)
    return {
        "rider": info[0],
        "season_stats": stats[0] if stats else None,
        "standings": standings_rows,
        "recent_results": recent,
    }


# --- live timing -------------------------------------------------------------
# See docs/live-timing-api.md: live.supermotocross.com reads public JSON from
# Live Race Media's S3 bucket, keyed by an event id we derive from the event's
# results page and cache in events.lrm_id.
_LRM_S3 = "https://s3.amazonaws.com/assets.liveracemedia.com/event_files"
_LRM_HEADERS = {"User-Agent": "MotoTracker/0.1 (personal project)"}
_SMX_ID_RE = re.compile(r"view_event&(?:amp;)?id=(\d+)")
_LRM_ID_RE = re.compile(r"event_files/(\d+)/")


def _derive_lrm_id(event_id: int, source_url: str | None) -> str | None:
    """Scrape the event's results page for its Live Race Media id and cache it."""
    m = _SMX_ID_RE.search(source_url or "")
    if not m:
        return None
    try:
        resp = requests.get(
            f"https://results.supermotocross.com/results/?p=view_event&id={m.group(1)}",
            headers=_LRM_HEADERS, timeout=15,
        )
        found = _LRM_ID_RE.search(resp.text)
    except requests.RequestException:
        return None
    if not found:
        return None
    lrm_id = found.group(1)
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE events SET lrm_id = %s WHERE id = %s",
                        (lrm_id, event_id))
    return lrm_id


def _lrm_json(lrm_id: str, name: str):
    try:
        resp = requests.get(f"{_LRM_S3}/{lrm_id}/{name}.json",
                            headers=_LRM_HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


# The live feed is the ONLY source that knows every rider's bike (the results
# site's BIKE/BRAND columns are empty everywhere) — so while a race runs,
# permanently capture manufacturers for riders our team-name parsing missed.
# Per-process seen-set keeps this to one DB write per rider per deploy.
_MAKE_BACKFILL_SEEN: set = set()


def _backfill_manufacturers(riders):
    pairs = []
    for r in riders:
        name, make_raw = r.get("name"), r.get("manufacturer")
        if not name or not make_raw or name.lower() in _MAKE_BACKFILL_SEEN:
            continue
        make = _make_from_team(make_raw)   # normalizes GASGAS/GAS GAS etc.
        if make:
            pairs.append((name, make))
    if not pairs:
        return
    try:
        with _pool.connection() as conn:
            with conn.cursor() as cur:
                for name, make in pairs:
                    cur.execute(
                        "UPDATE riders SET manufacturer = %s "
                        "WHERE lower(full_name) = lower(%s) "
                        "  AND manufacturer IS NULL",
                        (make, name),
                    )
                    _MAKE_BACKFILL_SEEN.add(name.lower())
    except Exception:
        pass   # enrichment only — never disturb the live payload


def _rider_status(r: dict) -> str:
    if r.get("IsDisqualified"):
        return "dsq"
    if r.get("IsDidNotStart"):
        return "dns"
    if r.get("IsDidNotFinish") or r.get("IsBroken"):
        return "dnf"
    return "running"


# --- "is the day actually over?" -------------------------------------------
# The race window alone is a bad liveness signal: it runs hours past the last
# checkered flag, which left Race Day stuck on red LIVE and Live Activities
# parked on lock screens showing "Waiting for the gate…". So we also look at
# the feed itself and retire the day once the program's FINAL race is done.
#
# Matching the final race per series matters — a moto finishing mid-program
# must NOT end the day (there's another one coming), only the last one does.
_FINAL_RACE_RE = {
    "MX":  re.compile(r"450.*moto\D*2\b", re.I),          # 450 Moto #2 closes MX
    "SX":  re.compile(r"450.*(?:main|race\D*3\b)", re.I),  # 450 Main (or Race #3, Triple Crown)
    "SMX": re.compile(r"450.*main", re.I),
}
# Keep the final running order up briefly after the checkered so the finish is
# visible, then hand over to the official results.
_DAY_DONE_GRACE_S = 600
_DAY_DONE_AT: dict = {}   # event_id -> monotonic stamp of first "final race done"


def _race_finished(timing) -> bool:
    """True when the on-track race has taken the checkered / gone official."""
    clock = timing.get("clock") or {}
    flag = str(clock.get("flag") or "").lower()
    status = str(timing.get("race_status") or "").lower()
    remaining = clock.get("remaining")
    if "checker" in flag or "finish" in flag:
        return True
    if any(k in status for k in ("complete", "official", "finish")):
        return True
    msgs = " ".join(str(a.get("m") or "") for a in (timing.get("announcements") or []))
    if "checkered" in msgs.lower():
        # Belt-and-braces: the clock also has to have run out, so a checkered
        # from the PREVIOUS race can't retire the day while the next one runs.
        return not remaining or float(remaining) <= 0
    return False


def _is_final_race_of_day(race_name, series) -> bool:
    pat = _FINAL_RACE_RE.get(str(series or "").upper())
    return bool(pat and race_name and pat.search(str(race_name)))


def _race_started(timing) -> bool:
    """Has the session actually gone green? The feed publishes the grid (with a
    full clock and everyone on 0 laps) once a session is staged, well before the
    gate drops — so 'a session exists' must not be read as 'racing'. It's live
    once the clock has ticked, someone has completed a lap, or race control has
    flagged green."""
    riders = timing.get("riders") or []
    max_laps = max((r.get("laps") or 0) for r in riders) if riders else 0
    if max_laps > 0:
        return True
    clock = timing.get("clock") or {}
    try:
        if float(clock.get("elapsed") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    msgs = " ".join(str(a.get("m") or "") for a in
                    (timing.get("announcements") or [])).lower()
    return "green" in msgs or "gate drop" in msgs or "holeshot" in msgs


# --- live combined qualifying -------------------------------------------------
# Qualifying is only about fastest lap, and the TV shows ONE overall board across
# both groups — not each group's in-session order. So during qualifying we merge
# every rider's best lap across all their qualifying sessions (the live group
# from the LRM feed + the already-finished groups from cached results) into a
# single class-wide leaderboard sorted by best lap, mirroring the broadcast.
_QUAL_CLASS_RE = re.compile(r"\b(250|450)\b")


def _lap_to_secs(v):
    """Best-lap value -> seconds. Handles '2:16.623 (5)' (the results page tacks
    on the lap number), '2:12.562', and the LRM feed's bare seconds float."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"(\d+):(\d+(?:\.\d+)?)", s)          # M:SS.mmm ( lap# )
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.match(r"(\d+(?:\.\d+)?)", s)                # bare seconds
    if m:
        x = float(m.group(1))
        return x if x > 0 else None
    return None


def _secs_to_lap(x):
    if x is None:
        return None
    m = int(x // 60)
    s = x - m * 60
    return f"{m}:{s:06.3f}" if m else f"{s:.3f}"


def _cached_session_results(sid, p):
    """Session results from cache only (never scrapes) — keeps /live fast."""
    hit = _sessions_cache_get((p, sid))
    if hit is not None:
        return hit
    return _db_cache_get(f"{p}:{sid}")


def _combined_qualifying(race_name, live_riders):
    """One class-wide qualifying board (best lap across both groups), or None."""
    if not race_name or "qualif" not in race_name.lower():
        return None
    m = _QUAL_CLASS_RE.search(race_name)
    if not m:
        return None
    cls = m.group(1)

    best = {}   # number -> merged best-lap record

    def add(number, name, secs, manu, team, on_track):
        number = (str(number or "")).strip()
        if not number or secs is None:
            return
        cur = best.get(number)
        if cur is None or secs < cur["secs"]:
            best[number] = {"number": number, "name": name, "secs": secs,
                            "manufacturer": manu, "team": team,
                            "on_track": on_track or (cur["on_track"] if cur else False)}
        elif cur:
            cur["on_track"] = cur["on_track"] or on_track

    for r in (live_riders or []):
        add(r.get("number"), r.get("name"), _lap_to_secs(r.get("best_lap")),
            r.get("manufacturer"), r.get("team"), True)

    try:
        sessions = live_sessions().get("sessions", [])
    except Exception:
        sessions = []
    for s in sessions:
        if s.get("kind") != "qualifying" or cls not in (s.get("label") or ""):
            continue
        res = _cached_session_results(s.get("id"), s.get("p") or "view_race_result")
        for row in ((res or {}).get("results") or []):
            add(row.get("number"), row.get("name"), _lap_to_secs(row.get("primary")),
                row.get("manufacturer"), row.get("team"), False)

    if not best:
        return None
    ranked = sorted(best.values(), key=lambda x: x["secs"])
    leader = ranked[0]["secs"]
    return {
        "klass": cls,
        "group_label": race_name,   # e.g. "250 Group A Qualifying 2" (the subline)
        "riders": [
            {"position": i + 1, "number": x["number"], "name": x["name"],
             "best_lap": _secs_to_lap(x["secs"]),
             "gap": (f"+{x['secs'] - leader:.3f}" if i else "Fastest"),
             "manufacturer": x["manufacturer"], "team": x["team"],
             "on_track": x["on_track"]}
            for i, x in enumerate(ranked)
        ],
    }


@app.get("/live")
def live(demo: bool = False):
    """Live-timing snapshot for the event happening now (if any).

    Returns {live: false, next_event} outside the race window; during an event
    (4 hours before the broadcast start — qualifying runs all morning — to
    ~6 hours after) returns the current on-track running order from Live Race
    Media.

    With demo=true and no live event, replays the most recent completed event's
    timing feed so the live screen can be tested/demoed on any day.
    """
    rows = query(
        """
        SELECT e.id AS event_id, s.abbrev AS series, e.round_number,
               e.round_label, e.venue, e.city, e.state, e.event_date,
               e.start_time_utc, e.status, e.broadcast, e.source_url, e.lrm_id
        FROM events e
        JOIN seasons se ON se.id = e.season_id
        JOIN series  s  ON s.id  = se.series_id
        WHERE e.start_time_utc IS NOT NULL
          AND now() >= e.start_time_utc - interval '4 hours'
          AND now() <= e.start_time_utc + interval '6 hours'
        ORDER BY e.start_time_utc
        LIMIT 1
        """
    )
    is_demo = False
    if not rows and demo:
        # Replay the latest completed round (its timing JSON stays up on S3).
        rows = query(
            """
            SELECT e.id AS event_id, s.abbrev AS series, e.round_number,
                   e.round_label, e.venue, e.city, e.state, e.event_date,
                   e.start_time_utc, e.status, e.broadcast, e.source_url, e.lrm_id
            FROM events e
            JOIN seasons se ON se.id = e.season_id
            JOIN series  s  ON s.id  = se.series_id
            WHERE e.status = 'final' AND e.source_url LIKE '%%view_event%%'
            ORDER BY e.start_time_utc DESC
            LIMIT 1
            """
        )
        is_demo = bool(rows)
    if not rows:
        nxt = next_events(limit=1)
        return {"live": False, "event": None,
                "next_event": nxt[0] if nxt else None}

    ev = dict(rows[0])
    lrm_id = ev.pop("lrm_id", None) or _derive_lrm_id(ev["event_id"], ev["source_url"])
    if not lrm_id:
        # The event's own results page may not exist yet (common on race
        # morning). The LRM feed is series-wide — event_files/{id}/ carries
        # whatever race is on track NOW — so fall back to the most recently
        # cached id.
        fb = query(
            "SELECT lrm_id FROM events WHERE lrm_id IS NOT NULL "
            "ORDER BY event_date DESC LIMIT 1"
        )
        lrm_id = fb[0]["lrm_id"] if fb else None
    ev.pop("source_url", None)
    ev = _decorate_event(ev)
    if not lrm_id:
        return {"live": True, "demo": is_demo, "event": ev, "timing": None}

    # Fetch the feed files in parallel — keeps the live view snappy.
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_race = ex.submit(_lrm_json, lrm_id, "race")
        f_riders = ex.submit(_lrm_json, lrm_id, "riders")
        f_clock = ex.submit(_lrm_json, lrm_id, "clock")
        f_ann = ex.submit(_lrm_json, lrm_id, "announcements")
        race = f_race.result()
        riders_raw = f_riders.result() or []
        clock = f_clock.result()
        ann_raw = f_ann.result() or []

    riders = [
        {
            "position": r.get("Position"),
            "name": f"{r.get('FirstName', '')} {r.get('LastName', '')}".strip(),
            "number": r.get("BikeNumber"),
            "laps": r.get("CompletedLaps"),
            "last_lap": r.get("LapTime"),
            "best_lap": r.get("FastestLap"),
            "gap": r.get("DifferenceBehindLeaderDisplay") or "",
            "manufacturer": r.get("Manufacturer"),
            "team": r.get("TeamName"),
            "status": _rider_status(r),
            "position_change": r.get("PositionChangeSinceLastLap"),
            "fastest_overall": bool(r.get("IsFastestLapBestOverall")),
            # Latest-lap sector times: pb = personal best, fast = fastest overall.
            "sectors": [
                {"n": sct.get("SectorNumber"), "t": sct.get("SectorTimeSeconds"),
                 "pb": bool(sct.get("IsFastestSectorNumber")),
                 "fast": bool(sct.get("IsFastestSectorNumberOverall"))}
                for sct in (r.get("LatestSectors") or [])
            ],
        }
        for r in sorted(riders_raw, key=lambda x: x.get("Position") or 999)
    ]

    _backfill_manufacturers(riders)

    # Race control feed, newest first ("Green Flag", "#96 ... holeshot", …).
    announcements = [
        {"t": a.get("DateTimeLocalDisplay"),
         "m": (a.get("Message") or "").split(" at: ")[0]}
        for a in ann_raw[-4:][::-1]
    ]

    timing = {
        "race_name": (race or {}).get("RaceNameOverride")
                     or (race or {}).get("ClassName"),
        "event_name": (race or {}).get("EventName"),
        "race_status": (race or {}).get("RaceStatus"),
        "clock": {
            "elapsed": (clock or {}).get("Elapsed"),
            "remaining": (clock or {}).get("Remaining"),
            "flag": (clock or {}).get("FlagType"),
        },
        "riders": riders,
        "announcements": announcements,
    }

    # Where this session actually is: staged on the gate, running, or finished.
    # Without this the app framed a staged grid (everyone on 0 laps, full clock)
    # as a live race with a "leader", and kept saying LIVE after the checkered.
    if _race_finished(timing):
        timing["race_state"] = "finished"
    elif _race_started(timing):
        timing["race_state"] = "racing"
    else:
        timing["race_state"] = "staged"

    # Qualifying: also expose the class-wide combined board (both groups by best
    # lap), which is what the broadcast shows and what fans actually want.
    race_name = timing["race_name"]
    if race_name and "qualif" in race_name.lower():
        combined = _combined_qualifying(race_name, riders)
        if combined:
            timing["combined_qualifying"] = combined

    # Retire the day once the program's FINAL race is done (plus a short grace
    # so the finish stays visible). This is what drops Race Day out of red LIVE
    # and lets _live_activity_loop tear down lock-screen activities, instead of
    # both lingering for hours on the time window alone. Demo replays are exempt.
    if not is_demo and _race_finished(timing) and _is_final_race_of_day(
            timing.get("race_name"), ev.get("series")):
        first = _DAY_DONE_AT.setdefault(ev["event_id"], time.monotonic())
        if time.monotonic() - first >= _DAY_DONE_GRACE_S:
            nxt = next_events(limit=1)
            return {"live": False, "day_complete": True, "event": ev,
                    "next_event": nxt[0] if nxt else None}
    else:
        _DAY_DONE_AT.pop(ev.get("event_id"), None)

    return {"live": True, "demo": is_demo, "event": ev, "timing": timing}


# --- session results (race-day program browser) -------------------------------
# The results site publishes every session's finishing order as it completes;
# its /results/ homepage always shows the current/most recent event.
_RESULTS_HOME = "https://results.supermotocross.com/results/"
_RESULT_HEADER = ["POS", "#", "BIKE", "RIDER"]
_POS_RE = re.compile(r"^(\d+|DNF|DNS|DSQ|DNQ)$", re.I)
_RACE_LINK_RE = re.compile(r"view_race_result&(?:amp;)?id=(\d+)")
_OVERALL_LINK_RE = re.compile(r"view_multi_main_result&(?:amp;)?id=(\d+)")
_COMBQUAL_LINK_RE = re.compile(
    r"view_combined_round_ranking&(?:amp;)?id=(\d+)&(?:amp;)?rt=(\d+)"
    r"&(?:amp;)?class_id=(\d+)")
_ENTRY_LINK_RE = re.compile(
    r"view_entry_list&(?:amp;)?id=(\d+)&(?:amp;)?class_id=(\d+)")
_TRACK_MAP_RE = re.compile(
    r"https://assets\.liveracemedia\.com/event_files/[^\"']*Map[^\"']*\.(?:jpg|jpeg|png)")
# Results views the session browser may request (guards the upstream URL).
_SESSION_VIEWS = {"view_race_result", "view_multi_main_result",
                  "view_combined_round_ranking"}

# Bike makes recognized inside team names (kept in sync with results_html.py).
_MAKES = ["KTM", "Honda", "Yamaha", "Kawasaki", "Suzuki", "GasGas", "GASGAS", "GAS GAS",
          "Husqvarna", "Ducati", "Triumph", "Beta", "Stark"]
# The results site misspells some team names — "Rockstar Energy Husqvarana" left
# a factory rider (DiFrancesco) with no bike at all. Match the variants we've
# actually seen. Keep in sync with MANUFACTURER_ALIASES in adapters/results_html.
_MAKE_ALIASES = {
    "HUSQVARANA": "Husqvarna", "HUSQVARNA": "Husqvarna", "HUSKY": "Husqvarna",
    "KAWASKI": "Kawasaki", "YAHAMA": "Yamaha",
}

# Both session endpoints scrape the results site on demand; short TTL caches
# make chip-taps in the app instant and shield the site from per-user polling.
_SESSIONS_CACHE: dict = {}   # key -> (expires_at, payload)
_SESSIONS_LIST_TTL = 30      # the day's session list (new ones post ~each half hour)
_SESSION_RESULT_TTL = 21600  # one session's finishing order — final once posted, so
                             # keep it warm all race day (6h) instead of re-scraping


def _sessions_cache_get(key):
    hit = _SESSIONS_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _db_cache_get(key: str):
    """Read a pre-stored session payload from the DB (None if absent/unavailable).

    Completed session results are immutable, so the DB copy is authoritative and
    serving it skips the slow results-site scrape entirely.
    """
    try:
        rows = query(
            "SELECT payload FROM scraped_session_cache WHERE cache_key = %s", (key,)
        )
        return rows[0]["payload"] if rows else None
    except Exception:
        return None   # table not migrated yet / DB hiccup — fall back to scraping


def _db_cache_put(key: str, payload) -> None:
    """Persist a scraped session payload so future reads (even after a restart,
    or from a cold new process) skip the scrape. Best-effort."""
    try:
        with _pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO scraped_session_cache (cache_key, payload, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (cache_key)
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                    """,
                    (key, Json(payload)),
                )
    except Exception:
        pass   # in-memory cache still serves this process


def _session_kind(label: str) -> str:
    """Classify a session by its label so the app can group and explain it."""
    low = (label or "").lower()
    if "overall" in low:
        return "overall"        # motos combined — the result that sets the podium
    if "combined" in low and ("qual" in low or "practice" in low):
        return "combined"       # merged A+B group qualifying times
    if "lcq" in low or "last chance" in low:
        return "lcq"
    if "qual" in low or "practice" in low:
        return "qualifying"
    if "heat" in low:
        return "heat"
    return "race"   # motos, main events, Triple Crown races


def _make_from_team(team):
    if not team:
        return None
    up = team.upper()
    best, best_pos = None, -1
    for make in _MAKES:
        pos = up.rfind(make.upper())
        if pos > best_pos:
            best, best_pos = make, pos
    for typo, real in _MAKE_ALIASES.items():   # tolerate the site's misspellings
        pos = up.rfind(typo)
        if pos > best_pos:
            best, best_pos = real, pos
    return "GasGas" if best in ("GASGAS", "GAS GAS") else best


@app.get("/live/sessions")
def live_sessions():
    """All sessions of the current (or most recent) event, in program order.

    The results site only lists a session once its results are posted, so every
    entry here is a completed session — the app marks these done and infers
    what's still to come from the day's program.
    """
    cached = _sessions_cache_get("list")
    if cached is not None:
        return cached
    try:
        resp = requests.get(_RESULTS_HOME, headers=_LRM_HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        stored = _db_cache_get("list")   # serve the last-known list if the site is down
        if stored is not None:
            return stored
        raise HTTPException(status_code=502, detail="results site unavailable")
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.string if soup.title and soup.title.string else ""
    event_name = title.split("::")[-1].strip() if "::" in title else title.strip()

    seen, sessions = set(), []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "export=pdf" in href:
            continue
        # Each session links to one of three results views. Overall (motos
        # combined) and Combined Qualifying carry extra params we preserve.
        m = _RACE_LINK_RE.search(href)
        if m:
            sess = {"id": m.group(1), "p": "view_race_result"}
        elif (g := _OVERALL_LINK_RE.search(href)):
            sess = {"id": g.group(1), "p": "view_multi_main_result"}
        elif (g := _COMBQUAL_LINK_RE.search(href)):
            # The URL's id is the event; class_id identifies the class.
            sess = {"id": g.group(3), "p": "view_combined_round_ranking",
                    "event_id": g.group(1), "rt": g.group(2)}
        else:
            continue
        key = (sess["p"], sess["id"])
        if key in seen:
            continue
        seen.add(key)
        label = a.get_text(" ", strip=True)
        sess.update(label=label, kind=_session_kind(label), status="complete")
        sessions.append(sess)

    # Track maps (LRM-hosted per-round images) + per-class entry lists.
    maps = _TRACK_MAP_RE.findall(resp.text)
    track_map = {
        "2d": next((m for m in maps if "2D" in m), None),
        "3d": next((m for m in maps if "3D" in m), None),
    }
    entry_seen, entry_lists = set(), []
    for a in soup.find_all("a", href=_ENTRY_LINK_RE):
        href = a.get("href", "")
        if "export=pdf" in href:
            continue
        g = _ENTRY_LINK_RE.search(href)
        if g.group(2) in entry_seen:
            continue
        entry_seen.add(g.group(2))
        entry_lists.append({"event_id": g.group(1), "class_id": g.group(2),
                            "label": a.get_text(" ", strip=True)})

    # Track maps only exist on this page while the round is on track, so keep a
    # copy against the event. Otherwise the map vanishes the moment the race
    # ends and there's no way to look at the layout afterwards.
    if (track_map.get("2d") or track_map.get("3d")) and entry_lists:
        _db_cache_put(f"trackmap:{entry_lists[0]['event_id']}", track_map)

    payload = {"event_name": event_name, "sessions": sessions,
               "track_map": track_map, "entry_lists": entry_lists}
    _SESSIONS_CACHE["list"] = (time.time() + _SESSIONS_LIST_TTL, payload)
    _db_cache_put("list", payload)
    return payload


def _event_track_map(source_url):
    """The stored track map for an event, keyed by its results-site event id."""
    m = re.search(r"view_event&id=(\d+)", source_url or "")
    if not m:
        return None
    stored = _db_cache_get(f"trackmap:{m.group(1)}")
    if stored and (stored.get("2d") or stored.get("3d")):
        return stored
    return None


@app.get("/live/sessions/{race_id}")
def live_session_results(race_id: int, p: str = "view_race_result",
                         event_id: int | None = None, rt: int | None = None):
    """Finishing order for one session, parsed from its results page.

    ``p`` selects the view: ``view_race_result`` (a single moto/qualifying
    session), ``view_multi_main_result`` (the round Overall — motos combined),
    or ``view_combined_round_ranking`` (Combined Qualifying, which also needs
    ``event_id`` + ``rt``, with ``race_id`` carrying the class_id).
    """
    if p not in _SESSION_VIEWS:
        raise HTTPException(status_code=400, detail="unknown results view")
    cache_key = (p, race_id)
    db_key = f"{p}:{race_id}"
    cached = _sessions_cache_get(cache_key)
    if cached is not None:
        return cached
    # Pre-stored in the DB (populated by the warmer / earlier taps): a ~50ms read
    # instead of a 5-15s scrape, and it survives restarts + the site going down.
    stored = _db_cache_get(db_key)
    if stored is not None:
        _SESSIONS_CACHE[cache_key] = (time.time() + _SESSION_RESULT_TTL, stored)
        return stored
    if p == "view_combined_round_ranking":
        if event_id is None or rt is None:
            raise HTTPException(status_code=400,
                                detail="combined ranking needs event_id and rt")
        url = f"{_RESULTS_HOME}?p={p}&id={event_id}&rt={rt}&class_id={race_id}"
    else:
        url = f"{_RESULTS_HOME}?p={p}&id={race_id}"
    try:
        resp = requests.get(url, headers=_LRM_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="results site unavailable")
    soup = BeautifulSoup(resp.text, "html.parser")

    table = header = None
    for tb in soup.find_all("table"):
        first = tb.find("tr")
        if not first:
            continue
        cells = [c.get_text(" ", strip=True).upper() for c in first.find_all(["th", "td"])]
        if cells[:4] == _RESULT_HEADER:
            table, header = tb, cells
            break
    if table is None:
        raise HTTPException(status_code=404, detail="results not posted yet")

    # Column layout varies by view: after POS/#/BIKE/RIDER come 1-3 stat columns
    # (best lap, gap; or moto1/moto2/total), optionally trailed by HOMETOWN/TEAM.
    up = [h.upper() for h in header]
    team_idx = up.index("TEAM") if "TEAM" in up else None
    home_idx = up.index("HOMETOWN") if "HOMETOWN" in up else None
    stat_end = min(i for i in (team_idx, home_idx, len(header)) if i is not None)
    is_overall = "MOTO 1" in up and "MOTO 2" in up

    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True)
                 for c in tr.find_all(["th", "td"], recursive=False)]
        if len(cells) < 5 or not _POS_RE.match(cells[0] or ""):
            continue
        team = ((cells[team_idx].strip() or None)
                if team_idx is not None and team_idx < len(cells) else None)
        name = re.sub(r"\s+HOLESHOT$", "", cells[3] or "", flags=re.I).strip()
        if is_overall:
            # Show the moto scores (e.g. "1-1") plus the round point total.
            m1 = cells[4] if len(cells) > 4 else ""
            m2 = cells[5] if len(cells) > 5 else ""
            total = cells[6] if len(cells) > 6 else ""
            primary_label, primary = "MOTOS", (f"{m1}-{m2}" if m1 or m2 else None)
            secondary_label, secondary = "", (f"{total} pts" if total else None)
        else:
            # Pass the site's own column labels through with the values.
            primary_label = header[4] if len(header) > 4 else ""
            primary = (cells[4].strip() or None) if len(cells) > 4 else None
            has_sec = 5 < stat_end   # col 5 is a stat, not hometown/team
            secondary_label = header[5] if (has_sec and len(header) > 5) else ""
            secondary = ((cells[5].strip() or None)
                         if has_sec and len(cells) > 5 else None)
        rows.append({
            "position": int(cells[0]) if cells[0].isdigit() else None,
            "status": "finished" if cells[0].isdigit() else cells[0].lower(),
            "number": (cells[1] or "").strip() or None,
            "name": name.title(),
            "primary_label": primary_label,
            "primary": primary,
            "secondary_label": secondary_label,
            "secondary": secondary,
            "team": team,
            "manufacturer": _make_from_team(team),
        })
    payload = {"race_id": race_id, "p": p, "results": rows}
    _SESSIONS_CACHE[cache_key] = (time.time() + _SESSION_RESULT_TTL, payload)
    _db_cache_put(db_key, payload)
    return payload


# --- WMX (Women's Motocross) standings ----------------------------------------
# WMX riders never pass through our results pipeline, and the official points
# table includes adjustments (penalties) that recomputing from raw results
# would miss — so WMX standings come straight from the series-points page,
# cached in memory + DB like session results. rider_id is null (no rider pages).
_WMX_SERIES_URL = (_RESULTS_HOME +
                   "?p=view_series_points&id=25")   # 2026 WMX Motocross Championship
_WMX_TTL = 600
_ROUND_COL_RE = re.compile(r"^\d+:")
_FINISH_RE = re.compile(r"^\d+(st|nd|rd|th)$", re.I)


def _wmx_standings():
    cached = _sessions_cache_get("wmx")
    if cached is not None:
        return cached
    try:
        resp = requests.get(_WMX_SERIES_URL, headers=_LRM_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        stored = _db_cache_get("wmx:standings")
        if stored is not None:
            return stored
        raise HTTPException(status_code=502, detail="results site unavailable")
    soup = BeautifulSoup(resp.text, "html.parser")

    table = header = None
    for tb in soup.find_all("table"):
        first = tb.find("tr")
        if not first:
            continue
        cells = [c.get_text(" ", strip=True).upper()
                 for c in first.find_all(["th", "td"])]
        if "RIDER" in cells and "POINTS" in cells:
            table, header = tb, cells
            break
    if table is None:
        raise HTTPException(status_code=404, detail="WMX standings not posted yet")

    pts_i = header.index("POINTS")
    round_idx = [i for i, h in enumerate(header) if _ROUND_COL_RE.match(h)]
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True)
                 for c in tr.find_all(["th", "td"], recursive=False)]
        if len(cells) <= pts_i or not (cells[0] or "").isdigit():
            continue
        # Each round cell reads "<round pts> <overall finish> <moto lines…>";
        # the second token is the round-overall finish ("1st", "2nd", …).
        finishes = []
        for i in round_idx:
            toks = (cells[i] or "").split() if i < len(cells) else []
            if len(toks) >= 2 and _FINISH_RE.match(toks[1]):
                finishes.append(toks[1].lower())
        rows.append({
            "class": "WMX",
            "position": int(cells[0]),
            "rider_id": None,
            "full_name": (cells[3] or "").strip(),
            "number": (cells[1] or "").strip() or None,
            "team": None,
            "manufacturer": None,
            "headshot_url": None,
            "points": int(cells[pts_i]) if cells[pts_i].lstrip("-").isdigit() else 0,
            "wins": sum(1 for f in finishes if f == "1st"),
            "podiums": sum(1 for f in finishes if f in ("1st", "2nd", "3rd")),
        })
    leader = rows[0]["points"] if rows else 0
    for r in rows:
        r["gap"] = leader - r["points"]

    # Enrich with rider identities from our pipeline (WMX motos are ingested),
    # so matched rows get tap-through rider pages, team/manufacturer, and
    # headshots if Feld ever publishes WMX media. Unmatched rows stay display-only.
    names = [r["full_name"].lower() for r in rows if r["full_name"]]
    if names:
        try:
            matches = query(
                """
                SELECT id, full_name, team, manufacturer,
                       COALESCE(headshot_override, headshot_racerx, headshot_url) AS headshot_url
                FROM riders WHERE lower(full_name) = ANY(%s)
                """,
                (names,),
            )
            by_name = {m["full_name"].lower(): m for m in matches}
            for r in rows:
                m = by_name.get((r["full_name"] or "").lower())
                if m:
                    r["rider_id"] = m["id"]
                    r["team"] = m["team"]
                    r["manufacturer"] = m["manufacturer"]
                    r["headshot_url"] = m["headshot_url"]
        except Exception:
            pass   # enrichment is best-effort; official points still serve

    _SESSIONS_CACHE["wmx"] = (time.time() + _WMX_TTL, rows)
    _db_cache_put("wmx:standings", rows)
    return rows


def _wmx_row_for(rider_id: int):
    """This rider's row in the scraped WMX standings, or None.

    Best-effort on purpose: WMX points come off an external page, so a scrape
    failure must degrade to "no WMX championship" rather than break a rider
    page or a head-to-head.
    """
    if not rider_id:
        return None
    try:
        for row in _wmx_standings():
            if row.get("rider_id") == rider_id:
                return row
    except Exception:
        pass
    return None


def _wmx_standing_lines(rider_id: int) -> list[dict]:
    """WMX shaped like a /riders/{id} standings entry (so the app needs no
    special case: class chips, the compare button and the opponent picker all
    read this same shape)."""
    row = _wmx_row_for(rider_id)
    if not row:
        return []
    return [{
        "series": "MX", "class": "WMX", "position": row.get("position"),
        "points": row.get("points"), "wins": row.get("wins"),
        "podiums": row.get("podiums"), "gap": row.get("gap"),
    }]


@app.get("/live/entries/{event_id}/{class_id}")
def live_entries(event_id: int, class_id: int):
    """Entry list for one class of the current event — who's racing today."""
    db_key = f"entries:{event_id}:{class_id}"
    cached = _sessions_cache_get(db_key)
    if cached is not None:
        return cached
    url = f"{_RESULTS_HOME}?p=view_entry_list&id={event_id}&class_id={class_id}"
    try:
        resp = requests.get(url, headers=_LRM_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        stored = _db_cache_get(db_key)
        if stored is not None:
            return stored
        raise HTTPException(status_code=502, detail="results site unavailable")
    soup = BeautifulSoup(resp.text, "html.parser")

    # Layout: a title row, then '# | BRAND | RIDER | HOMETOWN | TEAM' rows.
    rows = []
    for tb in soup.find_all("table"):
        for tr in tb.find_all("tr"):
            cells = [c.get_text(" ", strip=True)
                     for c in tr.find_all(["th", "td"], recursive=False)]
            if len(cells) < 5 or not (cells[0] or "").isdigit():
                continue
            team = (cells[4] or "").strip() or None
            rows.append({
                "number": cells[0],
                "name": (cells[2] or "").title(),
                "hometown": (cells[3] or "").strip() or None,
                "team": team,
                "manufacturer": _make_from_team(team),
            })
        if rows:
            break
    payload = {"event_id": event_id, "class_id": class_id, "entries": rows}
    # Entry lists firm up through the week — cache for an hour.
    _SESSIONS_CACHE[db_key] = (time.time() + 3600, payload)
    _db_cache_put(db_key, payload)
    return payload


@app.get("/live/warm")
def warm_sessions():
    """Pre-fetch every current-event session into cache so the first user tap on
    race day is instant instead of a cold scrape of the results site.

    Self-gating: outside the race window this does nothing but a quick DB check,
    so a frequent cron ping (see .github/workflows/warm.yml) stays cheap. Inside
    the window it warms the session list plus every session's finishing order in
    parallel; already-cached sessions return immediately, so repeated pings only
    pay for new sessions (or a re-warm after a server restart).
    """
    live = query(
        """
        SELECT 1 FROM events
        WHERE start_time_utc IS NOT NULL
          AND now() >= start_time_utc - interval '6 hours'
          AND now() <= start_time_utc + interval '9 hours'
        LIMIT 1
        """
    )
    if not live:
        return {"live": False, "warmed": 0, "total": 0}
    data = live_sessions()
    sess = data.get("sessions", [])

    def _warm(s):
        try:
            kwargs = {"p": s.get("p", "view_race_result")}
            if s.get("event_id"):
                kwargs["event_id"] = int(s["event_id"])
                kwargs["rt"] = int(s["rt"])
            live_session_results(int(s["id"]), **kwargs)
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=6) as ex:
        warmed = sum(ex.map(_warm, sess))
    return {"live": True, "event_name": data.get("event_name"),
            "warmed": warmed, "total": len(sess)}


# --- push notifications ------------------------------------------------------
# The app registers its device token here (no accounts — the token is the id);
# the actual sending lives in src/notify.py, driven by the pipeline.
_DEFAULT_PREFS = {"results": True, "gate": True, "leader": True, "news": True}


class PushRegister(BaseModel):
    token: str
    rider_ids: list[int] = []
    platform: str | None = None
    prefs: dict[str, bool] | None = None   # results | gate | leader | news
    # Per-rider overrides for rider-scoped alerts, keyed by rider id:
    # {"41": {"results": true, "news": false}}. Missing → global prefs apply.
    rider_prefs: dict[str, dict[str, bool]] | None = None


@app.post("/push/register")
def push_register(body: PushRegister):
    """Store/refresh an Expo push token, the riders this device follows, and
    which notification types it wants.

    The token is the identity — re-registering (e.g. after the user stars a new
    rider or flips a toggle in Settings) just updates the row. Idempotent.
    """
    if not body.token.startswith("ExponentPushToken"):
        raise HTTPException(status_code=400, detail="not an Expo push token")
    prefs = {**_DEFAULT_PREFS, **(body.prefs or {})}
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO push_tokens
                    (token, rider_ids, platform, prefs, rider_prefs, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (token) DO UPDATE
                  SET rider_ids   = EXCLUDED.rider_ids,
                      platform    = EXCLUDED.platform,
                      prefs       = EXCLUDED.prefs,
                      rider_prefs = EXCLUDED.rider_prefs,
                      updated_at  = now()
                """,
                (body.token, Json(body.rider_ids), body.platform, Json(prefs),
                 Json(body.rider_prefs or {})),
            )
    return {"ok": True, "following": len(body.rider_ids), "prefs": prefs}


class LiveActivityRegister(BaseModel):
    token: str
    kind: str = "update"   # 'update' (one running activity) | 'start' (iOS 17.2+)


@app.post("/live-activity/register")
def live_activity_register(body: LiveActivityRegister):
    """Store a Live Activity APNs token so the race-day loop can address the
    lock-screen activity. Tokens rotate freely; stale ones self-prune when
    Apple rejects them."""
    if body.kind not in ("update", "start"):
        raise HTTPException(status_code=400, detail="kind must be update|start")
    if not re.fullmatch(r"[0-9a-fA-F]{32,200}", body.token or ""):
        raise HTTPException(status_code=400, detail="not an APNs token")
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO live_activity_tokens (token, kind, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (token) DO UPDATE
                  SET kind = EXCLUDED.kind, updated_at = now()
                """,
                (body.token, body.kind),
            )
    return {"ok": True}


# --- rundown (newcomer "catch me up" on the current field) -------------------
_SERIES_LONG = {"SX": "Supercross", "MX": "Pro Motocross", "SMX": "SuperMotocross"}


def _first_name(full):
    return (full or "").split(" ")[0]


def _last_name(full):
    return (full or "").split(" ")[-1]


def _title_fight_line(leader, chaser, gap, rounds_left):
    if not chaser:
        return f"{leader} leads the championship."
    left = (f" with {rounds_left} round{'s' if rounds_left != 1 else ''} left"
            if rounds_left else "")
    if gap <= 8:
        return (f"{_first_name(leader)} holds a slim {gap}-point lead over "
                f"{_first_name(chaser)}{left} — this one's anyone's.")
    if gap <= 25:
        return (f"{_first_name(leader)} leads {_first_name(chaser)} by {gap} "
                f"points{left}, but it's far from over.")
    return (f"{_first_name(leader)} has built a commanding {gap}-point lead over "
            f"{_first_name(chaser)}{left}.")


@app.get("/widget/standings")
def widget_standings():
    """What the home-screen standings widget should show right now.

    Off race day that's the championship top five. While a session is running
    it's the live order instead — season points frozen at last week's total are
    the wrong thing to stare at mid-moto. Deliberately shaped like /rundown so
    the widget's existing decoder handles both. (iOS decides when to refresh a
    widget, so this lags the app by minutes; the lock-screen Live Activity is
    the real-time surface.)
    """
    try:
        lp = live()
    except Exception:
        lp = None
    lt = (lp or {}).get("timing") if (lp or {}).get("live") else None
    if lt:
        cq = lt.get("combined_qualifying")
        src = (cq.get("riders") if cq else lt.get("riders")) or []
        state = lt.get("race_state") or "racing"
        rows = [
            {"position": r.get("position"), "rider_id": None,
             "full_name": r.get("name"), "points": None,
             "detail": (r.get("best_lap") if cq
                        else ("Leader" if r.get("position") == 1 else r.get("gap")))}
            for r in src[:5]
        ]
        if rows:
            label = lt.get("race_name") or "On track"
            if state == "staged":
                label += " · on the gate"
            elif state == "finished":
                label += " · final"
            # Key is "class" (not "klass") — that's what the widget decodes.
            return {"live": True,
                    "series_long": ((lp.get("event") or {}).get("venue")
                                    or "Race day"),
                    "classes": [{"class": label, "top5": rows}]}
    rd = rundown()
    return {"live": False, "series_long": rd.get("series_long"),
            "classes": rd.get("classes")}


@app.get("/rundown")
def rundown():
    """A newcomer-friendly 'catch me up' on the currently-active series.

    On race day this returns the CURRENT session's running order instead of the
    season table — the home-screen widget reads this endpoint, and season points
    frozen at last week's total are the wrong thing to show mid-moto. (iOS still
    decides when to refresh a widget, so this lags the app; the lock-screen Live
    Activity remains the real-time surface.)
    """
    year = _current_year()

    # Active series = the next upcoming event's, else the latest completed one's.
    nxt = query(
        """
        SELECT s.abbrev, e.venue, e.event_date
        FROM events e JOIN seasons se ON se.id = e.season_id
        JOIN series s ON s.id = se.series_id
        WHERE e.event_date >= CURRENT_DATE ORDER BY e.event_date LIMIT 1
        """
    )
    active = None
    if nxt:
        active = nxt[0]["abbrev"]
        next_event = {"series": nxt[0]["abbrev"], "venue": nxt[0]["venue"],
                      "date": str(nxt[0]["event_date"])}
    else:
        next_event = None
    if not active:
        recent = query(
            """
            SELECT s.abbrev FROM events e JOIN seasons se ON se.id = e.season_id
            JOIN series s ON s.id = se.series_id
            WHERE e.status = 'final' ORDER BY e.event_date DESC LIMIT 1
            """
        )
        active = recent[0]["abbrev"] if recent else "MX"

    # Season progress for the active series.
    prog = query(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE e.status = 'final') AS done
        FROM events e JOIN seasons se ON se.id = e.season_id
        JOIN series s ON s.id = se.series_id
        WHERE s.abbrev = %s AND se.year = %s
        """,
        [active, year],
    )
    total = prog[0]["total"] if prog else 0
    done = prog[0]["done"] if prog else 0
    rounds_left = max(0, total - done)

    # Latest completed round of the active series (for "won last round").
    last = query(
        """
        SELECT e.id, e.venue FROM events e JOIN seasons se ON se.id = e.season_id
        JOIN series s ON s.id = se.series_id
        WHERE s.abbrev = %s AND e.status = 'final'
          AND EXISTS (SELECT 1 FROM sessions x JOIN results r ON r.session_id = x.id
                      WHERE x.event_id = e.id)
        ORDER BY e.event_date DESC LIMIT 1
        """,
        [active],
    )
    last_venue = last[0]["venue"] if last else None
    last_winner = {}  # class -> winner name
    if last:
        wr = query(
            """
            SELECT sess.class, ri.full_name, SUM(r.points) AS pts
            FROM results r JOIN sessions sess ON sess.id = r.session_id
            JOIN riders ri ON ri.id = r.rider_id
            WHERE sess.event_id = %s AND sess.type = ANY(%s)
            GROUP BY sess.class, ri.full_name
            ORDER BY sess.class, pts DESC
            """,
            [last[0]["id"], ["main", "moto"]],
        )
        for row in wr:
            last_winner.setdefault(row["class"], row["full_name"])

    # Standings top-5 per class for the active series.
    rows = query(
        """
        SELECT st.class, st.position, r.id AS rider_id, r.full_name, r.number,
               r.manufacturer,
               COALESCE(r.headshot_override, r.headshot_racerx, r.headshot_url) AS headshot_url,
               r.hometown,
               st.points, st.wins, st.podiums
        FROM standings st JOIN seasons se ON se.id = st.season_id
        JOIN series s ON s.id = se.series_id JOIN riders r ON r.id = st.rider_id
        WHERE s.abbrev = %s AND se.year = %s AND st.position <= 5
        ORDER BY st.class, st.position
        """,
        [active, year],
    )
    by_class = {}
    for r in rows:
        by_class.setdefault(r["class"], []).append(r)

    def class_sort(c):  # 450 first, then 250 variants
        return (0 if c.startswith("450") else 1, c)

    classes = []
    for cls in sorted(by_class, key=class_sort):
        cr = by_class[cls]
        leader, chaser = cr[0], (cr[1] if len(cr) > 1 else None)
        gap = (chaser["points"] and leader["points"] - chaser["points"]) if chaser else 0
        classes.append({
            "class": cls,
            "leader": {k: leader[k] for k in
                       ("rider_id", "full_name", "number", "manufacturer",
                        "headshot_url", "hometown", "points", "wins", "podiums")},
            "chaser": ({"full_name": chaser["full_name"], "gap": gap}
                       if chaser else None),
            "title_fight": _title_fight_line(
                leader["full_name"], chaser["full_name"] if chaser else None,
                gap, rounds_left),
            "won_last_round": last_winner.get(cls),
            "top5": [{"rider_id": x["rider_id"], "position": x["position"],
                      "full_name": x["full_name"], "number": x["number"],
                      "manufacturer": x["manufacturer"], "points": x["points"]}
                     for x in cr],
        })

    # Auto-generated storylines.
    storylines = []
    for c in classes:
        names = [x["full_name"] for x in c["top5"][:3]]
        surs = [_last_name(n) for n in names]
        for sur in set(surs):
            if surs.count(sur) >= 2:
                who = [n for n in names if _last_name(n) == sur]
                storylines.append(
                    f"👨‍👦 The {sur} family is running the {c['class']} class — "
                    f"{' and '.join(_first_name(n) for n in who)} sit inside the top 3.")
                break
    for c in classes:
        if c["chaser"] and c["chaser"]["gap"] <= 8:
            storylines.append(
                f"🔥 The {c['class']} title is on a knife's edge — just "
                f"{c['chaser']['gap']} points separate the top two.")
    if last_venue and last_winner:
        first_cls = classes[0]["class"] if classes else None
        w = last_winner.get(first_cls)
        if w:
            storylines.append(f"🏁 {_first_name(w)} took the win at {last_venue}.")

    # One-line nod to the series that already wrapped (context for newcomers).
    prev_note = None
    if active == "MX":
        champ = query(
            """
            SELECT r.full_name FROM standings st
            JOIN seasons se ON se.id = st.season_id
            JOIN series s ON s.id = se.series_id JOIN riders r ON r.id = st.rider_id
            WHERE s.abbrev = 'SX' AND st.class = '450' AND st.position = 1
              AND se.year = %s LIMIT 1
            """,
            [year],
        )
        if champ:
            prev_note = (f"Supercross wrapped up earlier this year — "
                         f"{champ[0]['full_name']} took the 450 title. Now the "
                         f"series moves outdoors for Pro Motocross.")

    how_it_works = [
        "The year has three championships: Supercross (winter, in stadiums), "
        "Pro Motocross (summer, outdoors), and the SuperMotocross playoffs (fall).",
        "Two classes race at every round: 450 (the premier class, the stars) and "
        "250 (the up-and-comers).",
        "In Motocross each round is two races (motos) — combine both finishes for "
        "the overall. Most points at season's end wins the title.",
    ]

    return {
        "series": active,
        "series_long": _SERIES_LONG.get(active, active),
        "as_of": f"after {last_venue}" if last_venue else "preseason",
        "rounds_done": done, "rounds_total": total, "rounds_left": rounds_left,
        "how_it_works": how_it_works,
        "previous_series_note": prev_note,
        "classes": classes,
        "storylines": storylines,
        "next_event": next_event,
    }


# --- recap -------------------------------------------------------------------
@app.get("/recap")
def recap():
    """Summary of the most recent completed event, for the in-app recap story.

    Event "overall" per class = most points scored across that event's
    sessions (works for SX mains and MX two-moto rounds alike), tie-broken by
    the better finish in the final session.
    """
    ev_rows = query(
        """
        SELECT e.id AS event_id, s.abbrev AS series, e.round_number,
               e.round_label, e.venue, e.city, e.state, e.event_date
        FROM events e
        JOIN seasons se ON se.id = e.season_id
        JOIN series  s  ON s.id  = se.series_id
        WHERE e.status = 'final'
          AND EXISTS (SELECT 1 FROM sessions sess
                      JOIN results r ON r.session_id = sess.id
                      WHERE sess.event_id = e.id)
        ORDER BY e.event_date DESC
        LIMIT 1
        """
    )
    if not ev_rows:
        return {"event": None, "classes": []}
    ev = ev_rows[0]

    rows = query(
        """
        SELECT sess.class, sess.id AS session_id, sess.label,
               r.rider_id, ri.full_name, ri.number, ri.manufacturer,
               COALESCE(ri.headshot_override, ri.headshot_racerx, ri.headshot_url) AS headshot_url,
               r.position, r.points
        FROM sessions sess
        JOIN results r ON r.session_id = sess.id
        JOIN riders  ri ON ri.id = r.rider_id
        WHERE sess.event_id = %s
        """,
        [ev["event_id"]],
    )

    classes = []
    by_class: dict[str, list] = {}
    for r in rows:
        by_class.setdefault(r["class"], []).append(r)

    for cls, cls_rows in sorted(by_class.items(), reverse=True):  # 450 first
        last_session = max(r["session_id"] for r in cls_rows)
        agg: dict[int, dict] = {}
        for r in cls_rows:
            a = agg.setdefault(r["rider_id"], {
                "full_name": r["full_name"], "number": r["number"],
                "manufacturer": r["manufacturer"],
                "headshot_url": r["headshot_url"],
                "event_points": 0, "last_pos": 999, "finishes": [],
            })
            a["event_points"] += r["points"] or 0
            if r["position"]:
                a["finishes"].append(r["position"])
                if r["session_id"] == last_session:
                    a["last_pos"] = r["position"]
        ranked = sorted(
            agg.values(),
            key=lambda a: (-a["event_points"], a["last_pos"]),
        )
        podium = []
        for i, a in enumerate(ranked[:3], start=1):
            podium.append({
                "overall": i,
                "full_name": a["full_name"],
                "number": a["number"],
                "manufacturer": a["manufacturer"],
                "headshot_url": a["headshot_url"],
                "event_points": a["event_points"],
                "finishes": "-".join(str(f) for f in a["finishes"]),
            })
        # Championship top-3 after this round (SX 250 splits into East/West).
        st = query(
            """
            SELECT st.class, st.position, r.full_name, st.points
            FROM standings st
            JOIN seasons se ON se.id = st.season_id
            JOIN series  s  ON s.id  = se.series_id
            JOIN riders  r  ON r.id  = st.rider_id
            WHERE s.abbrev = %s AND st.class LIKE %s AND st.position <= 3
            ORDER BY st.class, st.position
            """,
            [ev["series"], f"{cls}%"],
        )
        classes.append({"class": cls, "podium": podium, "standings_top3": st})

    return {"event": ev, "classes": classes}


# --- events ----------------------------------------------------------------
@app.get("/events/{event_id}")
def event(event_id: int):
    info = query(
        """
        SELECT e.id, s.abbrev AS series, e.round_number, e.round_label,
               e.venue, e.city, e.state, e.event_date, e.start_time_utc,
               e.status, e.source_url
        FROM events e
        JOIN seasons se ON se.id = e.season_id
        JOIN series  s  ON s.id  = se.series_id
        WHERE e.id = %s
        """,
        [event_id],
    )
    if not info:
        raise HTTPException(status_code=404, detail="event not found")
    info[0]["track_map"] = _event_track_map(info[0].pop("source_url", None))
    sessions = query(
        "SELECT id, class, type, label FROM sessions WHERE event_id = %s ORDER BY id",
        [event_id],
    )
    results = query(
        """
        SELECT sess.id AS session_id, sess.class, sess.label, res.position,
               r.id AS rider_id, r.full_name, r.number, res.points, res.status
        FROM sessions sess
        JOIN results res ON res.session_id = sess.id
        JOIN riders  r   ON r.id = res.rider_id
        WHERE sess.event_id = %s
        ORDER BY sess.id, res.position
        """,
        [event_id],
    )
    return {"event": info[0], "sessions": sessions, "results": results}


# --- video feed ---------------------------------------------------------------
# YouTube publishes a public RSS feed per channel (no API key, no quota) listing
# the latest uploads — enough for a browsable feed. The app plays these through
# YouTube's official IFrame player, which is what their terms require: we never
# touch the underlying streams. That also sidesteps the broadcast-footage
# copyright problem entirely — this is the publishers' own content, in their own
# player, with their ads intact.
#
# Two exclusions worth remembering so nobody "helpfully" re-adds them: the
# @vitalmx handle resolves to "Vital MTB" (mountain biking, not moto), and
# Swapmoto Live's channel (UCvOh-WOBvelVw2akcAdjyMQ) serves a valid but EMPTY
# feed — 0 entries. A dead channel degrades gracefully here, it's just a wasted
# request every cycle.
_YT_CHANNELS = [
    ("SuperMotocross", "UCcAjWBbd4aO4AhoovRKNbag"),   # official SX/SMX
    ("Pro Motocross",  "UCKtQ4DDoVusEa1i_Q8OEyew"),   # official MX
    ("Racer X",        "UCzLDrufzDTIQX_F20r0EiMA"),
    ("PulpMX",         "UCpMfM2f4b6ehg1H_olAx02Q"),
    ("Gypsy Tales",    "UCsBGR5UR7UCyLvNbHSxisFQ"),
]
_YT_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
_VIDEOS_TTL = 900            # uploads aren't frequent; 15 min is plenty
_VIDEOS_CACHE: dict = {}     # 'all' -> (expires_at, items)

_YT_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
_YT_VID_RE = re.compile(r"<yt:videoId>(.*?)</yt:videoId>")
_YT_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_YT_PUB_RE = re.compile(r"<published>(.*?)</published>")


def _yt_channel_videos(name: str, channel_id: str):
    """Latest uploads for one channel. Never raises — one dead channel must not
    empty the whole feed."""
    from html import unescape
    out = []
    try:
        resp = requests.get(_YT_RSS.format(channel_id), timeout=15)
        for block in _YT_ENTRY_RE.findall(resp.text):
            vid = _YT_VID_RE.search(block)
            title = _YT_TITLE_RE.search(block)
            if not vid or not title:
                continue
            pub = _YT_PUB_RE.search(block)
            vid_id = vid.group(1)
            out.append({
                "video_id": vid_id,
                "title": unescape(title.group(1)).strip(),
                "channel": name,
                "published_at": pub.group(1) if pub else None,
                "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={vid_id}",
            })
    except Exception:
        pass
    return out


@app.get("/videos")
def videos(limit: int = 40):
    """Latest moto videos across the major channels, newest first."""
    hit = _VIDEOS_CACHE.get("all")
    if hit and hit[0] > time.time():
        return hit[1][:limit]

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(_yt_channel_videos, n, c) for n, c in _YT_CHANNELS]
        items = [v for f in futs for v in f.result()]

    if items:
        items.sort(key=lambda x: x.get("published_at") or "", reverse=True)
        _VIDEOS_CACHE["all"] = (time.time() + _VIDEOS_TTL, items)
        _db_cache_put("videos:latest", items)
        return items[:limit]

    # Every channel failed (network blip) — serve the last good copy.
    return (_db_cache_get("videos:latest") or [])[:limit]


# --- podcasts -----------------------------------------------------------------
# Standard podcast RSS: each <item> carries an <enclosure> pointing at the audio
# file, which is exactly what a player needs. Feed URLs came from the iTunes
# lookup API (entity=podcast exposes feedUrl), and every one was verified to have
# a 1:1 item->enclosure ratio before being listed here.
_PODCAST_FEEDS = [
    ("The PulpMX Show",  "https://www.pulpmx.com/apptabs/z_pmxs.xml"),
    ("Steve Matthes Show", "https://www.pulpmx.com/apptabs/z_tsms.xml"),
    ("Racer X Podcast",  "https://rss.libsyn.com/shows/117643/destinations/676139.xml"),
    ("Gypsy Tales",      "https://rss.art19.com/gypsy-tales"),
    ("Swapmoto Live",    "https://www.podserve.fm/series/rss/20/swapmoto-live-podcast.rss"),
    ("Whiskey Throttle", "https://anchor.fm/s/dbada008/podcast/rss"),
]
# Back catalogues are enormous (Steve Matthes alone has 2,700+ episodes), so we
# only keep the newest few per show and cache hard — new episodes are weekly.
_POD_PER_SHOW = 8
_PODCASTS_TTL = 1800
_PODCASTS_CACHE: dict = {}

_POD_ITEM_RE = re.compile(r"<item[^>]*>(.*?)</item>", re.S)
_POD_TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_POD_ENC_RE = re.compile(r"<enclosure[^>]+url=\"([^\"]+)\"", re.I)
_POD_DATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_POD_DUR_RE = re.compile(r"<itunes:duration>(.*?)</itunes:duration>", re.I | re.S)
_POD_IMG_RE = re.compile(r"<itunes:image[^>]+href=\"([^\"]+)\"", re.I)


def _https(url):
    """iOS App Transport Security silently refuses plain http:// media, so an
    http artwork URL just renders blank (that's why the Steve Matthes Show had
    no cover art). These hosts all serve the same asset over TLS."""
    if isinstance(url, str) and url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _pod_duration(raw):
    """<itunes:duration> is either H:MM:SS or a bare seconds count depending on
    the host (Swapmoto sends "3800", the rest send "1:58:13"). Normalise so the
    app never has to guess."""
    if not raw:
        return None
    raw = raw.strip()
    if ":" in raw:
        return raw
    try:
        secs = int(float(raw))
    except ValueError:
        return raw
    hrs, rem = divmod(secs, 3600)
    mins, sec = divmod(rem, 60)
    return f"{hrs}:{mins:02d}:{sec:02d}" if hrs else f"{mins}:{sec:02d}"


def _podcast_episodes(show: str, feed_url: str):
    """Newest episodes for one show. Never raises."""
    from email.utils import parsedate_to_datetime
    from html import unescape
    out = []
    try:
        body = requests.get(feed_url, timeout=25).text
        art = _POD_IMG_RE.search(body)          # channel art precedes the items
        artwork = art.group(1) if art else None
        for block in _POD_ITEM_RE.findall(body)[:_POD_PER_SHOW]:
            enc = _POD_ENC_RE.search(block)
            title = _POD_TITLE_RE.search(block)
            if not enc or not title:
                continue
            published = None
            d = _POD_DATE_RE.search(block)
            if d:
                try:
                    dt = parsedate_to_datetime(d.group(1).strip())
                    if dt.tzinfo:               # normalise so ISO strings sort
                        dt = dt.astimezone(datetime.timezone.utc)
                    published = dt.isoformat()
                except Exception:
                    published = None
            dur = _POD_DUR_RE.search(block)
            out.append({
                "show": show,
                "title": unescape(title.group(1)).strip(),
                "audio_url": _https(enc.group(1)),
                "published_at": published,
                "duration": _pod_duration(dur.group(1)) if dur else None,
                "artwork": _https(artwork),
            })
    except Exception:
        pass
    return out


@app.get("/podcasts")
def podcasts(limit: int = 40):
    """Newest episodes across the major moto podcasts, newest first."""
    hit = _PODCASTS_CACHE.get("all")
    if hit and hit[0] > time.time():
        return hit[1][:limit]

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(_podcast_episodes, n, u) for n, u in _PODCAST_FEEDS]
        items = [e for f in futs for e in f.result()]

    if items:
        items.sort(key=lambda x: x.get("published_at") or "", reverse=True)
        _PODCASTS_CACHE["all"] = (time.time() + _PODCASTS_TTL, items)
        _db_cache_put("podcasts:latest", items)
        return items[:limit]

    return (_db_cache_get("podcasts:latest") or [])[:limit]


# --- head-to-head rider comparison --------------------------------------------
# A season's story is usually a rivalry — the Lawrence brothers are 12 points
# apart with rounds to go. Every moto finish is already stored, so this needs no
# new data collection, just the right query.
@app.get("/compare")
def compare(
    a: int,
    b: int,
    series: str = "MX",
    klass: str | None = Query("450", alias="class"),
    year: int | None = None,
):
    """Season-long head-to-head between two riders in one class."""
    year = year or _current_year()
    ids = [a, b]

    riders = {
        r["rider_id"]: dict(r, points=None, position=None, wins=None, podiums=None)
        for r in query(
            """
            SELECT r.id AS rider_id, r.full_name, r.number, r.team,
                   r.manufacturer,
                   COALESCE(r.headshot_override, r.headshot_racerx, r.headshot_url) AS headshot_url
            FROM riders r WHERE r.id = ANY(%s)
            """,
            (ids,),
        )
    }
    if len(riders) < 2:
        raise HTTPException(status_code=404, detail="rider not found")

    for st in query(
        """
        SELECT st.rider_id, st.position, st.points, st.wins, st.podiums
        FROM standings st
        JOIN seasons se ON se.id = st.season_id
        JOIN series  s  ON s.id  = se.series_id
        WHERE s.abbrev = %s AND se.year = %s AND st.class = %s
          AND st.rider_id = ANY(%s)
        """,
        (series.upper(), year, klass, ids),
    ):
        riders[st["rider_id"]].update(
            position=st["position"], points=st["points"],
            wins=st["wins"], podiums=st["podiums"])

    # WMX isn't in the standings table (scraped series-points page instead), so
    # the moto-by-moto half below already worked while both riders' championship
    # lines came back null. Fill them from the same source /standings uses.
    if (klass or "").upper() == "WMX":
        for rid in ids:
            row = _wmx_row_for(rid)
            if row:
                riders[rid].update(
                    position=row.get("position"), points=row.get("points"),
                    wins=row.get("wins"), podiums=row.get("podiums"))

    rows = query(
        """
        SELECT e.round_number, e.venue, e.event_date, ses.id AS session_id,
               ses.label, res.rider_id, res.position, res.points
        FROM results res
        JOIN sessions ses ON ses.id = res.session_id
        JOIN events   e   ON e.id   = ses.event_id
        JOIN seasons  se  ON se.id  = e.season_id
        JOIN series   s   ON s.id   = se.series_id
        WHERE s.abbrev = %s AND se.year = %s AND ses.class = %s
          AND ses.type IN ('main', 'moto') AND res.rider_id = ANY(%s)
        ORDER BY e.round_number, ses.id
        """,
        (series.upper(), year, klass, ids),
    )

    rounds: dict = {}
    for r in rows:
        rnd = rounds.setdefault(r["round_number"], {
            "round": r["round_number"], "venue": r["venue"],
            "date": str(r["event_date"]), "sessions": {},
            "a_points": 0, "b_points": 0,
        })
        sess = rnd["sessions"].setdefault(
            r["session_id"], {"label": r["label"], "a": None, "b": None})
        side = "a" if r["rider_id"] == a else "b"
        sess[side] = r["position"]
        rnd[f"{side}_points"] += r["points"] or 0

    # The headline number: how many motos each rider finished ahead of the
    # other. Only motos where BOTH actually have a result count, so a DNS by
    # one rider isn't scored as a "win" for the other.
    wins_a = wins_b = 0
    out_rounds = []
    for rnd in rounds.values():
        sessions = list(rnd["sessions"].values())
        for sess in sessions:
            if sess["a"] and sess["b"]:
                if sess["a"] < sess["b"]:
                    wins_a += 1
                elif sess["b"] < sess["a"]:
                    wins_b += 1
        rnd["sessions"] = sessions
        rnd["winner"] = ("a" if rnd["a_points"] > rnd["b_points"]
                         else "b" if rnd["b_points"] > rnd["a_points"] else None)
        out_rounds.append(rnd)
    out_rounds.sort(key=lambda x: x["round"], reverse=True)

    return {
        "series": series.upper(), "class": klass, "year": year,
        "a": riders.get(a), "b": riders.get(b),
        "head_to_head": {"a": wins_a, "b": wins_b, "motos": wins_a + wins_b},
        "rounds": out_rounds,
    }
