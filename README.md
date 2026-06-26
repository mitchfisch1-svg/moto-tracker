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
python -m src.pipeline.run_schedule   # scrape the SX/MX/SMX schedule into `events`
```

Pipelines are idempotent — re-running updates existing rows instead of
duplicating them.

## Status

- [x] **Step 1 — Foundation:** schema, config, DB helpers, base adapter, seed scripts.
- [x] **Step 2 — Schedule scraper:** `src/adapters/schedule_smx.py` +
      `src/pipeline/run_schedule.py`. Pulls all SX/MX/SMX rounds from
      supermotocross.com into `events`.
- [ ] Step 3 — News via RSS
- [ ] Step 4 — Investigate the live-timing API
- [ ] Step 5 — Results parsing + rider resolution
- [ ] Step 6 — Scheduler

See `claude-code-kickoff.md` (in your Downloads) for the full build plan.
