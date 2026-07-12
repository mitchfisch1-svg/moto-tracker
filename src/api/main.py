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

from ..config import get_database_url
from ..notify import notify_work

# A small connection pool so requests reuse connections instead of reconnecting.
_pool: ConnectionPool | None = None

# Push-notification checks run here, in the always-warm API process, every 60s
# — far faster than the 5-min (often delayed) CI cron, which stays as a backup.
# An advisory lock inside notify_work() makes overlapping runners harmless.
_NOTIFY_INTERVAL_S = 60


def _notify_loop():
    while True:
        try:
            notify_work()
        except Exception:
            pass   # never let a bad cycle kill the loop
        time.sleep(_NOTIFY_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = ConnectionPool(
        get_database_url(),
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    _pool.open()
    threading.Thread(target=_notify_loop, daemon=True, name="notify-loop").start()
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
    return {"status": "ok", "db": True}


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
               e.start_time_utc, e.status, e.broadcast, e.tickets_url
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
    year = year or _current_year()
    sql = """
        SELECT st.class, st.position, r.id AS rider_id, r.full_name, r.number,
               r.team, r.manufacturer, st.points, st.wins, st.podiums
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
    out = []
    for cls in ("450", "250"):
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
        out.append({"class": cls, "rows": rows})
    return out


# --- news ------------------------------------------------------------------
@app.get("/news")
def news(limit: int = Query(20, le=100), source: str | None = None):
    sql = """
        SELECT a.title, a.url, a.summary, a.author, a.published_at,
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
    return query(sql, params)


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
        "headshot_url, country FROM riders WHERE id = %s",
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


def _rider_status(r: dict) -> str:
    if r.get("IsDisqualified"):
        return "dsq"
    if r.get("IsDidNotStart"):
        return "dns"
    if r.get("IsDidNotFinish") or r.get("IsBroken"):
        return "dnf"
    return "running"


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

    # Fetch the three feed files in parallel — keeps the live view snappy.
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_race = ex.submit(_lrm_json, lrm_id, "race")
        f_riders = ex.submit(_lrm_json, lrm_id, "riders")
        f_clock = ex.submit(_lrm_json, lrm_id, "clock")
        race = f_race.result()
        riders_raw = f_riders.result() or []
        clock = f_clock.result()

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
        }
        for r in sorted(riders_raw, key=lambda x: x.get("Position") or 999)
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
    }
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
# Results views the session browser may request (guards the upstream URL).
_SESSION_VIEWS = {"view_race_result", "view_multi_main_result",
                  "view_combined_round_ranking"}

# Bike makes recognized inside team names (kept in sync with results_html.py).
_MAKES = ["KTM", "Honda", "Yamaha", "Kawasaki", "Suzuki", "GasGas", "GASGAS",
          "Husqvarna", "Ducati", "Triumph", "Beta", "Stark"]

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
    return "GasGas" if best == "GASGAS" else best


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
    payload = {"event_name": event_name, "sessions": sessions}
    _SESSIONS_CACHE["list"] = (time.time() + _SESSIONS_LIST_TTL, payload)
    _db_cache_put("list", payload)
    return payload


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
                INSERT INTO push_tokens (token, rider_ids, platform, prefs, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (token) DO UPDATE
                  SET rider_ids  = EXCLUDED.rider_ids,
                      platform   = EXCLUDED.platform,
                      prefs      = EXCLUDED.prefs,
                      updated_at = now()
                """,
                (body.token, Json(body.rider_ids), body.platform, Json(prefs)),
            )
    return {"ok": True, "following": len(body.rider_ids), "prefs": prefs}


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


@app.get("/rundown")
def rundown():
    """A newcomer-friendly 'catch me up' on the currently-active series."""
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
               r.manufacturer, r.headshot_url, r.hometown,
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
               ri.headshot_url, r.position, r.points
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
               e.venue, e.city, e.state, e.event_date, e.start_time_utc, e.status
        FROM events e
        JOIN seasons se ON se.id = e.season_id
        JOIN series  s  ON s.id  = se.series_id
        WHERE e.id = %s
        """,
        [event_id],
    )
    if not info:
        raise HTTPException(status_code=404, detail="event not found")
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
