"""A mock with a held gate — so the delayed card can be seen on a real phone.

    python -m pytest tests/ -q

`hold_s` splits each session's gate into on-the-gate -> DELAYED -> green. It
exists so the "delayed" rendering added 09-11 can be drawn on a lock screen
before a real race needs it. It tests the DRAWING, not the detection — see
MAX_HOLD_S in src/mockrace.py.

No database and no network. A hand-cranked clock runs each programme in
milliseconds.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.mockrace as mockrace  # noqa: E402


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _run(monkeypatch, minutes=3, sessions=1, hold_s=120):
    clock = _Clock()
    monkeypatch.setattr(mockrace.time, "time", clock)
    mockrace.start(minutes, sessions=sessions, hold_s=hold_s)
    return clock


def _at(clock, offset):
    clock.t = 1_000_000.0 + offset
    return mockrace.timing()


# --- the shape of a held session ---------------------------------------------

def test_a_held_session_goes_gate_then_delayed_then_green_then_finish(monkeypatch):
    # minutes=3 -> 120s gate + 60s racing; hold 120s sits between them.
    c = _run(monkeypatch, hold_s=120)
    assert _at(c, 0)["race_state"] == "staged"
    assert _at(c, 119)["race_state"] == "staged"
    assert _at(c, 121)["race_state"] == "delayed"
    assert _at(c, 239)["race_state"] == "delayed"
    assert _at(c, 241)["race_state"] == "racing"
    assert _at(c, 299)["race_state"] == "racing"
    assert _at(c, 301)["race_state"] == "finished"


def test_a_hold_delays_the_race_rather_than_shortening_it(monkeypatch):
    # A real weather hold pushes the race back; it does not cut laps. So the
    # racing phase is the full 60s either way, just starting later.
    # timing() is None once racing is over for the day, which is correct —
    # count only the seconds that have a session.
    def racing_seconds(clock):
        return sum((_at(clock, t) or {}).get("race_state") == "racing"
                   for t in range(0, 400))

    held = _run(monkeypatch, hold_s=120)
    racing_held = racing_seconds(held)
    mockrace.stop()
    plain = _run(monkeypatch, hold_s=0)
    racing_plain = racing_seconds(plain)
    mockrace.stop()
    assert racing_held == racing_plain


# --- what a held grid carries -------------------------------------------------

def test_a_held_grid_has_no_running_clock(monkeypatch):
    c = _run(monkeypatch, hold_s=120)
    assert _at(c, 150)["clock"]["remaining"] is None


def test_a_held_grid_does_not_move(monkeypatch):
    # The whole point: a hold is a frozen order. If it moved, the stall rule
    # would never see it as stalled, and it would not be a hold.
    c = _run(monkeypatch, hold_s=120)
    first = [r["name"] for r in _at(c, 125)["riders"]]
    later = [r["name"] for r in _at(c, 235)["riders"]]
    assert first == later


def test_the_clock_counts_down_from_the_green_flag_not_the_start(monkeypatch):
    c = _run(monkeypatch, hold_s=120)
    # Green at 240s, 60s of racing: at 250s there are 50s left, not 170s-10s.
    assert _at(c, 250)["clock"]["remaining"] == 50


# --- the hold must not disturb anything that ran before it existed ------------

def test_no_hold_is_byte_for_byte_the_old_behaviour(monkeypatch):
    # Every mock that has proven something this week ran without a hold. If
    # this drifted, those proofs would stop meaning what they meant.
    c = _run(monkeypatch, hold_s=0)
    assert _at(c, 0)["race_state"] == "staged"
    assert _at(c, 121)["race_state"] == "racing"
    assert _at(c, 181)["race_state"] == "finished"
    assert _at(c, 150)["clock"]["remaining"] == 30
    assert all(_at(c, t)["race_state"] != "delayed" for t in range(0, 240))


def test_a_hold_is_capped(monkeypatch):
    # Every user sees a mock while it runs. A typo must not hold the app on a
    # fake delay for an hour.
    clock = _Clock()
    monkeypatch.setattr(mockrace.time, "time", clock)
    mockrace.start(3, hold_s=99_999)
    assert mockrace._run["hold_s"] == mockrace.MAX_HOLD_S
    mockrace.stop()


def test_a_negative_hold_is_no_hold(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(mockrace.time, "time", clock)
    mockrace.start(3, hold_s=-50)
    assert mockrace._run["hold_s"] == 0
    mockrace.stop()


def test_a_held_programme_still_reaches_the_end_of_day_card(monkeypatch):
    # Two held sessions: the seam and the day-complete card must survive the
    # longer blocks. block = 180 + 120 + 60 = 360; racing ends at 720.
    c = _run(monkeypatch, sessions=2, hold_s=120)
    assert _at(c, 725) is None                 # racing over
    c.t = 1_000_000.0 + 725
    day = mockrace.day_complete()
    assert day is not None and set(day) == {"250", "450"}
