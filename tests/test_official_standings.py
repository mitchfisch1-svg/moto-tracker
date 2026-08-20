"""Parsing the series' own standings page.

We compute points from results, but the series also applies manual penalties we
can never derive, so the published table is the authority. That makes this
parser load-bearing: read the wrong column and the app confidently publishes a
championship that never happened.

Fixtures below are trimmed from the real page. No network — CI has none.

    python -m pytest tests/ -q
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.adapters.official_standings import (  # noqa: E402
    CHAMPIONSHIPS,
    match_key,
    parse_series_points,
)

# The real shape: a blank leading column for position, then # / BIKE / RIDER /
# POINTS / POINT ADJUSTMENTS, then one column per round.
PAGE = """
<table>
  <tr><th></th><th>#</th><th>BIKE</th><th>RIDER</th><th>POINTS</th>
      <th>POINT ADJUSTMENTS</th><th>1: FOX RACEWAY</th></tr>
  <tr><td>1</td><td>18</td><td>HON</td><td>Jett Lawrence</td><td>402</td>
      <td>0</td><td>47</td></tr>
  <tr><td>2</td><td>96</td><td>HON</td><td>Hunter Lawrence</td><td>392</td>
      <td>0</td><td>25</td></tr>
  <tr><td>3</td><td>38</td><td>YAM</td><td>Haiden Deegan</td><td>336</td>
      <td>0</td><td>38</td></tr>
  <tr><td>4</td><td>24</td><td>HUS</td><td>R.J. Hampshire</td><td>282</td>
      <td>-5</td><td>30</td></tr>
</table>
"""


def test_it_reads_rider_and_points():
    rows = parse_series_points(PAGE)
    assert len(rows) == 4
    assert rows[0]["rider"] == "Jett Lawrence"
    assert rows[0]["points"] == 402
    assert rows[0]["position"] == 1


def test_it_reads_the_points_column_not_the_first_round():
    """Both are integers on the same row. Taking 'the last number' or a fixed
    index reads a single round's score as a season total — which is exactly the
    mistake that makes a parser look like it works."""
    rows = parse_series_points(PAGE)
    assert [r["points"] for r in rows] == [402, 392, 336, 282]


def test_it_carries_the_point_adjustment():
    """The whole reason for reading this page instead of computing."""
    assert parse_series_points(PAGE)[3]["adjustment"] == -5
    assert parse_series_points(PAGE)[0]["adjustment"] == 0


def test_columns_are_found_by_header_not_position():
    """Same data, extra leading column. A fixed index would now be off by one."""
    shifted = PAGE.replace("<th></th><th>#</th>", "<th></th><th>SEED</th><th>#</th>")
    shifted = shifted.replace("<td>1</td><td>18</td>", "<td>1</td><td>x</td><td>18</td>")
    rows = parse_series_points(shifted)
    assert rows[0]["rider"] == "Jett Lawrence" and rows[0]["points"] == 402


def test_rows_without_a_number_are_skipped():
    """Section headers and spacers ride along in the same table."""
    junk = PAGE.replace("</table>",
                        "<tr><td></td><td></td><td></td><td>250 CLASS</td>"
                        "<td></td><td></td><td></td></tr></table>")
    assert len(parse_series_points(junk)) == 4


def test_a_page_with_no_standings_table_yields_nothing():
    assert parse_series_points("<html><table><tr><td>hi</td></tr></table></html>") == []
    assert parse_series_points("") == []


# --- matching their names to ours --------------------------------------------

@pytest.mark.parametrize("theirs,ours", [
    ("R.J. Hampshire", "R J Hampshire"),     # the real disagreement
    ("Cornelius Tøndel", "Cornelius Tondel"),  # accent lost on ingest
    ("JETT LAWRENCE", "Jett Lawrence"),
    ("Jo  Shimoda", "Jo Shimoda"),
])
def test_the_same_rider_matches_across_spellings(theirs, ours):
    assert match_key(theirs) == match_key(ours)


def test_different_riders_do_not_collide():
    assert match_key("Jett Lawrence") != match_key("Hunter Lawrence")
    assert match_key("Lucas Coenen") != match_key("Sacha Coenen")


def test_every_championship_maps_to_a_class_we_store():
    """A typo here would silently update nothing at all."""
    valid = {"450", "250", "250 East", "250 West", "WMX"}
    for abbrev, cls, sid in CHAMPIONSHIPS:
        assert abbrev in {"SX", "MX", "SMX"}
        assert cls in valid
        assert isinstance(sid, int)
