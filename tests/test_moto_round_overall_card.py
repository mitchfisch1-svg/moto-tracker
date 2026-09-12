"""The end-of-day card for a round that settles on MOTOS, not a main.

    python -m pytest tests/ -q

`_la_day_results` reads sessions typed `main`. SMX playoffs and MX both run two
motos a class, so it finds nothing for 250/450 and the card fell back to the
last race's order — on 09-12 that would have been "450 Moto 2 · final", one moto
of one class standing in for the whole day.

The fix reads the series' own published Overall. Nothing is computed: the site
publishes the combined table with each rider's two finishes and the points they
add up to, and adding motos up ourselves is what mis-scored championships all
season.

The trap is that a class's Overall is published as soon as moto 1 is scored,
with moto 2 as dashes, looking identical to the finished thing. Most of these
tests are about not showing that.

No database and no network.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import (  # noqa: E402
    _la_has_championship_classes,
    _la_overall_rows,
    _overall_blocks_to_by_class,
    _overall_points,
)


def row(pos, name, num, motos, pts):
    """A parsed Overall row: primary is "1-1", secondary is "50 pts"."""
    return {"position": pos, "name": name, "number": num,
            "primary": motos, "secondary": (f"{pts} pts" if pts else None)}


def block(label, rows, settled=None):
    b = {"label": label, "race_id": label, "rows": rows}
    if settled is not None:
        b["settled"] = settled
    return b


FINISHED_250 = block("250 Overall", [
    row(1, "Haiden Deegan", "1", "1-1", 50),
    row(2, "Jo Shimoda", "91", "2-3", 42),
    row(3, "Levi Kitchen", "47", "4-2", 40),
], settled=True)

FINISHED_450 = block("450 Overall", [
    row(1, "Jett Lawrence", "18", "1-1", 50),
    row(2, "Chase Sexton", "4", "2-2", 44),
    row(3, "Eli Tomac", "3", "3-4", 38),
], settled=True)


# --- reading the points off the board -----------------------------------------

def test_points_come_out_of_the_secondary_column():
    assert _overall_points(row(1, "A", "1", "1-1", 50)) == 50
    assert _overall_points({"secondary": "38 pts"}) == 38


def test_a_row_with_no_points_is_not_a_crash():
    for bad in ({}, None, {"secondary": None}, {"secondary": ""},
                {"secondary": "—"}, {"secondary": "pts"}):
        assert _overall_points(bad) is None


# --- the good case ------------------------------------------------------------

def test_two_settled_overalls_make_the_six_row_card():
    by_class = _overall_blocks_to_by_class([FINISHED_250, FINISHED_450])
    assert set(by_class) == {"250", "450"}
    rows = _la_overall_rows(by_class)
    assert len(rows) == 6
    # The class label sits on the first row of each block only.
    assert [r["cls"] for r in rows] == ["250", "", "", "450", "", ""]
    assert [r["n"] for r in rows][:3] == ["Deegan", "Shimoda", "Kitchen"]
    # Points are what the result MEANS in a playoff round.
    assert [r["g"] for r in rows][:3] == ["50", "42", "40"]


def test_the_classes_are_always_in_the_same_order():
    forwards = _overall_blocks_to_by_class([FINISHED_250, FINISHED_450])
    backwards = _overall_blocks_to_by_class([FINISHED_450, FINISHED_250])
    assert _la_overall_rows(forwards) == _la_overall_rows(backwards)


# --- the trap: a half-finished round ------------------------------------------

def test_an_unsettled_overall_is_refused():
    # Published after moto 1, moto 2 still dashes. Showing this as the day's
    # result is the exact failure the card was built to fix.
    half = block("250 Overall", [row(1, "Haiden Deegan", "1", "1--", 25)],
                 settled=False)
    assert _overall_blocks_to_by_class([half]) == {}


def test_one_settled_class_does_not_borrow_the_other():
    # The 450's Overall lands minutes after the last moto. Until it does, the
    # card shows three rows and a truth rather than six and a guess.
    by_class = _overall_blocks_to_by_class([FINISHED_250])
    assert set(by_class) == {"250"}
    assert len(_la_overall_rows(by_class)) == 3


def test_a_cached_board_is_judged_on_its_rows_when_it_has_no_verdict():
    # Boards read back out of the cache predate the parser's verdict, so the
    # moto pairs decide. "1--" is a race that has not happened.
    unfinished = block("250 Overall", [row(1, "Haiden Deegan", "1", "1--", 25)])
    finished = block("250 Overall", [row(1, "Haiden Deegan", "1", "1-1", 50)])
    assert _overall_blocks_to_by_class([unfinished]) == {}
    assert set(_overall_blocks_to_by_class([finished])) == {"250"}


# --- classes the card does not show -------------------------------------------

def test_support_classes_are_left_off():
    # WMX and SMX Next race, but the card is always 250 + 450 so it looks the
    # same at every round.
    wmx = block("WMX Overall", [row(1, "Lachlan Turner", "1", "1-1", 50)],
                settled=True)
    nxt = block("SMX Next Overall", [row(1, "Someone", "9", "1-1", 50)],
                settled=True)
    by_class = _overall_blocks_to_by_class([wmx, nxt, FINISHED_250])
    assert set(by_class) == {"250"}
    assert "WMX" not in by_class and "SMX Next" not in by_class


def test_an_unrecognised_label_is_skipped_not_guessed():
    weird = block("Something Else", [row(1, "A", "1", "1-1", 50)], settled=True)
    assert _overall_blocks_to_by_class([weird]) == {}


# --- degrade quietly ----------------------------------------------------------

def test_junk_never_raises_inside_the_loop():
    for junk in (None, [], [None], [{}], [{"label": "250 Overall"}],
                 [{"label": None, "rows": None}]):
        assert isinstance(_overall_blocks_to_by_class(junk), dict)


def test_rows_without_a_position_are_dropped():
    b = block("250 Overall", [row(None, "A", "1", "1-1", 50),
                              row(1, "B", "2", "1-1", 50)], settled=True)
    assert [r["name"] for r in _overall_blocks_to_by_class([b])["250"]] == ["B"]


# --- the switch that decides whether to go looking -----------------------------

def test_a_main_event_round_does_not_trigger_the_overall_path():
    assert _la_has_championship_classes({"250": [{"position": 1}]}) is True
    assert _la_has_championship_classes({"450": [{"position": 1}]}) is True


def test_a_moto_round_does_trigger_it():
    # What 09-12 actually produces: only the SMX Next MAIN is typed `main`.
    assert _la_has_championship_classes({"SMX Next": [{"position": 1}]}) is False
    assert _la_has_championship_classes({}) is False
    assert _la_has_championship_classes(None) is False
    assert _la_has_championship_classes({"250": []}) is False
