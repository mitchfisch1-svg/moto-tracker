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
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..config import get_database_url

# A small connection pool so requests reuse connections instead of reconnecting.
_pool: ConnectionPool | None = None


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
            "/recap", "/news", "/riders", "/riders/{id}", "/events/{id}",
            "/health",
        ],
    }


@app.get("/health")
def health():
    try:
        query("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok", "db": True}


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
               e.event_date, e.start_time_utc, e.status, e.broadcast
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
               e.start_time_utc, e.status, e.broadcast
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
    return [_decorate_event(r) for r in query(sql, params)]


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

    race = _lrm_json(lrm_id, "race")
    riders_raw = _lrm_json(lrm_id, "riders") or []
    clock = _lrm_json(lrm_id, "clock")

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
