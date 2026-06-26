-- Moto Tracker — database schema (PostgreSQL)
-- Tracks AMA Supercross (SX), Pro Motocross (MX), and SuperMotocross (SMX):
-- schedules, results, riders, standings, and news.
--
-- This file is idempotent: it uses CREATE TABLE IF NOT EXISTS so it can be
-- re-applied safely. scripts/init_db.py runs it against your DATABASE_URL.

-- ---------------------------------------------------------------------------
-- Core reference data
-- ---------------------------------------------------------------------------

-- A racing series, e.g. Supercross (SX), Pro Motocross (MX), SuperMotocross (SMX).
CREATE TABLE IF NOT EXISTS series (
    id             SERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    abbrev         TEXT NOT NULL UNIQUE,        -- 'SX', 'MX', 'SMX'
    governing_body TEXT,                        -- 'AMA'
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One year of a series, e.g. Supercross 2026.
CREATE TABLE IF NOT EXISTS seasons (
    id          SERIAL PRIMARY KEY,
    series_id   INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    year        INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (series_id, year)
);

-- ---------------------------------------------------------------------------
-- Schedule
-- ---------------------------------------------------------------------------

-- A single round/event within a season. Upsert target: (season_id, round_number).
CREATE TABLE IF NOT EXISTS events (
    id             SERIAL PRIMARY KEY,
    season_id      INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    round_number   INTEGER NOT NULL,
    round_label    TEXT,                        -- raw label, e.g. 'Round 28/MX Championship Round'
    venue          TEXT,
    city           TEXT,
    state          TEXT,
    event_date     DATE,
    start_time_utc TIMESTAMPTZ,
    -- Lifecycle: scheduled -> live -> final (the scheduler flips this).
    status         TEXT NOT NULL DEFAULT 'scheduled',
    source_url     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (season_id, round_number)
);

CREATE INDEX IF NOT EXISTS idx_events_status ON events (status);
CREATE INDEX IF NOT EXISTS idx_events_date   ON events (event_date);

-- ---------------------------------------------------------------------------
-- News
-- ---------------------------------------------------------------------------

-- A content source we pull from (RSS feed, scrape target, or JSON API).
CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,                  -- the site homepage
    feed_url    TEXT,                           -- the RSS feed, when type = 'rss'
    type        TEXT NOT NULL DEFAULT 'rss',    -- 'rss' | 'scrape' | 'api'
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A news article. Upsert target: url (unique) so re-runs never duplicate.
CREATE TABLE IF NOT EXISTS news_articles (
    id           SERIAL PRIMARY KEY,
    source_id    INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL UNIQUE,
    summary      TEXT,
    author       TEXT,
    published_at TIMESTAMPTZ,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles (published_at DESC);

-- ---------------------------------------------------------------------------
-- Riders + entity resolution
-- ---------------------------------------------------------------------------

-- A canonical rider. Parsed names from results resolve to one of these.
CREATE TABLE IF NOT EXISTS riders (
    id          SERIAL PRIMARY KEY,
    full_name   TEXT NOT NULL,
    number      TEXT,                           -- race number, kept as text (e.g. '1', '94')
    country     TEXT,
    team        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_riders_name_lower ON riders (lower(full_name));

-- Known alternate spellings/forms that map to a canonical rider.
CREATE TABLE IF NOT EXISTS rider_aliases (
    id          SERIAL PRIMARY KEY,
    rider_id    INTEGER NOT NULL REFERENCES riders(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL UNIQUE
);

-- Low-confidence name matches parked here for a human to confirm or reject.
CREATE TABLE IF NOT EXISTS rider_match_review (
    id              SERIAL PRIMARY KEY,
    parsed_name     TEXT NOT NULL,
    suggested_rider INTEGER REFERENCES riders(id) ON DELETE SET NULL,
    score           REAL,                       -- fuzzy match score 0..100
    context         TEXT,                       -- where it came from (event/session)
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Results
-- ---------------------------------------------------------------------------

-- A timed session within an event, e.g. 450 Main Event, 250 Heat 1, Moto 1.
CREATE TABLE IF NOT EXISTS sessions (
    id          SERIAL PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    class       TEXT,                           -- '450' | '250'
    type        TEXT,                           -- 'qualifying' | 'heat' | 'lcq' | 'main' | 'moto'
    label       TEXT,                           -- human label, e.g. 'Heat 1', 'Moto 2'
    started_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, class, type, label)
);

-- A rider's finishing line in a session. Upsert target: (session_id, rider_id).
CREATE TABLE IF NOT EXISTS results (
    id          SERIAL PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    rider_id    INTEGER REFERENCES riders(id) ON DELETE SET NULL,
    position    INTEGER,
    points      INTEGER,
    laps        INTEGER,
    status      TEXT,                           -- 'finished' | 'dnf' | 'dns' | 'dsq'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, rider_id)
);

-- ---------------------------------------------------------------------------
-- Standings (recomputed from results)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS standings (
    id          SERIAL PRIMARY KEY,
    season_id   INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    class       TEXT NOT NULL,                  -- '450' | '250'
    rider_id    INTEGER NOT NULL REFERENCES riders(id) ON DELETE CASCADE,
    points      INTEGER NOT NULL DEFAULT 0,
    position    INTEGER,
    wins        INTEGER NOT NULL DEFAULT 0,
    podiums     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (season_id, class, rider_id)
);

-- ---------------------------------------------------------------------------
-- Migrations — additive, idempotent. Bring already-created tables up to date.
-- (CREATE TABLE IF NOT EXISTS above won't add columns to an existing table.)
-- ---------------------------------------------------------------------------

ALTER TABLE events ADD COLUMN IF NOT EXISTS round_label TEXT;
