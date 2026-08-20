"""Championship points — the table that decides every standing in the app.

Wrong scoring is the quietest kind of wrong: the leader is still the leader and
the page still looks plausible, so nothing announces the error. The app carried
the classic AMA table (25-22-20-18-16-15-...-1, top 20) for months. The real one
pays 17 for 5th and keeps paying down to 21st. After nine rounds that put the
championship leader 2 points light and the midfield 16 light, and it had 6th and
7th in the wrong order.

Every expected value below is from PUBLISHED official results, not from the
implementation — a test written off the code would have agreed with the bug.

    python -m pytest tests/ -q
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.standings import points_for  # noqa: E402


# --- the shape of the table --------------------------------------------------

@pytest.mark.parametrize("pos,pts", [(1, 25), (2, 22), (3, 20), (4, 18)])
def test_the_front_four_are_fixed(pos, pts):
    assert points_for("MX", pos) == pts


def test_fifth_pays_seventeen_not_sixteen():
    """The single value that exposed the bug. Everything behind it was short."""
    assert points_for("MX", 5) == 17


@pytest.mark.parametrize("pos", range(5, 22))
def test_from_fifth_back_you_score_twenty_two_minus_your_position(pos):
    assert points_for("MX", pos) == 22 - pos


def test_twenty_first_still_scores_a_point():
    """The old table stopped at 20th, so it silently binned 21st place."""
    assert points_for("MX", 21) == 1


@pytest.mark.parametrize("pos", [22, 23, 30, 40])
def test_twenty_second_and_back_score_nothing(pos):
    assert points_for("MX", pos) == 0


def test_no_position_scores_nothing():
    """A DNS/DNF row arrives with no position at all."""
    assert points_for("MX", None) == 0
    assert points_for("MX", 0) == 0


@pytest.mark.parametrize("series", ["SX", "MX", "SMX"])
def test_every_series_scores_the_same(series):
    """Checked against the official SX 450 and Pro Motocross standings both —
    if a series ever diverges, this is the test that should fail first."""
    assert points_for(series, 5) == 17
    assert points_for(series, 21) == 1


def test_an_unknown_series_still_scores_sanely():
    assert points_for("WSX", 1) == 25


# --- real rounds, real published totals --------------------------------------
# Unadilla 2026, 450 Overall (?p=view_multi_main_result&id=1017646).

@pytest.mark.parametrize("m1,m2,official", [
    (2, 1, 47),     # Jett Lawrence
    (3, 2, 42),     # Jorge Prado
    (4, 3, 38),     # Haiden Deegan
    (5, 4, 35),     # Garrett Marchbanks — the old table said 34
    (6, 8, 30),     # R.J. Hampshire   — old table: 28
    (9, 6, 29),     # Dylan Ferrandis  — old table: 27
    (1, 39, 25),    # Hunter Lawrence — won one, then a DNF-grade finish
    (21, 5, 18),    # Mark Fineis — proves 21st is worth exactly 1
    (20, 20, 4),    # Stephen Rubini — proves 20th is worth 2
    (24, 15, 7),    # Lorenzo Locurcio — proves 24th is worth 0
    (23, 16, 6),    # Malcolm Stewart  — proves 23rd is worth 0
])
def test_a_real_round_adds_up_to_the_published_total(m1, m2, official):
    assert points_for("MX", m1) + points_for("MX", m2) == official
