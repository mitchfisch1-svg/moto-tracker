"""When the home-screen widget leaves the championship for a running order.

    python -m pytest tests/ -q

/widget/standings swaps the championship top five for the live order whenever
something is on track, because season points frozen at last week's total are
the wrong thing to stare at mid-moto.

That is wrong for SMX. Its programme runs most of a working day — qualifying
from 08:50, wildcards at noon, first moto 15:06 — so the widget would show
practice times for six hours before the racing that counts. Found 09-11: the
widget read "250 Unseeded Qualifying 1" on a home screen the night before
Columbus, off a grid the provider had published a day early.

Same predicate as the lock-screen card, so the two surfaces cannot disagree
about what counts as racing.

No database and no network.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _widget_should_show_live  # noqa: E402


def ev(series):
    return {"event_id": 29, "series": series, "venue": "Historic Crew Stadium"}


def on(name):
    return {"race_name": name}


# --- SMX: the championship until the motos ------------------------------------

@pytest.mark.parametrize("name,live", [
    ("250 Unseeded Qualifying 1", False),   # what was on the home screen
    ("250 Seeded Free Practice", False),
    ("SMX Next Free Practice", False),
    ("450 Wildcard Race", False),           # races, but scores nothing
    ("250 Moto 1", True),
    ("450 Moto 1", True),
    ("250 Moto 2", True),
    ("450 Moto 2", True),
    ("SMX Next Main Event", True),
])
def test_smx_widget_waits_for_a_points_race(name, live):
    assert _widget_should_show_live(ev("SMX"), on(name)) is live


def test_the_columbus_morning_stays_on_the_championship():
    morning = ["250 Seeded Free Practice", "450 Unseeded Free Practice",
               "SMX Next Free Practice", "250 Unseeded Qualifying 1",
               "450 Wildcard Race"]
    assert not any(_widget_should_show_live(ev("SMX"), on(n)) for n in morning)


# --- SX and MX keep the old behaviour ----------------------------------------

@pytest.mark.parametrize("series", ["SX", "MX"])
@pytest.mark.parametrize("name", ["250 Group B Qualifying 1", "450 Heat 1",
                                  "250 LCQ", "450 Moto 1", "450 Main Event"])
def test_the_regular_season_shows_whatever_is_on_track(series, name):
    assert _widget_should_show_live(ev(series), on(name)) is True


def test_an_unknown_series_shows_what_is_on_track():
    assert _widget_should_show_live(ev(None), on("Practice")) is True
    assert _widget_should_show_live({}, on("Practice")) is True
    assert _widget_should_show_live(None, on("Practice")) is True


# --- nothing on track -> the championship, always ------------------------------

@pytest.mark.parametrize("series", ["SMX", "SX", "MX", None])
def test_no_session_means_the_championship(series):
    assert _widget_should_show_live(ev(series), None) is False
    assert _widget_should_show_live(ev(series), {}) is False


def test_junk_timing_does_not_raise():
    # This runs inside a widget request; an exception renders "Can't reach MXT"
    # and the widget sits there.
    for junk in ({"race_name": None}, {"race_name": 123}, {"race_name": []}):
        assert _widget_should_show_live(ev("SMX"), junk) is False


# --- the two surfaces must agree ----------------------------------------------

def test_the_widget_and_the_card_agree_about_what_counts_as_racing():
    from src.api.main import _is_points_race
    for name in ("250 Moto 1", "450 Moto 2", "SMX Next Main Event",
                 "250 Unseeded Qualifying 1", "450 Wildcard Race",
                 "250 Seeded Free Practice"):
        assert (_widget_should_show_live(ev("SMX"), on(name))
                is _is_points_race(on(name))), name
