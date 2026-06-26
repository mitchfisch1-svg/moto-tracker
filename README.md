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

Pipelines are idempotent — re-running updates existing rows instead of
duplicating them.

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
- [ ] Step 6 — Scheduler

> **Note on Vital MX:** its RSS feed (`vitalmx.com/rss.xml`) is valid but
> currently publishes no items, so it contributes 0 articles for now.

### Known limitations (results)

- **250SX East/West are combined.** Supercross runs two regional 250
  championships; we currently total them into one `SX / 250` standings. Splitting
  them needs each round's region (it's on the schedule page) — a future refinement.
- **SX Round 16 (Denver)** has no results link in the schedule source, so it
  isn't ingested. Other rounds backfill fully.
- **`rider_match_review`** holds name pairs the matcher wouldn't merge on its own
  (e.g. the Coenen brothers). Review and mark `resolved = TRUE` to dismiss, or
  merge by adding a row to `rider_aliases`.

See `claude-code-kickoff.md` (in your Downloads) for the full build plan.
