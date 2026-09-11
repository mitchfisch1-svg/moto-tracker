"""Push-to-start once per TOKEN, so a late install still gets a card.

    python -m pytest tests/ -q

It used to be one `lastart:{event}` key: the first cycle pushed every start
token on file and marked the whole event done. Anyone who installed after the
race window opened was never sent a card. On a showcase weekend that skips the
exact people a post about the app brings in — they install DURING the race.

The one failure that must never happen is the opposite: a phone that already
got its card getting another every 10 seconds. Most of these tests are about
that.

No database and no network.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _lastart_key, _tokens_to_start  # noqa: E402

EV = 29
A, B, C = "a" * 160, "b" * 160, "c" * 160


def start(tok):
    return {"token": tok, "kind": "start"}


def update(tok):
    return {"token": tok, "kind": "update"}


# --- the morning burst is unchanged -------------------------------------------

def test_the_first_cycle_launches_every_start_token():
    rows = [start(A), start(B), start(C)]
    assert _tokens_to_start(rows, EV, set(), set()) == {A, B, C}


def test_update_tokens_are_never_launched():
    # Update tokens drive a card that already exists; launching one is wrong.
    assert _tokens_to_start([update(A), update(B)], EV, set(), set()) == set()


# --- the fix: someone who installs later -------------------------------------

def test_a_token_that_arrives_later_is_launched_on_the_next_cycle():
    # A and B were launched at 8:30. C installed at noon.
    recorded = {_lastart_key(EV, A), _lastart_key(EV, B)}
    rows = [start(A), start(B), start(C)]
    assert _tokens_to_start(rows, EV, recorded, set()) == {C}


# --- the failure that must never happen: a second card -----------------------

def test_a_launched_token_is_never_launched_again():
    recorded = {_lastart_key(EV, A)}
    for _ in range(100):                     # a hundred cycles, ~17 minutes
        assert _tokens_to_start([start(A)], EV, recorded, set()) == set()


def test_the_in_process_memory_alone_prevents_a_repeat():
    # The database write that records a launch FAILED after the push worked.
    # Without the backstop, the next cycle would launch that card again.
    in_process = {(EV, A)}
    assert _tokens_to_start([start(A)], EV, set(), in_process) == set()


def test_either_record_is_enough_to_stop_a_repeat():
    assert _tokens_to_start([start(A)], EV, {_lastart_key(EV, A)}, set()) == set()
    assert _tokens_to_start([start(A)], EV, set(), {(EV, A)}) == set()


# --- keyed by event, so tomorrow's round starts fresh -------------------------

def test_a_launch_for_one_event_does_not_suppress_the_next():
    # Launched for Columbus (29); Dignity Health (30) is a new race.
    recorded = {_lastart_key(29, A)}
    in_process = {(29, A)}
    assert _tokens_to_start([start(A)], 30, recorded, in_process) == {A}


def test_the_key_names_both_the_event_and_the_token():
    assert _lastart_key(29, A) == f"lastart:29:{A}"
    assert _lastart_key(29, A) != _lastart_key(30, A)
    assert _lastart_key(29, A) != _lastart_key(29, B)


# --- junk in the table must not raise inside the loop -------------------------

def test_malformed_rows_are_skipped_rather_than_raising():
    # An exception here would skip every push that cycle. Degrade, don't raise.
    rows = [None, {}, {"kind": "start"}, {"token": "", "kind": "start"},
            start(A)]
    assert _tokens_to_start(rows, EV, set(), set()) == {A}


def test_no_rows_at_all_is_no_launches():
    assert _tokens_to_start([], EV, set(), set()) == set()
    assert _tokens_to_start(None, EV, set(), set()) == set()
