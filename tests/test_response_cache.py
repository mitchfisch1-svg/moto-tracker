"""Read answers are kept in memory so readers don't wake the database.

Neon's free plan allows 100 compute-hours a month, and every query keeps the
database awake for 5 minutes; it ran out on 07-31 and the app went dark.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.api import main  # noqa: E402


def _client(monkeypatch, near):
    """A client whose database is a counter: every query is a database wake."""
    calls = []
    main._RESP_CACHE.clear()
    monkeypatch.setattr(main, "_race_weekend_near", lambda now=None: near)
    monkeypatch.setattr(main, "query",
                        lambda sql, params=(): calls.append(sql) or [{"abbrev": "SX"}])
    return TestClient(main.app), calls


def test_a_second_read_is_answered_from_memory(monkeypatch):
    c, calls = _client(monkeypatch, near=False)
    a, b = c.get("/series"), c.get("/series")
    assert a.json() == b.json() == [{"abbrev": "SX"}]
    assert a.headers["x-cache"] == "miss" and b.headers["x-cache"] == "hit"
    assert len(calls) == 1


def test_between_race_weekends_everything_expires_together_after_the_hour():
    t = 1_800_000_000.0 + 1234          # any moment
    main._busy[:] = [t + 10_000, False]
    exp = main._cache_expiry(t)
    assert exp % 3600 == 600 and 0 < exp - t <= 3600 + 600


def test_on_a_race_weekend_answers_live_a_minute():
    t = 1_800_000_000.0
    main._busy[:] = [t + 10_000, True]
    assert main._cache_expiry(t) == t + main._RACE_WEEKEND_TTL


def test_live_timing_and_health_are_never_cached():
    for path in ("/live", "/live/sessions", "/live/sessions/5", "/health", "/debug/mock-race"):
        assert not main._CACHEABLE.match(path)
    for path in ("/standings", "/riders/41", "/season-lineup", "/widget/next", "/events/31"):
        assert main._CACHEABLE.match(path)


def test_query_order_does_not_split_the_cache():
    class R:
        def __init__(self, items):
            self.url = type("U", (), {"path": "/standings"})()
            self.query_params = type("Q", (), {"multi_items": lambda s: items})()
    assert main._cache_key(R([("series", "SX"), ("class", "450")])) == \
        main._cache_key(R([("class", "450"), ("series", "SX")]))
