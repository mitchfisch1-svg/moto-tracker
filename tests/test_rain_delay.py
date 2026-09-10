"""A paused race is not a phantom grid — the difference decides the whole day.

    python -m pytest tests/ -q

A red flag or a weather hold looks EXACTLY like a grid published early: dead
clock, frozen order, nobody on track. The stall rule could not tell them apart,
so a 30-minute hold past the scheduled start took the `stale_grid` branch,
called `_retire_round()` and returned `day_complete: True` — ending the day and
putting final results on the lock screen with the mains still to come. Rain
delays routinely run 30-90 minutes.

What separates them is WHEN. The bug the stall rule exists for was a grid
sitting at 11:51 PM for an 8 AM session, a day out of place. A pause happens
during the racing hours.

No database and no network — every case is a hand-built event.
"""

import datetime
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import (  # noqa: E402
    _DELAY_WINDOW_POST_S,
    _DELAY_WINDOW_PRE_S,
    _la_content_state,
    _racing_is_merely_paused,
)

GATE = datetime.datetime(2026, 9, 12, 18, 30, tzinfo=datetime.timezone.utc)


def ev(start=GATE):
    return {"event_id": 29, "venue": "Historic Crew Stadium",
            "start_time_utc": start}


def at(hours):
    return GATE + datetime.timedelta(hours=hours)


# --- when a dead grid means "waiting" rather than "not real" ------------------

@pytest.mark.parametrize("hours,paused", [
    (0, True),         # on the gate, held
    (-0.5, True),      # half an hour early — sighting laps due
    (-1.1, False),     # well before racing; a grid here was published early
    (2, True),         # mid-programme hold
    (6.5, True),       # a day that has run very long
    (8.9, True),       # still inside the window
    (9.5, False),      # hours after everything; the feed is just sitting there
    (-24, False),      # the day before
    (24, False),       # the day after
])
def test_a_stalled_grid_is_a_pause_only_during_racing_hours(hours, paused):
    assert _racing_is_merely_paused(ev(), now=at(hours)) is paused


def test_an_event_with_no_start_time_is_never_a_pause():
    # Without a clock we cannot tell the two apart, and the safe answer is the
    # old behaviour: do not claim a race is merely delayed.
    assert _racing_is_merely_paused(ev(start=None), now=at(0)) is False


def test_a_naive_start_time_is_treated_as_utc():
    naive = datetime.datetime(2026, 9, 12, 18, 30)
    assert _racing_is_merely_paused(ev(start=naive), now=at(1)) is True


def test_the_window_is_the_one_the_constants_declare():
    # Guards against someone widening a constant and quietly changing which
    # grids get called delays.
    assert _racing_is_merely_paused(
        ev(), now=GATE - datetime.timedelta(seconds=_DELAY_WINDOW_PRE_S - 1))
    assert not _racing_is_merely_paused(
        ev(), now=GATE - datetime.timedelta(seconds=_DELAY_WINDOW_PRE_S + 1))
    assert _racing_is_merely_paused(
        ev(), now=GATE + datetime.timedelta(seconds=_DELAY_WINDOW_POST_S - 1))
    assert not _racing_is_merely_paused(
        ev(), now=GATE + datetime.timedelta(seconds=_DELAY_WINDOW_POST_S + 1))


# --- what the lock screen says while it waits ---------------------------------

def card(state, riders=None):
    return _la_content_state({
        "event": {"venue": "Historic Crew Stadium"},
        "timing": {
            "race_name": "450 Main Event",
            "race_state": state,
            "clock": {"remaining": 1200, "flag": "red"},
            "riders": riders if riders is not None else [
                {"position": 1, "name": "Jett Lawrence", "number": "18",
                 "gap": None},
                {"position": 2, "name": "Chase Sexton", "number": "1",
                 "gap": "1.613"},
            ],
        },
    })


def test_a_delayed_card_says_delayed():
    assert card("delayed")["race"].endswith("· delayed")


def test_a_delayed_card_claims_no_gaps():
    # The times the feed still holds belong to a session that has stopped.
    # Claiming them describes racing that is not happening.
    assert [r["g"] for r in card("delayed")["riders"]] == ["", ""]


def test_a_delayed_card_does_not_say_leader_or_winner():
    words = " ".join(r["g"] for r in card("delayed")["riders"])
    assert "Leader" not in words and "Winner" not in words


def test_a_delayed_card_shows_no_countdown():
    # A clock that is not running must not appear to be running.
    assert card("delayed")["remaining"] is None


def test_the_riders_still_list_because_who_is_entered_is_real():
    assert len(card("delayed")["riders"]) == 2


def test_delayed_does_not_disturb_the_other_states():
    assert card("staged")["race"].endswith("· on the gate")
    assert card("finished")["race"].endswith("· final")
    assert card("racing")["remaining"] == 1200
    assert card("racing")["riders"][0]["g"] == "Leader"
    assert card("finished")["riders"][0]["g"] == "Winner"
