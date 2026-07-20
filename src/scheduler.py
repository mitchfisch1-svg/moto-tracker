"""Scheduler — runs the pipelines automatically.

Jobs:
  - schedule : weekly         (refresh the SX/MX/SMX calendar)
  - news     : every 20 min   (pull the latest headlines)
  - results  : every 3 min    (ingest results — but ONLY while an event is live)

"Live" is derived from each event's start_time_utc and an estimated 6-hour
window, so the results job does nothing the rest of the week.

Run it:
    python -m src.scheduler            # run forever (Ctrl+C to stop)
    python -m src.scheduler --once     # run every job once and exit (for testing)
"""

import argparse
import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .adapters.news_rss import NewsRSSAdapter
from .adapters.results_html import ResultsHTMLAdapter
from .adapters.schedule_smx import ScheduleSMXAdapter
from .db import get_connection
from .notify import notify_work
from .pipeline.run_results import resolve_missing_event_ids, select_events
from .standings import recompute_standings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("moto.scheduler")

# How long an event is considered "live" after its start time.
EVENT_WINDOW_HOURS = 6


def update_event_statuses(conn) -> int:
    """Set each event's status from its start time. Returns the # now live."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE events SET
                status = CASE
                    WHEN start_time_utc IS NULL THEN status
                    WHEN now() < start_time_utc THEN 'scheduled'
                    WHEN now() <= start_time_utc + make_interval(hours => %s)
                        THEN 'live'
                    ELSE 'final'
                END,
                updated_at = now()
            WHERE start_time_utc IS NOT NULL
            """,
            (EVENT_WINDOW_HOURS,),
        )
        cur.execute("SELECT count(*) FROM events WHERE status = 'live'")
        return cur.fetchone()[0]


# --- job work (raises on failure) -------------------------------------------
def schedule_work():
    log.info("schedule: starting")
    count = ScheduleSMXAdapter().run()
    log.info("schedule: done (%s events)", count)


def news_work():
    log.info("news: starting")
    count = NewsRSSAdapter().run()
    log.info("news: done (%s articles)", count)


def _catch_up_missed_rounds(conn):
    """Re-ingest recent rounds that finished but have no results.

    Ingest can fail during a race window for all sorts of reasons (results id
    never published, the site down, a deploy mid-race). Before this, a failure
    there was permanent: results_work only ever looked at LIVE events, so once
    the window closed nobody retried and the round stayed missing until a human
    noticed the standings were wrong. Spring Creek sat broken for two days that
    way, showing the wrong 250 championship leader to live App Store users.

    Cheap when there's nothing to do: one indexed query, and we only reach for
    the network if a round is genuinely missing.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id FROM events e
            WHERE e.status = 'final'
              AND e.event_date >= current_date - 21
              AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.event_id = e.id)
            """
        )
        missed = {row[0] for row in cur.fetchall()}
    if not missed:
        return

    log.warning("results: %s recent final round(s) have NO results — catching up",
                len(missed))
    # A round with no results id can only be recovered while it's still the
    # current event on the results homepage; one that already has an id can be
    # retried at any time.
    resolve_missing_event_ids(conn)
    events = [e for e in select_events(conn, target_status="final")
              if e["event_id"] in missed]
    if not events:
        log.warning("results: catch-up found no usable results id yet; "
                    "will retry next run")
        return

    adapter = ResultsHTMLAdapter()
    season_ids = set()
    for ev in events:
        try:
            adapter.ingest_event(conn, ev)
            season_ids.add(ev["season_id"])
        except Exception:
            log.exception("results: catch-up failed for event %s", ev["event_id"])
    for sid in season_ids:
        recompute_standings(conn, season_id=sid)
    log.info("results: caught up %s missed round(s)", len(events))


def results_work():
    with get_connection() as conn:
        live = update_event_statuses(conn)
        if not live:
            # No race on right now — but a previous round may have silently
            # failed to ingest, so check before going back to sleep.
            _catch_up_missed_rounds(conn)
            return
        events = select_events(conn, target_status="live")
        if not events:
            # Race morning: the event is live but its results link wasn't on
            # the schedule page when we last scraped. Refresh and retry once.
            log.info("results: %s live event(s) without a results id — "
                     "refreshing schedule", live)
            ScheduleSMXAdapter().run()
            events = select_events(conn, target_status="live")
        if not events:
            # The schedule page frequently NEVER publishes the results link, so
            # the refresh above is a dead end (that's how RedBud, Denver,
            # Southwick and Spring Creek all silently failed to ingest).
            # Recover the id straight from the results homepage instead.
            if resolve_missing_event_ids(conn):
                events = select_events(conn, target_status="live")
        if not events:
            log.warning("results: still no results id for the live event(s) — "
                        "this round will NOT ingest")
            return
        log.info("results: ingesting %s live event(s)", len(events))
        adapter = ResultsHTMLAdapter()
        season_ids = set()
        for ev in events:
            adapter.ingest_event(conn, ev)
            season_ids.add(ev["season_id"])
        for sid in season_ids:
            recompute_standings(conn, season_id=sid)
        log.info("results: done")


# --- scheduler wrappers (catch + log so one bad run doesn't kill the loop) ---
def job_schedule():
    try:
        schedule_work()
    except Exception:
        log.exception("schedule: failed")


def job_news():
    try:
        news_work()
    except Exception:
        log.exception("news: failed")


def job_results():
    try:
        results_work()
    except Exception:
        log.exception("results: failed")


def job_notify():
    try:
        notify_work()
    except Exception:
        log.exception("notify: failed")


def run_once():
    """Run every job a single time (for testing)."""
    job_schedule()
    job_news()
    job_results()
    job_notify()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--once", action="store_true", help="run each job once and exit"
    )
    ap.add_argument(
        "--job", choices=["schedule", "news", "results", "notify"],
        help="run a single job once and exit (used by scheduled CI jobs)",
    )
    args = ap.parse_args()

    if args.job:
        # CI mode: let exceptions propagate so a broken run shows up red.
        {"schedule": schedule_work, "news": news_work, "results": results_work,
         "notify": notify_work}[args.job]()
        return

    if args.once:
        run_once()
        return

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(job_schedule, CronTrigger(day_of_week="mon", hour=6),
                      id="schedule", name="weekly schedule refresh")
    scheduler.add_job(job_news, IntervalTrigger(minutes=20),
                      id="news", name="news every 20 min")
    scheduler.add_job(job_results, IntervalTrigger(minutes=3),
                      id="results", name="results every 3 min (live only)")
    scheduler.add_job(job_notify, IntervalTrigger(minutes=5),
                      id="notify", name="push notifications every 5 min")

    log.info("Scheduler started. Jobs: schedule (weekly), news (20m), "
             "results (3m, live only), notify (5m). Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
