"""The lock screen froze because the guard against freezing never fired.

At Ironman the Live Activity read "250 Moto #1 · on the gate" at 1:13 when the
green flag had flown at 1:11 and the app had it right. The loop already had a
guard: only push when the state changes, because Apple budgets Live Activity
updates and silently throttles once you exceed it — the comment above it says
that is how the lock screen had frozen before.

But the state it compared included `remaining`, the race clock in seconds. It
changes every second. So "has anything changed?" was true on every pass, the
guard never fired once, and the loop pushed every 10 seconds for a whole moto:
~180 pushes a race, well over a thousand across a race day.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _la_change_key, _LA_MIN_GAP_S, _LA_INTERVAL_S  # noqa: E402


def _state(remaining, riders=None, race="250 Moto #1"):
    return {
        "race": race, "venue": "Ironman", "flag": "green",
        "riders": riders or [{"p": 1, "n": "Davies", "num": "37", "g": "Leader"}],
        "remaining": remaining,
    }


def test_the_clock_alone_is_not_news():
    """The whole bug, in one assertion."""
    assert _la_change_key(_state(1792)) == _la_change_key(_state(1791))
    assert _la_change_key(_state(1792)) == _la_change_key(_state(60))


def test_the_order_changing_is_news():
    a = _state(1700, [{"p": 1, "n": "Davies", "num": "37", "g": "Leader"}])
    b = _state(1700, [{"p": 1, "n": "Hymas", "num": "29", "g": "Leader"}])
    assert _la_change_key(a) != _la_change_key(b)


def test_a_gap_changing_is_news():
    a = _state(1700, [{"p": 2, "n": "Hymas", "num": "29", "g": "1.613"}])
    b = _state(1700, [{"p": 2, "n": "Hymas", "num": "29", "g": "2.140"}])
    assert _la_change_key(a) != _la_change_key(b)


def test_leaving_the_gate_is_news():
    """The exact transition the lock screen missed for two minutes."""
    staged = _state(None, race="250 Moto #1 · on the gate")
    racing = _state(1792, race="250 Moto #1")
    assert _la_change_key(staged) != _la_change_key(racing)


def test_the_checkered_flag_is_news():
    assert _la_change_key(_state(0, race="250 Moto #1")) != \
           _la_change_key(_state(None, race="250 Moto #1 · final"))


def test_the_clock_still_goes_out_in_the_payload():
    """Excluded from the COMPARISON, not from what the lock screen shows."""
    assert _state(1792)["remaining"] == 1792


def test_the_floor_leaves_the_feed_room_to_matter():
    """Long enough to stay inside Apple's budget across a six-moto day, short
    enough that a pass on the last lap is not stale by the time it lands."""
    assert _LA_INTERVAL_S <= _LA_MIN_GAP_S <= 30
