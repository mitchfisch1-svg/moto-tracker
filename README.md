# Moto Tracker

A personal data pipeline for AMA **Supercross (SX)**, **Pro Motocross (MX)**, and
**SuperMotocross (SMX)** — schedules, results, riders, standings, and news.

Stack: **Python 3.11+** and **PostgreSQL** (hosted free on [Neon](https://neon.tech)).

## Project layout

```
.
├── schema.sql            # All database tables
├── requirements.txt
├── .env.example          # Copy to .env and add your DATABASE_URL
├── src/
│   ├── config.py         # Loads .env, exposes DATABASE_URL
│   ├── db.py             # Connection helper + generic upsert()
│   ├── adapters/
│   │   └── base.py       # BaseAdapter: fetch() -> normalize() -> upsert()
│   ├── pipeline/         # Pipeline runners (added later)
│   └── resolve/          # Rider entity resolution (added later)
└── scripts/
    ├── init_db.py        # Applies schema.sql to the database
    └── seed_series.py    # Inserts the 3 series + current-year seasons
```

## One-time setup

1. **Create a Postgres database.** Sign up at [neon.tech](https://neon.tech),
   create a project, and copy the connection string (looks like
   `postgresql://user:pass@host/db?sslmode=require`).
2. **Add your secret.** Copy `.env.example` to `.env` and paste the connection
   string in as `DATABASE_URL`. The `.env` file is git-ignored — never commit it.
3. **Create the virtual environment and install dependencies** (Windows
   PowerShell):
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

## Initialize the database

With `.env` filled in and the venv active:

```powershell
python scripts/init_db.py     # creates the tables
python scripts/seed_series.py # inserts SX / MX / SMX + this year's seasons
```

`init_db.py` prints the tables it created and `seed_series.py` prints the seeded
series, so you can confirm the database is live.

## Run the pipelines

With `.env` filled in and the venv active:

```powershell
python scripts/seed_sources.py        # one-time: register the RSS news sources
python -m src.pipeline.run_schedule   # scrape the SX/MX/SMX schedule into `events`
python -m src.pipeline.run_news       # pull headlines into `news_articles`
python -m src.pipeline.run_results    # parse race results + recompute standings
```

`run_results` ingests all completed rounds by default; pass `--smx-id <id>` for a
single event or `--limit <n>` to cap how many.

## Run the scheduler (automation)

Instead of running each pipeline by hand, the scheduler runs them on a timer:

```powershell
python -m src.scheduler          # runs forever — Ctrl+C to stop
python -m src.scheduler --once   # run each job a single time (handy for testing)
```

Cadence:

| Job | Frequency | What it does |
|---|---|---|
| schedule | weekly (Mon 06:00 UTC) | refresh the calendar |
| news | every 20 minutes | pull the latest headlines |
| results | every 3 minutes | ingest results + standings — **only while an event is live** |

"Live" is derived from each event's `start_time_utc` plus a 6-hour window, so the
results job does nothing the rest of the week. Keep the process running on your PC,
or deploy it to an always-on host (Railway / Render / Fly.io) for 24/7 updates.

Pipelines are idempotent — re-running updates existing rows instead of
duplicating them.

## REST API

A read-only API (FastAPI) serves the data as JSON — this is what a web or iPhone
app calls. Run it locally (venv active, from the project root):

```powershell
uvicorn src.api.main:app --reload
```

Then open **http://127.0.0.1:8000/docs** for interactive, auto-generated docs.

| Endpoint | Returns |
|---|---|
| `GET /series` | the three series + season year |
| `GET /schedule?series=MX&year=2026&status=final` | events (each with `event_id`) |
| `GET /schedule/next?series=MX` | the next upcoming round(s) |
| `GET /standings?series=SX&class=450` | championship standings (250 = `250 East`/`250 West`) |
| `GET /news?limit=20` | latest headlines |
| `GET /riders?search=deegan` · `GET /riders/{id}` | rider lookup + detail |
| `GET /events/{id}` | one event with its sessions and results |
| `GET /health` | liveness + DB check |

## Status

- [x] **Step 1 — Foundation:** schema, config, DB helpers, base adapter, seed scripts.
- [x] **Step 2 — Schedule scraper:** `src/adapters/schedule_smx.py` +
      `src/pipeline/run_schedule.py`. Pulls all SX/MX/SMX rounds from
      supermotocross.com into `events`.
- [x] **Step 3 — News via RSS:** `scripts/seed_sources.py` +
      `src/adapters/news_rss.py` + `src/pipeline/run_news.py`. Pulls headlines
      from Racer X, Vital MX, PulpMX, MX Vice, and Swapmoto Live into
      `news_articles`.
- [x] **Step 4 — Live-timing API investigation:** see
      [`docs/live-timing-api.md`](docs/live-timing-api.md). Found a public Live
      Race Media JSON API (real-time) **and** a durable HTML/PDF results backend;
      documented endpoints, field shapes, and the event-id linkage.
- [x] **Step 5 — Results + standings:** `src/adapters/results_html.py` (race
      parsing), `src/resolve/riders.py` (rider entity resolution),
      `src/standings.py` (points + standings), `src/pipeline/run_results.py`.
      Handles Supercross Triple Crown rounds (combined overall) and flags
      ambiguous rider names to `rider_match_review`.
- [x] **Step 6 — Scheduler:** `src/scheduler.py` (APScheduler). Schedule weekly,
      news every 20 min, results every 3 min for live events only (status derived
      from `start_time_utc` + a 6-hour window).

> **Note on Vital MX:** its RSS feed (`vitalmx.com/rss.xml`) is valid but
> currently publishes no items, so it contributes 0 articles for now.

### Known limitations (results)

- **SX Round 16 (Denver)** has no results link in the schedule source, so it
  isn't ingested. Other rounds backfill fully.
- **`rider_match_review`** holds name pairs the matcher wouldn't merge on its own
  (e.g. the Coenen brothers). Review and mark `resolved = TRUE` to dismiss, or
  merge by adding a row to `rider_aliases`.

All six build steps from `claude-code-kickoff.md` (in your Downloads) are complete.

### Later phases (not built yet)

- **Always-on hosting** for the scheduler (Railway / Render / Fly.io).
- **X / social auto-posting** of news and results.
- **A frontend** (e.g. Next.js reading this same database) for an ESPN-style UI.
