"""A race day is a SEQUENCE of sessions, and the seam between two was untested.

The first mock ran one moto. That exercised the gate, the green flag, the
running order and the push budget — everything except the moment one race ends
and a different one appears back on the gate. On a real Saturday that seam
happens five or six times, and the Live Activity has to carry a new race name
through it without being torn down.

These tests walk a two-session programme second by second and assert what the
lock screen would be told at each point.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.mockrace as mockrace  # noqa: E402


class _Clock:
    """A hand-cranked clock, so a 45-minute programme runs in milliseconds."""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _programme(monkeypatch, minutes=3, sessions=2):
    clock = _Clock()
    monkeypatch.setattr(mockrace.time, "time", clock)
    mockrace.start(minutes, sessions=sessions)
    return clock


def _at(clock, offset):
    """The timing block `offset` seconds into the run."""
    clock.t = 1_000_000.0 + offset
    return mockrace.timing()


def test_one_session_runs_gate_then_green_then_finish(monkeypatch):
    clock = _programme(monkeypatch, minutes=3, sessions=1)
    assert _at(clock, 0)["race_state"] == "staged"
    assert _at(clock, 119)["race_state"] == "staged"
    assert _at(clock, 121)["race_state"] == "racing"
    assert _at(clock, 179)["race_state"] == "racing"
    # The finish is new. Before this, the feed simply vanished mid-race and the
    # card never saw a "finished" state at all.
    assert _at(clock, 181)["race_state"] == "finished"
    assert _at(clock, 239)["race_state"] == "finished"
    assert _at(clock, 241) is None


def test_the_gate_hides_the_clock_and_green_reveals_it(monkeypatch):
    clock = _programme(monkeypatch, minutes=3, sessions=1)
    staged = _at(clock, 10)["clock"]
    assert staged["remaining"] is None and staged["flag"] == "prestage"
    green = _at(clock, 130)["clock"]
    assert green["flag"] == "green"
    # Counts down the SESSION, not the whole programme.
    assert green["remaining"] == 180 - 130
    done = _at(clock, 200)["clock"]
    assert done["remaining"] == 0 and done["flag"] == "checkered"


def test_the_second_session_is_a_different_race_back_on_the_gate(monkeypatch):
    """The seam. This is the whole reason the file exists."""
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    first_racing = _at(clock, 150)
    first_final = _at(clock, 200)
    second_gate = _at(clock, 250)
    second_racing = _at(clock, 370)

    # One race ends...
    assert first_final["race_state"] == "finished"
    assert first_final["race_name"] == first_racing["race_name"]
    # ...and a DIFFERENT one is on the gate.
    assert second_gate["race_state"] == "staged"
    assert second_gate["race_name"] != first_racing["race_name"]
    assert second_racing["race_name"] == second_gate["race_name"]
    # The race name is inside the Live Activity change key, so a session change
    # is guaranteed to push rather than being mistaken for more of the same.
    assert "250 Moto 1" in first_racing["race_name"]
    assert "450 Moto 1" in second_gate["race_name"]


def test_each_session_races_a_genuinely_different_order(monkeypatch):
    """A second moto replaying the first would test the seam but not the card."""
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    first = [r["name"] for r in _at(clock, 150)["riders"]]
    second = [r["name"] for r in _at(clock, 370)["riders"]]
    assert first != second


def test_the_programme_ends_and_stays_ended(monkeypatch):
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    assert _at(clock, 479) is not None
    assert _at(clock, 481) is None
    assert _at(clock, 10_000) is None
    assert mockrace.status()["running"] is False


def test_status_names_the_session_you_are_in(monkeypatch):
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    clock.t = 1_000_000.0 + 150
    st = mockrace.status()
    assert st["session"] == 1 and st["sessions"] == 2
    assert st["state"] == "racing"
    clock.t = 1_000_000.0 + 250
    st = mockrace.status()
    assert st["session"] == 2 and st["state"] == "staged"


def test_a_run_cannot_be_left_going_forever(monkeypatch):
    """Every user of the app sees a mock while it runs. Bound it."""
    clock = _Clock()
    monkeypatch.setattr(mockrace.time, "time", clock)
    run = mockrace.start(999, sessions=999)
    longest = mockrace.MAX_SESSIONS * (
        mockrace.MAX_MINUTES * 60 + mockrace.FINISH_S)
    assert run["remaining_s"] <= longest
    assert run["sessions"] == mockrace.MAX_SESSIONS
    mockrace.stop()
