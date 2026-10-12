"""What the hourly news watcher counts as a rider move.

Deegan raced 250SX in 2026 and the 450 outdoors. A headline saying he moves
up to 450 Supercross has no signing verb and no new team, so the watcher
never saw it: only the entry list a week before A1 would have. These are the
headlines it must, and must not, act on.

    python -m pytest tests/ -q
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.pipeline.watch_moves import classify  # noqa: E402

TEAMS = {"star racing yamaha": "Monster Energy Star Racing Yamaha",
         "team tedder husqvarna": "Team Tedder Husqvarna"}


@pytest.mark.parametrize("title", [
    "Haiden Deegan moves up to 450SX for 2027",
    "Official: Haiden Deegan steps up to the 450 class full-time",
    "Deegan going 450 for 2027 Supercross",
    "Haiden Deegan graduates to the 450 class",
])
def test_a_step_up_to_450_is_a_class_move(title):
    assert classify(title, "", TEAMS)[0] == "class"


@pytest.mark.parametrize("title", [
    "Deegan moves up to 450 for World Supercross wildcard",   # one-off ride
    "Deegan takes first 450 class win",                       # a result
    "Haiden Deegan finally claims his first 450 victory",
])
def test_a_one_off_or_a_result_is_not_a_move(title):
    assert classify(title, "", TEAMS)[0] is None


@pytest.mark.parametrize("title", [
    "Deegan set to move up to 450SX in 2027",
    "Will Deegan race the 450 in Supercross?",
])
def test_talk_of_a_move_is_a_rumour(title):
    assert classify(title, "", TEAMS)[0] == "rumour"


def test_a_signing_still_names_its_team():
    assert classify("Cooper Webb Signs Multi-Year Deal with Team Tedder Husqvarna",
                    "", TEAMS) == ("signing", "Team Tedder Husqvarna")
