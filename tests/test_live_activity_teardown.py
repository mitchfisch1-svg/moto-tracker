"""A finished card had no route off a locked phone.

Found 08-31 by running a mock and then watching what happened when it ended.
Two teardown paths existed and neither could run:

  * the SERVER checks `window_open` before the payload, so once the race window
    shut the loop slept and never reached the branch that ends activities;
  * the APP's `LiveActivity.end()` lived in RaceDay(), rendered only by
    `{tab === 'raceday' && <RaceDay />}` — and the app opens on Standings.

So the mock ended and the card sat on the lock screen reading "0:09, Beaumer
P1" until it was swiped away by hand. Three times that evening the teardown
appeared to work, and each time it worked only because somebody happened to be
on the Race Day tab when the race ended.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import (  # noqa: E402
    _LA_RESULT_HOLD_S, _LA_SESSION_DONE, _la_closing_frame,
    _la_should_end_now,
)


def test_a_closed_window_ends_the_cards():
    assert _la_should_end_now(was_open=True, window_open=False,
                              window_known=True) is True


def test_a_failed_window_check_is_not_a_closed_window():
    """The bug this guard exists for.

    The window check falls back to False on ANY exception. Without the
    `window_known` guard, one transient database blip mid-moto would end every
    Live Activity on every phone. A stale card recovers on the next push; an
    ended one is gone.
    """
    assert _la_should_end_now(was_open=True, window_open=False,
                              window_known=False) is False


def test_an_open_window_ends_nothing():
    assert _la_should_end_now(was_open=True, window_open=True,
                              window_known=True) is False


def test_a_loop_that_was_never_open_ends_nothing():
    """Fresh process, no race: don't touch cards we never pushed to."""
    assert _la_should_end_now(was_open=False, window_open=False,
                              window_known=True) is False


def _state(race="250 Moto 1", remaining=412):
    return {
        "race": race, "venue": "Historic Crew Stadium",
        "riders": [{"p": 1, "n": "Beaumer", "num": "13", "g": "Leader"},
                   {"p": 2, "n": "Hymas", "num": "29", "g": "0.727"}],
        "flag": "green", "remaining": remaining,
    }


def test_the_last_thing_we_sent_becomes_the_result():
    """No fresh payload when the window shuts — but we still hold the order."""
    final, hold = _la_closing_frame(_state())
    assert final["race"] == "250 Moto 1 · final"
    assert [r["n"] for r in final["riders"]] == ["Beaumer", "Hymas"]
    assert hold == _LA_RESULT_HOLD_S


def test_the_result_stays_up_for_an_hour():
    """Mitch, 08-31: half an hour was gone before anyone came back to it."""
    assert _LA_RESULT_HOLD_S == 3600


def test_the_countdown_is_cleared_on_the_final_frame():
    """A finished race with a clock still on it reads as still running."""
    final, _ = _la_closing_frame(_state(remaining=412))
    assert final["remaining"] is None


def test_a_staged_card_does_not_end_up_double_labelled():
    """"250 Moto 1 · on the gate · final" is nonsense. Strip, then label."""
    final, _ = _la_closing_frame(_state(race="250 Moto 1 · on the gate"))
    assert final["race"] == "250 Moto 1 · final"


def test_nothing_pushed_means_nothing_to_leave_behind():
    """A window that opens and shuts with no race gets a blank, dismissed at once.

    Holding an empty card for an hour would be worse than clearing it: there is
    no result to read, just a dead card the user has to swipe away.
    """
    final, hold = _la_closing_frame(None)
    assert final == _LA_SESSION_DONE
    assert hold == 0


def test_the_closing_frame_does_not_mutate_what_it_was_given():
    """The loop keeps pushing from `last_pushed`; relabelling it in place would
    put "· final" on a live card."""
    live = _state()
    _la_closing_frame(live)
    assert live["race"] == "250 Moto 1"
    assert live["remaining"] == 412
