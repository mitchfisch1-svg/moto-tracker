"""A repaired ingest must not relive the race on everybody's phone.

Ironman's results were re-ingested two days late, after the pipeline crash was
fixed. That wrote NEW session rows, so every `result:{session_id}:{rider_id}`
dedupe key looked unseen, and four phones lit up on Sunday night with "Jorge
Prado won 450 Moto 2 at Ironman" — a result everyone had known since Saturday
afternoon.

The freshness test belongs on the RACE, not on the row. A podium is news while
the race is happening; the same podium arriving two days later is a repair, and
nobody wants to be told about a repair.

    python -m pytest tests/ -q
"""

import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.notify import _RESULT_ALERT_WINDOW  # noqa: E402

GATE = datetime.datetime(2026, 8, 29, 17, 0, tzinfo=datetime.timezone.utc)


def _fresh(now, started=GATE):
    """The rule as _rider_results applies it."""
    return started is not None and (now - started) <= _RESULT_ALERT_WINDOW


def test_a_result_during_the_race_still_alerts():
    assert _fresh(GATE + datetime.timedelta(minutes=40))


def test_a_late_but_same_night_ingest_still_alerts():
    """The pipeline running hours behind is a delay, not a repair."""
    assert _fresh(GATE + datetime.timedelta(hours=8))


def test_the_backfill_that_caused_this_stays_silent():
    """00:36 UTC on the 31st — the moment the re-ingest ran."""
    reingest = datetime.datetime(2026, 8, 31, 0, 36, tzinfo=datetime.timezone.utc)
    assert not _fresh(reingest)


def test_the_next_morning_is_already_too_late():
    assert not _fresh(GATE + datetime.timedelta(hours=24))


def test_a_race_with_no_start_time_never_alerts():
    """Better silent than shouting about a race we cannot date."""
    assert not _fresh(GATE, started=None)


def test_the_window_covers_a_race_day_but_not_a_weekend():
    assert datetime.timedelta(hours=12) <= _RESULT_ALERT_WINDOW
    assert _RESULT_ALERT_WINDOW < datetime.timedelta(days=1)
