"""The end-of-day card has to remember the whole day, not half of it.

A programme's last session is ONE class. A closing card built from it drops the
other one — you wait all afternoon and the lock screen shows 450 while the 250
result, which finished an hour earlier, is nowhere. So the day-complete card is
built from the day's results per class instead of from whatever raced last.

Six rows: top three of each championship class, in a fixed order so the card
looks the same at every round.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import (  # noqa: E402
    _LA_OVERALL_CLASSES, _la_overall_rows, _la_overall_state,
)


def _rider(pos, name, num, points=None):
    return {"position": pos, "name": name, "number": num, "points": points}


def _day():
    return {
        "250": [_rider(1, "Cole Davies", "37", 25),
                _rider(2, "Levi Kitchen", "47", 22),
                _rider(3, "Dylan Anstie", "61", 20),
                _rider(4, "Chance Hymas", "29", 18)],
        "450": [_rider(1, "Chase Sexton", "4", 25),
                _rider(2, "Justin Cooper", "32", 22),
                _rider(3, "Jorge Prado", "26", 20)],
    }


def test_three_from_each_class_makes_six_rows():
    rows = _la_overall_rows(_day())
    assert len(rows) == 6
    assert [r["n"] for r in rows] == [
        "Davies", "Kitchen", "Anstie", "Sexton", "Cooper", "Prado"]


def test_the_class_is_labelled_once_not_six_times():
    """Two labelled blocks, not the same word repeated down the card."""
    rows = _la_overall_rows(_day())
    assert [r["cls"] for r in rows] == ["250", "", "", "450", "", ""]


def test_the_order_of_classes_is_fixed_not_whatever_the_dict_says():
    """The card must look identical at every round."""
    backwards = {"450": _day()["450"], "250": _day()["250"]}
    rows = _la_overall_rows(backwards)
    assert [r["cls"] for r in rows][0] == _LA_OVERALL_CLASSES[0]
    assert rows[3]["cls"] == _LA_OVERALL_CLASSES[1]


def test_surnames_only_because_a_lock_screen_row_is_narrow():
    rows = _la_overall_rows(_day())
    assert rows[0]["n"] == "Davies"          # not "Cole Davies"


def test_points_are_shown_and_absent_points_leave_the_column_empty():
    """An overall board has no gap to show; points are what it means."""
    assert _la_overall_rows(_day())[0]["g"] == "25"
    no_pts = {"250": [_rider(1, "Cole Davies", "37", None)]}
    assert _la_overall_rows(no_pts)[0]["g"] == ""


def test_a_class_that_did_not_race_is_skipped_not_padded():
    """Three rows and a truth beats six rows and a guess."""
    rows = _la_overall_rows({"250": _day()["250"]})
    assert len(rows) == 3
    assert all(r["cls"] in ("250", "") for r in rows)


def test_no_results_at_all_means_no_card():
    """None tells the caller to fall back to the last race's order."""
    assert _la_overall_state({"venue": "Ironman"}, {}) is None
    assert _la_overall_state({"venue": "Ironman"}, None) is None


def test_the_card_names_the_round_and_carries_no_clock():
    state = _la_overall_state({"venue": "Historic Crew Stadium"}, _day())
    assert state["race"] == "Final results"
    assert state["venue"] == "Historic Crew Stadium"
    assert state["remaining"] is None      # a finished day has no countdown
    assert state["flag"] is None


def test_a_long_venue_cannot_overflow_the_card():
    state = _la_overall_state({"venue": "X" * 80}, _day())
    assert len(state["venue"]) <= 28


def test_positions_come_from_the_results_not_the_row_number():
    """A DSQ can leave a gap; render what the result says."""
    rows = _la_overall_rows({"250": [_rider(1, "Cole Davies", "37"),
                                     _rider(3, "Levi Kitchen", "47")]})
    assert [r["p"] for r in rows] == [1, 3]
