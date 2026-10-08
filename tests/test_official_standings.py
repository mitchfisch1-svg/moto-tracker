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
    for season, champs in CHAMPIONSHIPS.items():
        assert isinstance(season, int) and season >= 2026
        for abbrev, cls, sid in champs:
            assert abbrev in {"SX", "MX", "SMX"}
            assert cls in valid
            assert isinstance(sid, int)


def test_a_page_for_another_season_is_refused(monkeypatch):
    """2026's ids would serve 2026's tables in 2027. The heading is the only
    thing on the page that says which season it is, so it is checked."""
    from src.adapters import official_standings as o

    class R:
        text = "<h2>2026 SX 450 Championship</h2>" + PAGE
        def raise_for_status(self): pass
    monkeypatch.setattr(o.requests, "get", lambda *a, **k: R())
    assert o.fetch_standings(16, 1, season=2026)
    assert o.fetch_standings(16, 1, season=2027) == []


# Round cells as the provider really draws them: the round's points and its
# OVERALL finish, then a nested table of moto lines. Hunter Lawrence won 12
# motos and 6 rounds in 2026; the championship counts rounds.
def _cell(points, finish, motos):
    lines = "".join(f"<tr><td>{p} {f}</td></tr>" for p, f in motos)
    return f"<td>{points} {finish}<table>{lines}</table></td>"


ROUND_PAGE = (
    "<table><tr><th></th><th>#</th><th>BIKE</th><th>RIDER</th><th>POINTS</th>"
    "<th>POINT ADJUSTMENTS</th><th>1: FOX RACEWAY</th><th>2: HANGTOWN</th>"
    "<th>3: THUNDER VALLEY</th></tr>"
    "<tr><td>1</td><td>96</td><td>HON</td><td>Hunter Lawrence</td><td>140</td><td>0</td>"
    + _cell(50, "1st", [(25, "1st"), (25, "1st")])
    + _cell(47, "1st", [(25, "1st"), (22, "2nd")])
    + _cell(43, "3rd", [(25, "1st"), (18, "4th")])
    + "</tr>"
    "<tr><td>2</td><td>38</td><td>YAM</td><td>Haiden Deegan</td><td>120</td><td>0</td>"
    + _cell(42, "2nd", [(22, "2nd"), (20, "3rd")])
    + _cell(36, "4th", [(18, "4th"), (18, "4th")])
    + "<td></td></tr></table>"
)


def test_wins_and_podiums_are_round_finishes_not_motos():
    rows = parse_series_points(ROUND_PAGE)
    assert [(r["rider"], r["wins"], r["podiums"]) for r in rows] == [
        ("Hunter Lawrence", 2, 3),     # 4 moto wins, but 2 round wins
        ("Haiden Deegan", 0, 1),       # missed round 3: an empty cell
    ]


def test_a_page_without_round_columns_has_no_wins():
    """None, not 0: an overlay must not zero a count it was never told."""
    assert parse_series_points(PAGE)[0]["wins"] is None
