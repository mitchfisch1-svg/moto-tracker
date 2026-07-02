"""Moto Tracker REST API (read-only).

Serves the data your pipelines collect as JSON over HTTPS — this is what a web or
iPhone app would call. Interactive docs are auto-generated at /docs.

Run locally (from the project root, venv active):
    uvicorn src.api.main:app --reload
then open http://127.0.0.1:8000/docs
"""

import datetime
import json
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

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
            "/series", "/schedule", "/schedule/next", "/standings",
            "/news", "/riders", "/riders/{id}", "/events/{id}", "/health",
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
        "SELECT id, full_name, number, team, manufacturer, country "
        "FROM riders WHERE id = %s",
        [rider_id],
    )
    if not info:
        raise HTTPException(status_code=404, detail="rider not found")
    standings_rows = query(
        """
        SELECT s.abbrev AS series, st.class, st.position, st.points,
               st.wins, st.podiums
        FROM standings st
        JOIN seasons se ON se.id = st.season_id
        JOIN series  s  ON s.id  = se.series_id
        WHERE st.rider_id = %s
        ORDER BY s.id, st.class
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
    return {"rider": info[0], "standings": standings_rows, "recent_results": recent}


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
