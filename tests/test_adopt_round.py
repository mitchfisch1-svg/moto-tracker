"""The race-morning round-adoption tool — the parts that pick a target and an id.

    python -m pytest tests/ -q

`adopt_round.py` writes a results-site id onto an event row, and writing the
WRONG one publishes last month's race under this weekend's name. The decision
about whether the site has moved on lives in `_site_shows_this_round` and is
covered in test_race_state.py; what is tested here is everything around it —
which event gets targeted, which ids count as already-closed, and pulling a
Live Race Media id out of an asset path.

No database and no network: every case is a hand-built payload or row.
"""

import datetime
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load():
    """scripts/ isn't a package, so load the tool by path."""
    spec = importlib.util.spec_from_file_location(
        "adopt_round", ROOT / "scripts" / "adopt_round.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adopt = _load()

RESULTS = "https://results.supermotocross.com/results/"


def event(eid, *, status="scheduled", src=None, lrm=None, days=0, venue="V"):
    return {
        "id": eid,
        "round_label": f"R{eid}",
        "venue": venue,
        "status": status,
        "start_time_utc": datetime.datetime(2026, 9, 12, 18, 30,
                                            tzinfo=datetime.timezone.utc)
        + datetime.timedelta(days=days),
        "source_url": src,
        "lrm_id": lrm,
        "series": "SMX",
    }


# --- pulling the Live Race Media id out of an asset path ---------------------

def test_lrm_id_comes_out_of_a_track_map_path():
    payload = {"track_map": {
        "2d": "https://assets.liveracemedia.com/event_files/8888/999001/m2d.jpg",
        "3d": "https://assets.liveracemedia.com/event_files/8888/999001/m3d.jpg",
    }}
    assert adopt.lrm_from_payload(payload) == "8888"


def test_no_track_map_means_no_lrm_id():
    # The site posts maps late; the caller must fall back, not invent one.
    assert adopt.lrm_from_payload({"track_map": {}}) is None
    assert adopt.lrm_from_payload({}) is None
    assert adopt.lrm_from_payload(None) is None


def test_a_path_without_both_ids_is_not_an_lrm_id():
    # event_files/{lrm}/{smx}/ — a single-segment path is something else.
    payload = {"track_map": {"2d": "https://x/event_files/8888/logo.png"}}
    assert adopt.lrm_from_payload(payload) is None


# --- which ids count as already closed ---------------------------------------

def test_finished_ids_only_counts_closed_rounds_with_a_real_id():
    events = [
        event(28, status="final", src=f"{RESULTS}?p=view_event&id=516110"),
        event(27, status="final", src=f"{RESULTS}?p=view_event&id=515268"),
        # Still running — its id must NOT read as closed, or we would refuse to
        # re-adopt the round we are standing in.
        event(29, status="scheduled", src=f"{RESULTS}?p=view_event&id=999001"),
        # The generic schedule URL carries no round id at all.
        event(30, status="final", src="https://www.supermotocross.com/schedule/"),
        event(31, status="final", src=None),
    ]
    assert adopt.finished_ids(events) == {"516110", "515268"}


# --- which event gets the id --------------------------------------------------

def test_target_is_the_soonest_unfinished_round():
    events = [
        event(28, status="final", days=-14),
        event(29, days=0),
        event(30, days=7),
    ]
    assert adopt.pick_target(events, None)["id"] == 29


def test_a_closed_round_is_never_the_default_target():
    # Ironman is the most recent event in the table and must not be picked up
    # again just because it sorts last among finished rounds.
    events = [event(28, status="final", days=-14), event(29, days=0)]
    assert adopt.pick_target(events, None)["status"] != "final"


def test_an_explicit_event_id_wins():
    events = [event(29, days=0), event(30, days=7)]
    assert adopt.pick_target(events, 30)["id"] == 30


def test_an_unknown_event_id_is_refused_rather_than_guessed():
    assert adopt.pick_target([event(29)], 999) is None


def test_no_unfinished_rounds_means_no_target():
    assert adopt.pick_target([event(28, status="final")], None) is None


# --- the guard this whole tool exists to respect ------------------------------

@pytest.mark.parametrize("theirs,ours,done,expected", [
    # The normal pre-race state: the site still lists the round we closed.
    ("516110", None, {"516110"}, False),
    # Race morning: an id we have never ingested — this round is on track.
    ("999001", None, {"516110"}, True),
    # Already adopted; re-running must stay safe.
    ("999001", "999001", {"516110"}, True),
    # The site is serving something other than the round we already named.
    ("999002", "999001", {"516110"}, False),
    # Nothing derivable yet.
    (None, None, {"516110"}, False),
])
def test_adoption_is_gated_on_the_site_having_moved_on(theirs, ours, done,
                                                       expected):
    assert adopt._site_shows_this_round(ours, theirs, done) is expected
