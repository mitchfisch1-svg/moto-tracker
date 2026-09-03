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
    # Includes the day-complete phase, which is deliberately part of a run:
    # the window must stay open past the last session or the loop takes the
    # window-closed teardown instead of building the end-of-day card.
    longest = mockrace.MAX_SESSIONS * (
        mockrace.MAX_MINUTES * 60 + mockrace.FINISH_S) + mockrace.DAY_DONE_S
    assert run["remaining_s"] <= longest
    assert run["sessions"] == mockrace.MAX_SESSIONS
    mockrace.stop()


# --- the day itself ending ---------------------------------------------------
# The end-of-day card (top three of BOTH classes, six rows, a class label) is
# built only when /live reports day_complete — and nothing could produce that.
# So the widget change shipped in 1.6.0 compiled, passed its checks, and had
# never been DRAWN on a screen. This phase is what makes looking at it possible
# without waiting for a real race day.

def test_racing_ends_but_the_run_keeps_the_window_open(monkeypatch):
    """If `running` went false the loop would take the window-CLOSED teardown
    path and never build the day-complete card at all."""
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    racing = 2 * (180 + mockrace.FINISH_S)
    clock.t = 1_000_000.0 + racing + 10
    assert mockrace.timing() is None            # nothing on track
    assert mockrace.status()["running"] is True  # ...but still running
    assert mockrace.status()["state"] == "day_done"


def test_the_day_complete_block_covers_every_class_that_raced(monkeypatch):
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    clock.t = 1_000_000.0 + 2 * (180 + mockrace.FINISH_S) + 10
    day = mockrace.day_complete()
    assert sorted(day) == ["250", "450"]
    assert all(len(rows) >= 3 for rows in day.values())


def test_each_class_result_is_its_own_race_not_a_copy(monkeypatch):
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    clock.t = 1_000_000.0 + 2 * (180 + mockrace.FINISH_S) + 10
    day = mockrace.day_complete()
    assert [r["name"] for r in day["250"][:3]] != [r["name"] for r in day["450"][:3]]


def test_points_are_carried_so_the_card_has_something_to_show(monkeypatch):
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    clock.t = 1_000_000.0 + 2 * (180 + mockrace.FINISH_S) + 10
    day = mockrace.day_complete()
    assert [r["points"] for r in day["450"][:3]] == [25, 22, 20]


def test_there_is_no_day_complete_while_racing_is_still_on(monkeypatch):
    """Reporting the day done mid-programme would blank a live card."""
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    for off in (10, 150, 250, 400, 470):
        clock.t = 1_000_000.0 + off
        assert mockrace.day_complete() is None, f"day_complete leaked at t+{off}"


def test_the_day_complete_window_expires_with_the_run(monkeypatch):
    clock = _programme(monkeypatch, minutes=3, sessions=2)
    end = 2 * (180 + mockrace.FINISH_S) + mockrace.DAY_DONE_S
    clock.t = 1_000_000.0 + end - 5
    assert mockrace.day_complete() is not None
    clock.t = 1_000_000.0 + end + 5
    assert mockrace.day_complete() is None
    assert mockrace.status()["running"] is False


def test_a_session_too_short_to_race_is_lengthened(monkeypatch):
    """GATE_S is 120s. A 2-minute session is all gate: `staged` swallows it and
    it skips to `finished` without ever going green — silently, which is the
    worst way to waste a run. Found by launching one (09-02)."""
    clock = _Clock()
    monkeypatch.setattr(mockrace.time, "time", clock)
    mockrace.start(1, sessions=1)
    # Whatever was asked for, the session must be able to reach a green flag.
    saw_racing = False
    for off in range(0, mockrace.MIN_MINUTES * 60, 10):
        clock.t = 1_000_000.0 + off
        t = mockrace.timing()
        if t and t["race_state"] == "racing":
            saw_racing = True
            break
    assert saw_racing, "a run was accepted that can never go green"
    mockrace.stop()


# --- push-to-start, the one path a mock could never reach --------------------
# The loop guards it with `f"lastart:{ev_id}" if ev_id else None`, so the mock's
# event_id of 0 skipped the branch entirely. That was deliberate — it stopped a
# mock remote-launching a card onto other people's phones. The install count
# turned out to be FOUR, not 35, so it is now opt-in instead of impossible.

def test_a_mock_cannot_remote_launch_by_default(monkeypatch):
    """The default must stay falsy. This is the safety property."""
    clock = _Clock()
    monkeypatch.setattr(mockrace.time, "time", clock)
    mockrace.start(3, sessions=1)
    assert mockrace.event_id() == 0
    assert not mockrace.event_id(), "a truthy default would fire push-to-start"
    mockrace.stop()


def test_opting_in_gives_a_truthy_event_id(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(mockrace.time, "time", clock)
    mockrace.start(3, sessions=1, push_to_start=True)
    assert mockrace.event_id() == mockrace.PUSH_TO_START_EVENT_ID
    assert mockrace.event_id(), "the loop's start branch keys off truthiness"
    mockrace.stop()


def test_the_mock_event_id_cannot_collide_with_a_real_one():
    """Real event ids are small serials; the push_sent key is shared."""
    assert mockrace.PUSH_TO_START_EVENT_ID > 100_000


def test_nothing_running_reports_the_safe_id():
    mockrace.stop()
    assert mockrace.event_id() == mockrace.DEFAULT_EVENT_ID
