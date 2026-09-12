"""When a lock-screen card is worth launching — and it is per series.

    python -m pytest tests/ -q

iOS ends a Live Activity about eight hours after it starts, and each phone is
launched once per event, so a card shown too early is lost twice over: by
Apple's clock, or by someone swiping away something parked on their lock screen
since breakfast. Neither comes back.

SMX PLAYOFFS: wait for the first POINTS race. Columbus (09-12) runs qualifying
at 08:50, wildcards at noon, and 250 Moto 1 at 15:06. Reading it off the feed
rather than the clock means a rain delay carries the launch with it.

SX and MX: launch when the window opens, which is when qualifying starts.

No database and no network.
"""

import datetime
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import (  # noqa: E402
    _LA_SMX_BACKSTOP_S,
    _is_points_race,
    _ready_to_launch,
)

UTC = datetime.timezone.utc
# The payload serves the GATE as start_time_utc (see _decorate_event): 19:00 UTC
# = 3:00 PM ET for Columbus. First moto 15:06 ET.
GATE = datetime.datetime(2026, 9, 12, 19, 0, tzinfo=UTC)


def smx(start=GATE.isoformat()):
    return {"event_id": 29, "series": "SMX", "venue": "Historic Crew Stadium",
            "start_time_utc": start}


def on_track(name):
    return {"race_name": name}


def et(hh, mm=0):
    """A wall-clock Eastern time on race day, as UTC (EDT = UTC-4)."""
    return datetime.datetime(2026, 9, 12, hh + 4, mm, tzinfo=UTC)


# --- which sessions count as racing -------------------------------------------

@pytest.mark.parametrize("name,points", [
    ("250 Moto 1", True),
    ("450 Moto 1", True),
    ("250 Moto 2", True),
    ("450 Moto 2", True),
    ("SMX Next Main Event", True),
    ("250 Moto 1 (system test)", True),      # the mock must still launch
    ("250 Seeded Free Practice", False),
    ("SMX Next Free Practice", False),
    ("250 Group B Qualifying 1", False),
    ("450 Wildcard Race", False),            # racing, but scores nothing
    ("", False),
    (None, False),
])
def test_only_a_scoring_race_counts(name, points):
    assert _is_points_race(on_track(name)) is points


def test_junk_timing_does_not_raise():
    # This runs inside the push loop; an exception here would skip a cycle.
    for junk in (None, {}, {"race_name": 123}, {"race_name": []}):
        assert _is_points_race(junk) is False


# --- SMX: the actual Columbus day ---------------------------------------------

@pytest.mark.parametrize("hh,mm,name,launch,why", [
    (8, 50, "250 Seeded Free Practice", False, "qualifying practice"),
    (10, 30, "450 Seeded Free Practice", False, "still practice"),
    (12, 0, "450 Wildcard Race", False, "wildcards score nothing"),
    (14, 30, "250 Moto 1", True, "gridding up for the first points race"),
    (15, 6, "250 Moto 1", True, "250 Moto 1"),
    (15, 43, "450 Moto 1", True, "450 Moto 1"),
    (17, 29, "450 Moto 2", True, "last race — a late install still gets one"),
])
def test_columbus_launches_at_the_first_points_race(hh, mm, name, launch, why):
    assert _ready_to_launch(smx(), on_track(name), now=et(hh, mm)) is launch, why


def test_a_rain_delay_carries_the_launch_with_it():
    # The reason this reads the feed instead of the clock. Racing slips two
    # hours; the card should slip too, not sit on a lock screen through the
    # hold burning its eight hours.
    assert _ready_to_launch(smx(), on_track("250 Moto 1"), now=et(17)) is True
    assert _ready_to_launch(smx(), on_track("250 Seeded Free Practice"),
                            now=et(16)) is False


def test_the_backstop_launches_even_if_nothing_ever_scores():
    # If the feed renamed its sessions, no card all day would be worse than an
    # early one. 90 minutes past the gate, launch regardless.
    late = GATE + datetime.timedelta(seconds=_LA_SMX_BACKSTOP_S + 60)
    early = GATE + datetime.timedelta(seconds=_LA_SMX_BACKSTOP_S - 60)
    weird = on_track("Something Nobody Has Seen Before")
    assert _ready_to_launch(smx(), weird, now=early) is False
    assert _ready_to_launch(smx(), weird, now=late) is True


def test_the_last_moto_is_well_inside_the_eight_hours():
    launched = et(15, 6)                       # 250 Moto 1
    expires = launched + datetime.timedelta(hours=8)
    last_moto_ends = et(17, 29) + datetime.timedelta(minutes=35)
    assert expires - last_moto_ends >= datetime.timedelta(hours=4)


# --- SX and MX: from the window opening, i.e. qualifying ----------------------

@pytest.mark.parametrize("series", ["SX", "MX"])
def test_the_regular_season_launches_at_qualifying(series):
    ev = {"event_id": 1, "series": series, "start_time_utc": GATE.isoformat()}
    for name in ("250 Group B Qualifying 1", "450 Free Practice", ""):
        assert _ready_to_launch(ev, on_track(name), now=et(8, 50)) is True


def test_an_unknown_series_launches_rather_than_staying_silent():
    ev = {"event_id": 1, "series": None, "start_time_utc": GATE.isoformat()}
    assert _ready_to_launch(ev, on_track("Practice"), now=et(8)) is True


# --- degrade toward launching, never toward silence ---------------------------

def test_the_mock_event_still_launches():
    # It has no start_time_utc, and its series is SMX. It gets there on the
    # race name — miss this and every push-to-start test goes silently dead.
    mock = {"event_id": 999999, "series": "SMX", "venue": "MXT System Test"}
    assert _ready_to_launch(mock, on_track("250 Moto 1 (system test)")) is True


def test_an_smx_event_with_no_readable_start_still_launches():
    for bad in (None, "", "not a date", 12345, [], {}):
        assert _ready_to_launch(smx(start=bad), on_track("Practice")) is True


def test_no_event_at_all_launches():
    assert _ready_to_launch(None, on_track("Practice")) is True
    assert _ready_to_launch({}, on_track("Practice")) is True
