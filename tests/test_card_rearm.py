"""Clearing a card must RE-ARM the phone, not spend its only launch.

    python -m pytest tests/ -q

Columbus, 09-12. Push-to-start fires once per phone per event. The loop also
clears cards during a session that does not earn a lock screen — qualifying,
the wildcards. Those two shared one ledger, and it only ran one way: clearing a
card during the morning spent that phone's single launch for the whole event.
When the motos finally started nothing relaunched, and the cards people already
had sat orphaned — frozen on "on the gate" — for the rest of the day. Every
screenshot from that day is a staged frame.

`/health` said pushes climbing, failed 0, tokens 38 the entire time, because
start tokens outnumber update tokens forty to one and Apple accepts pushes to
activities that no longer exist.

No database and no network: this exercises the ledger arithmetic that decides
who gets launched.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _lastart_key, _tokens_to_start  # noqa: E402

EV = 29
PHONE = "a" * 160
OTHER = "b" * 160


def start(tok):
    return {"token": tok, "kind": "start"}


def test_the_columbus_failure_in_one_test():
    """Morning clear, then the motos. The phone must get a card."""
    rows = [start(PHONE), start(OTHER)]

    # 14:32 — the first points race grids up, both phones launch.
    launched = _tokens_to_start(rows, EV, set(), set())
    assert launched == {PHONE, OTHER}
    recorded = {_lastart_key(EV, t) for t in launched}
    in_process = {(EV, t) for t in launched}

    # Mid-afternoon the feed shows a non-points session and the loop clears the
    # cards. THE OLD BEHAVIOUR: records kept, so nothing could ever relaunch.
    assert _tokens_to_start(rows, EV, recorded, in_process) == set()

    # THE FIX: clearing tears up the records for this event.
    recorded = {k for k in recorded if not k.startswith(f"lastart:{EV}:")}
    in_process = {p for p in in_process if p[0] != EV}

    # The next moto grids up — and both phones get a card again.
    assert _tokens_to_start(rows, EV, recorded, in_process) == {PHONE, OTHER}


def test_re_arming_is_scoped_to_the_event():
    # Tearing up Columbus's records must not relaunch a different round.
    recorded = {_lastart_key(29, PHONE), _lastart_key(30, PHONE)}
    in_process = {(29, PHONE), (30, PHONE)}
    recorded = {k for k in recorded if not k.startswith("lastart:29:")}
    in_process = {p for p in in_process if p[0] != 29}
    assert _tokens_to_start([start(PHONE)], 29, recorded, in_process) == {PHONE}
    assert _tokens_to_start([start(PHONE)], 30, recorded, in_process) == set()


def test_a_phone_is_still_launched_only_once_per_racing_block():
    # Re-arming must not turn into a card every ten seconds while racing.
    rows = [start(PHONE)]
    launched = _tokens_to_start(rows, EV, set(), set())
    recorded = {_lastart_key(EV, t) for t in launched}
    in_process = {(EV, t) for t in launched}
    for _ in range(200):            # ~30 minutes of cycles
        assert _tokens_to_start(rows, EV, recorded, in_process) == set()


def test_a_phone_that_arrives_after_the_clear_is_launched_with_the_rest():
    # Someone installs during qualifying; the clear has already happened.
    recorded, in_process = set(), set()
    assert _tokens_to_start([start(PHONE)], EV, recorded, in_process) == {PHONE}
