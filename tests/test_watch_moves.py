"""What the hourly news watcher counts as a rider move.

Deegan raced 250SX in 2026 and the 450 outdoors. A headline saying he moves
up to 450 Supercross has no signing verb and no new team, so the watcher
never saw it: only the entry list a week before A1 would have. These are the
headlines it must, and must not, act on.

    python -m pytest tests/ -q
"""

import datetime
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.pipeline.watch_moves import classify, record_left, scan  # noqa: E402

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


def test_a_past_title_is_not_next_seasons_class():
    from src.pipeline.watch_moves import class_named
    assert class_named("Colt Nichols Joins ROKiT Partzilla PRMX for 2027",
                       "the team announced the signing of former 250SX East Champion "
                       "Colt Nichols") is None
    assert class_named("Official: Quad Lock Honda sign Michael Mosiman for 2027",
                       "will join the team's 250 program for the 2027 season") == "250"
    assert class_named("Beta Signs Shane McElrath to Two-Year 450SX Supercross Contract", "") == "450"
    # two classes named: not for us to pick
    assert class_named("Team X signs Y", "a 450 veteran joins the 250 program") is None


def test_the_headlines_team_beats_the_one_he_is_leaving():
    teams = {"quad lock honda": "Quad Lock Honda",
             "troy lee designs/red bull ducati": "TLD Ducati"}
    assert classify("Official: Joey Savatgy Signs Multi-Year Deal with Troy Lee Designs/Red Bull Ducati",
                    "after three seasons at Quad Lock Honda", teams) == ("signing", "TLD Ducati")


class FakeConn:
    """Answers scan's and record_left's queries by what they ask for."""

    def __init__(self, articles=(), later=()):
        self.answers = {
            "SELECT DISTINCT team": [("Monster Energy Kawasaki",), ("Quad Lock Honda",),
                                     ("Red Bull KTM Factory Racing",)],
            "WHERE left_team IS NOT NULL": [(96, "Monster Energy Kawasaki", "Monster Energy Kawasaki")],
            "JOIN rider_seasons rs": [(96, "Chase Sexton", "450"), (43, "Jorge Prado", "450")],
            "FROM news_articles": list(articles),
            "SELECT rider_id FROM rider_seasons": [],
            "SELECT year, team FROM rider_seasons": list(later),
        }
        self.row = None
        self.wrote = False
        self.commit = lambda: None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.wrote = self.wrote or sql.lstrip().startswith("UPDATE")
        self.row = next((v for k, v in self.answers.items() if k in sql), [("Chase Sexton", "X")])

    def fetchall(self):
        return self.row

    def fetchone(self):
        return self.row[0]


def two_outlets(title, summary=""):
    """The same story from two outlets: enough for the watcher to act alone."""
    return [(1, 1, title, summary, "u1"), (2, 2, title, summary, "u2")]


def scan_of(*articles):
    return scan(FakeConn(articles), today=datetime.date(2026, 11, 3), dry_run=True)


def test_a_rider_who_left_is_never_filed_back_under_that_team():
    applied, decided = scan_of(*two_outlets("Chase Sexton Signs with KTM",
                                            "He left Monster Energy Kawasaki in October."))
    assert applied == []
    assert {(d[4], d[6]) for d in decided} == {(None, "pending")}


def test_his_real_signing_lands_even_when_the_headline_names_the_old_team():
    applied, _ = scan_of(*two_outlets("Chase Sexton Leaves Monster Energy Kawasaki, Joins Quad Lock Honda"))
    assert [(a[0], a[2]) for a in applied] == [(96, "Quad Lock Honda")]


def test_another_riders_news_does_not_sign_a_free_agent():
    applied, _ = scan_of(*two_outlets("Jorge Prado Joins Red Bull KTM Factory Racing After Chase Sexton Talks End"))
    assert [(a[0], a[2]) for a in applied] == [(43, "Red Bull KTM Factory Racing")]


def test_leaving_is_refused_when_a_later_season_is_already_recorded():
    conn = FakeConn(later=[(2027, "Monster Energy Kawasaki")])
    with pytest.raises(ValueError):
        record_left(conn, 104, datetime.date(2026, 11, 3), "url")
    assert not conn.wrote


def test_leaving_is_recorded_when_there_is_no_later_season():
    conn = FakeConn()
    assert record_left(conn, 96, datetime.date(2026, 10, 23), "url")
    assert conn.wrote
