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
from .pipeline.run_results import select_events
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


# --- jobs ------------------------------------------------------------------
def job_schedule():
    log.info("schedule: starting")
    try:
        count = ScheduleSMXAdapter().run()
        log.info("schedule: done (%s events)", count)
    except Exception:
        log.exception("schedule: failed")


def job_news():
    log.info("news: starting")
    try:
        count = NewsRSSAdapter().run()
        log.info("news: done (%s articles)", count)
    except Exception:
        log.exception("news: failed")


def job_results():
    try:
        with get_connection() as conn:
            live = update_event_statuses(conn)
            if not live:
                log.info("results: no live events; skipping")
                return
            events = select_events(conn, target_status="live")
            if not events:
                log.info("results: %s live event(s) but none have a results id", live)
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
    except Exception:
        log.exception("results: failed")


def run_once():
    """Run every job a single time (for testing)."""
    job_schedule()
    job_news()
    job_results()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--once", action="store_true", help="run each job once and exit"
    )
    ap.add_argument(
        "--job", choices=["schedule", "news", "results"],
        help="run a single job once and exit (used by scheduled CI jobs)",
    )
    args = ap.parse_args()

    if args.job:
        {"schedule": job_schedule, "news": job_news, "results": job_results}[
            args.job
        ]()
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

    log.info("Scheduler started. Jobs: schedule (weekly), news (20m), "
             "results (3m, live only). Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
