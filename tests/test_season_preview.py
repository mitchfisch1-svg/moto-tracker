"""A rider's coming season, before it has a result: only what is known.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api import main  # noqa: E402

A1 = {"event_id": 1, "round_number": 1, "venue": "Angel Stadium", "city": "Anaheim",
      "state": "CA", "event_date": "2027-01-09", "start_time_utc": None}
FOX = dict(A1, venue="Fox Raceway", event_date="2027-05-29")


def _fake_query(rider_season=None):
    def q(sql, params=()):
        if "FROM events e JOIN seasons" in sql and "LIMIT 1" in sql and "SELECT 1" in sql:
            return [{"?column?": 1}] if params[0] == 2027 else []
        if "FROM rider_seasons" in sql:
            return [rider_season] if rider_season else []
        if "ORDER BY e.event_date, e.round_number" in sql:
            return [A1] if params[0] == "SX" else [FOX]
        raise AssertionError(sql)
    return q


def test_an_announced_move_and_where_his_season_starts(monkeypatch):
    monkeypatch.setattr(main, "query", _fake_query(
        {"team": "Team Tedder Husqvarna", "manufacturer": "Husqvarna",
         "number": None, "class": None, "source": "news"}))
    rows = [{"series": "SX", "year": 2026}, {"series": "SMX", "year": 2026}]
    p = main._rider_preview(5, {"retired_after": None}, rows, [2026])
    assert p["year"] == 2027 and p["team"] == "Team Tedder Husqvarna"
    assert p["source"] == "news" and not p["retired"]
    assert p["starts"]["venue"] == "Angel Stadium"


def test_nothing_announced_is_said_as_nothing_not_guessed(monkeypatch):
    monkeypatch.setattr(main, "query", _fake_query(None))
    p = main._rider_preview(5, {"retired_after": None},
                            [{"series": "MX", "year": 2026}], [2026])
    assert p["team"] is None and p["manufacturer"] is None and p["number"] is None
    assert p["starts"]["venue"] == "Fox Raceway"       # an MX-only rider starts in May


def test_a_retired_rider_has_no_season_to_start(monkeypatch):
    monkeypatch.setattr(main, "query", _fake_query(None))
    p = main._rider_preview(5, {"retired_after": 2026},
                            [{"series": "SX", "year": 2026}], [2026])
    assert p["retired"] and p["retired_after"] == 2026 and p["starts"] is None


def test_no_preview_without_a_calendar(monkeypatch):
    monkeypatch.setattr(main, "query", _fake_query(None))
    assert main._rider_preview(5, {"retired_after": None},
                               [{"series": "SX", "year": 2027}], [2027]) is None
