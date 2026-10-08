"""A rider page's season line is counted in ROUNDS, from the championship
rows shown under it. Hunter Lawrence's 2026 read 18 "wins" (every main and
moto) above championships saying SX 5, MX 6, SMX 0.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _season_line  # noqa: E402

HUNTER = [
    {"series": "SX", "wins": 5, "podiums": 12, "round_finishes": [1, 2, 3, 1]},
    {"series": "MX", "wins": 6, "podiums": 9, "round_finishes": [1, 1, 4]},
    {"series": "SMX", "wins": 0, "podiums": 2, "round_finishes": [3, 2, 5]},
]


def test_wins_and_podiums_are_the_championships_added_up():
    line = _season_line(HUNTER, {"rounds": 31, "dnfs": 0}, 2026)
    assert (line["wins"], line["podiums"]) == (11, 23)
    assert line["year"] == 2026


def test_best_and_average_are_round_finishes():
    line = _season_line(HUNTER, {"rounds": 10, "dnfs": 1}, 2026)
    assert line["best_finish"] == 1
    assert line["avg_finish"] == 2.3        # 23 / 10


def test_a_row_without_finishes_blanks_best_and_average_rather_than_skew():
    rows = HUNTER[:2] + [dict(HUNTER[2], round_finishes=None)]
    line = _season_line(rows, {"rounds": 31, "dnfs": 0}, 2026)
    assert line["best_finish"] is None and line["avg_finish"] is None
    assert line["wins"] == 11


def test_no_championship_means_no_line():
    assert _season_line([], {"rounds": 3, "dnfs": 0}, 2026) is None


def test_rounds_are_championship_rounds_when_known():
    """Turner rode 7 events but the WMX title counted 6."""
    assert _season_line(HUNTER, {"rounds": 99, "dnfs": 0}, 2026)["rounds"] == 10
    rows = [dict(HUNTER[0], round_finishes=None)]
    assert _season_line(rows, {"rounds": 17, "dnfs": 0}, 2026)["rounds"] == 17
