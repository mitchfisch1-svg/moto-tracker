"""Don't spend the Live Activity's eight hours before the racing starts.

    python -m pytest tests/ -q

iOS ends a Live Activity about eight hours after it starts. The race window
opens six hours before the gate so the app can show qualifying, and launching
cards then burns most of the budget before anything anyone came for. Against
Columbus's published schedule (09-12), a card launched when the window opened
at 08:30 would be killed by the system around 16:30 — before 250 Moto 2 at
16:51 and 450 Moto 2 at 17:29 — and push-to-start will not relaunch, because
each phone is launched once per event on purpose.

No database and no network.
"""

import datetime
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _LA_START_LEAD_S, _ready_to_launch  # noqa: E402

UTC = datetime.timezone.utc
# The payload serves the GATE as start_time_utc (see _decorate_event), which for
# Columbus is 19:00 UTC = 3:00 PM ET. The first moto is 15:06 ET.
GATE = datetime.datetime(2026, 9, 12, 19, 0, tzinfo=UTC)


def ev(start=GATE.isoformat()):
    return {"event_id": 29, "venue": "Historic Crew Stadium",
            "start_time_utc": start}


def et(hh, mm=0):
    """A wall-clock Eastern time on race day, as UTC (EDT = UTC-4)."""
    return datetime.datetime(2026, 9, 12, hh + 4, mm, tzinfo=UTC)


# --- the actual race day ------------------------------------------------------

@pytest.mark.parametrize("hh,mm,launch,why", [
    (8, 30, False, "window opens; 8h would expire ~16:30, before both Moto 2s"),
    (10, 0, False, "qualifying — a card here gets swiped long before racing"),
    (12, 0, False, "wildcard races"),
    (13, 30, False, "still 90 min out"),
    (14, 0, True, "one hour to the gate — racing is obviously imminent"),
    (14, 30, True, "opening ceremonies"),
    (15, 6, True, "250 Moto 1"),
    (17, 29, True, "450 Moto 2 — a late install still gets a card"),
    (19, 0, True, "after the racing; results are still worth a card"),
])
def test_columbus_schedule(hh, mm, launch, why):
    assert _ready_to_launch(ev(), now=et(hh, mm)) is launch, why


def test_the_last_moto_is_inside_the_eight_hours():
    # The point of the whole change: launch, then check the system's 8-hour
    # limit still covers the final moto.
    first_allowed = GATE - datetime.timedelta(seconds=_LA_START_LEAD_S)
    expires = first_allowed + datetime.timedelta(hours=8)
    last_moto_ends = et(17, 29) + datetime.timedelta(minutes=35)
    assert expires > last_moto_ends
    # ...with room for a rain delay, which is the reason for the margin. The
    # 09-11 forecast for Columbus was 55% rain.
    assert (expires - last_moto_ends) >= datetime.timedelta(hours=3)


def test_the_card_is_not_parked_on_a_lock_screen_for_hours_first():
    # The swipe problem: every idle hour before the racing is a chance for
    # someone to clear the card, and a swipe is permanent for the same
    # once-per-phone reason a launch is.
    first_allowed = GATE - datetime.timedelta(seconds=_LA_START_LEAD_S)
    assert (et(15, 6) - first_allowed) <= datetime.timedelta(hours=1, minutes=30)


# --- degrade toward launching, never toward silence ---------------------------

def test_a_missing_start_time_still_launches():
    # The mock event carries no start_time_utc at all. It must still be able to
    # put a card on a phone, or every push-to-start test goes silently dead.
    assert _ready_to_launch({"event_id": 999999}) is True
    assert _ready_to_launch({"event_id": 999999, "start_time_utc": None}) is True


def test_junk_start_times_still_launch():
    for bad in ("", "not a date", "2026-13-45T99:99", 12345, [], {}):
        assert _ready_to_launch(ev(start=bad)) is True, bad


def test_no_event_at_all_still_launches():
    assert _ready_to_launch(None) is True
    assert _ready_to_launch({}) is True


# --- shapes the payload can actually take -------------------------------------

def test_a_real_datetime_works_as_well_as_a_string():
    assert _ready_to_launch({"start_time_utc": GATE}, now=et(14)) is True
    assert _ready_to_launch({"start_time_utc": GATE}, now=et(8, 30)) is False


def test_a_naive_start_time_is_read_as_utc():
    naive = datetime.datetime(2026, 9, 12, 19, 0)
    assert _ready_to_launch({"start_time_utc": naive}, now=et(14)) is True
    assert _ready_to_launch({"start_time_utc": naive}, now=et(8, 30)) is False


def test_the_boundary_is_exactly_the_lead():
    just_before = GATE - datetime.timedelta(seconds=_LA_START_LEAD_S + 1)
    just_after = GATE - datetime.timedelta(seconds=_LA_START_LEAD_S - 1)
    assert _ready_to_launch(ev(), now=just_before) is False
    assert _ready_to_launch(ev(), now=just_after) is True
