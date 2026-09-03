"""A countdown asks one question: how long until the race.

The stored `start_time_utc` is when COVERAGE begins. For SMX Playoff 1 that is
the 2:30 PM pre-race show, while the gate drops at 3:00 — so the widget's big
number, the app's countdown and the schedule row were all pointing half an hour
early at something that is not the race.

Only the PAYLOAD moves. Every race-window and race-day query reads the
`start_time_utc` column straight from SQL, so none of that behaviour shifts.

    python -m pytest tests/ -q
"""

import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _decorate_event, _gate_drop_utc  # noqa: E402

UTC = datetime.timezone.utc


def _blocks():
    """Parsed blocks, as _gate_drop_utc receives them."""
    return [
        {"label": "Race Day Live", "start_utc": "2026-09-12T13:00:00+00:00"},
        {"label": "Pre-Race Show", "start_utc": "2026-09-12T18:30:00+00:00"},
        {"label": "Gate Drop", "start_utc": "2026-09-12T19:00:00+00:00"},
        {"label": "Post-Race", "start_utc": "2026-09-12T22:00:00+00:00"},
        {"label": "Sunday Encore"},                       # no timestamp, next day
    ]


# What the DATABASE actually stores: a TEXT column of JSON, times as loose
# strings, no timestamps. _stamp_broadcast derives those. Tests that go through
# _decorate_event must use this shape or they are testing a fiction.
RAW_BROADCAST = json.dumps([
    {"label": "Race Day Live", "time_et": "9 am", "providers": ["Peacock"]},
    {"label": "Pre-Race Show", "time_et": "2:30 pm", "providers": ["Peacock"]},
    {"label": "Gate Drop", "time_et": "3 pm", "providers": ["Peacock"]},
    {"label": "Post-Race", "time_et": "6 pm", "providers": ["Peacock"]},
    {"label": "Sunday Encore", "time_et": "4 pm", "providers": ["NBC"]},
])


def _row(**kw):
    row = {"start_time_utc": datetime.datetime(2026, 9, 12, 18, 30, tzinfo=UTC),
           "event_date": datetime.date(2026, 9, 12)}
    row.update(kw)
    return row


def test_the_gate_is_found_among_the_blocks():
    assert _gate_drop_utc(_blocks()) == datetime.datetime(2026, 9, 12, 19, 0, tzinfo=UTC)


def test_no_gate_block_means_keep_the_stored_time():
    """Never invent one. Qualifying-only days and odd formats have no gate."""
    assert _gate_drop_utc([{"label": "Race Day Live",
                            "start_utc": "2026-09-12T13:00:00+00:00"}]) is None
    assert _gate_drop_utc([]) is None
    assert _gate_drop_utc(None) is None


def test_a_block_with_no_timestamp_is_not_a_gate():
    """Sunday Encore has no start_utc — the only-forwards rule refused to guess."""
    assert _gate_drop_utc([{"label": "Gate Drop"}]) is None


def test_the_countdown_points_at_the_gate_not_the_pre_race_show():
    row = _decorate_event(_row(broadcast=RAW_BROADCAST))
    assert row["start_time_utc"] == datetime.datetime(2026, 9, 12, 19, 0, tzinfo=UTC)
    assert "3:00 PM ET" in row["start_time_et"]
    assert "2:30" not in row["start_time_et"]


def test_the_coverage_times_all_survive_for_the_race_day_tab():
    """The distinction is kept where there is room to explain it."""
    row = _decorate_event(_row(broadcast=RAW_BROADCAST))
    labels = [b["label"] for b in row["broadcast"]]
    assert "Pre-Race Show" in labels and "Gate Drop" in labels
    pre = next(b for b in row["broadcast"] if b["label"] == "Pre-Race Show")
    assert pre["start_utc"].startswith("2026-09-12T18:30")


def test_an_event_with_no_broadcast_is_untouched():
    row = _decorate_event(_row(broadcast=None))
    assert row["start_time_utc"] == datetime.datetime(2026, 9, 12, 18, 30, tzinfo=UTC)
    assert "2:30 PM ET" in row["start_time_et"]


def test_an_event_with_no_start_time_still_decorates():
    row = _decorate_event({"start_time_utc": None, "event_date": None, "broadcast": None})
    assert row["start_time_et"] is None
