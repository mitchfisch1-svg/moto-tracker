"""SQLite storage. Everything lives in one file: data/scout.db."""
import datetime as dt
import sqlite3

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY, ran_at TEXT, universe_n INTEGER, shortlist_n INTEGER);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY, scan_id INTEGER, ticker TEXT, name TEXT, sector TEXT,
  price REAL, mcap REAL, features TEXT, score REAL, rank INTEGER);
CREATE TABLE IF NOT EXISTS picks (
  id INTEGER PRIMARY KEY, ticker TEXT, picked_at TEXT, entry_price REAL,
  horizon_days INTEGER, resolve_after TEXT, features TEXT, prob REAL,
  status TEXT DEFAULT 'open', exit_price REAL, ret_pct REAL, label INTEGER,
  resolved_at TEXT);
CREATE TABLE IF NOT EXISTS weights (
  id INTEGER PRIMARY KEY, updated_at TEXT, weights TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS news (
  id INTEGER PRIMARY KEY, fetched_at TEXT, published TEXT, source TEXT,
  ticker TEXT, headline TEXT, url TEXT UNIQUE, score REAL, hits TEXT);
CREATE TABLE IF NOT EXISTS universe (
  ticker TEXT PRIMARY KEY, name TEXT, sector TEXT, price REAL, mcap REAL,
  updated_at TEXT);
"""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect():
    config.DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(config.DATA_DIR / "scout.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def catalyst_for(conn, ticker):
    """Net news score for a ticker over the lookback window, clipped to [-1, 1].

    A hot FDA headline pushes it toward +1; an offering / going-concern
    headline drags it negative. Feeds the 'catalyst' feature in the model.
    """
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(hours=config.NEWS_LOOKBACK_H)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT score FROM news WHERE ticker=? AND fetched_at>=?",
        (ticker, cutoff)).fetchall()
    return max(-1.0, min(1.0, sum(r["score"] for r in rows)))


def pick_stats(conn):
    r = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(label),0) wins, COALESCE(AVG(ret_pct),0) avg_ret "
        "FROM picks WHERE status='resolved'").fetchone()
    o = conn.execute("SELECT COUNT(*) n FROM picks WHERE status='open'").fetchone()
    n = r["n"]
    return {"resolved": n, "wins": r["wins"],
            "win_rate": (r["wins"] / n * 100 if n else 0.0),
            "avg_ret": r["avg_ret"], "open": o["n"]}
