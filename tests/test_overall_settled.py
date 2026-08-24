"""Refusing to file half a round as the round's result.

/events/27 served Budds Creek's WMX Overall as "Lachlan Turner 1---- 25 pts" —
one moto, dressed as the round. Fetching it again returned "1-1 50 pts". The
Overall had been scraped on the Friday, between WMX's two motos, and pinned:
`overall:{smx}` and `view_multi_main_result:{race_id}` have no expiry, so the
board that happened to be on the page at that moment became the board forever.
Unadilla was worse — BOTH the 250 and the 450 were stuck a moto short, and the
450 is the one carrying a one-point title fight.

The site publishes the Overall the moment moto 1 is scored and dashes the
moto-2 column, so the half-result and the result are the same table with the
same authority. Only the digits tell them apart.

Every row below is real, from those cached boards.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import (  # noqa: E402
    _overall_is_settled,
    _overall_block_is_settled,
    _OVERALL_SETTLED_ROWS,
)

# Budds Creek 450 Overall, both motos in — the real thing.
FINAL_450 = ["1-1", "3-3", "2-4", "5-2", "8-5", "6-9", "10-7", "7-10", "4-13",
             "12-6", "9-11", "14-8"]
# Unadilla 450 as it was cached: moto 1 only, moto 2 still dashes.
HALF_450 = ["1----", "2----", "3----", "4----", "5----", "6----", "7----",
            "8----", "9----", "10----", "11----", "12----"]


def _board(primaries):
    return [{"position": i, "primary": p} for i, p in enumerate(primaries, 1)]


def test_the_half_round_is_not_a_result():
    assert not _overall_block_is_settled(_board(HALF_450))


def test_the_finished_round_is():
    assert _overall_block_is_settled(_board(FINAL_450))


def test_an_empty_board_is_not_a_result():
    """No rows means the Overall has not been posted, not that it is settled.
    all([]) is True, and that is exactly how you cache an empty round."""
    assert not _overall_is_settled([])
    assert not _overall_block_is_settled([])


def test_one_missing_moto_deep_in_the_top_ten_still_blocks_it():
    """The lie does not have to be at the top. A board whose tenth row is a
    moto short is still a board taken before the race ended."""
    mixed = FINAL_450[:9] + ["10----"] + FINAL_450[10:]
    assert not _overall_block_is_settled(_board(mixed))


def test_the_check_stops_at_the_top_ten():
    """Riders outside the top ten sit out motos all the time, and a round must
    not be re-scraped forever because someone in 30th packed up early. Nobody
    who misses a moto finishes the round in the top ten."""
    tail_short = FINAL_450[:_OVERALL_SETTLED_ROWS] + ["21----", "22----"]
    assert _overall_block_is_settled(_board(tail_short))


def test_a_short_field_must_still_be_complete():
    """WMX ran 34 riders; a class could run fewer than ten. Every row there is
    has to be scored — a two-row board is not settled by running out of rows."""
    assert _overall_block_is_settled(_board(["1-1", "2-2"]))
    assert not _overall_block_is_settled(_board(["1-1", "2----"]))


# --- the parse, end to end ---------------------------------------------------
# Reduced from the results site's own markup: POS/#/BIKE/RIDER, then the two
# moto columns and the round total.
_HEAD = "<tr><th>Pos</th><th>#</th><th>Bike</th><th>Rider</th>" \
        "<th>Moto 1</th><th>Moto 2</th><th>Total</th></tr>"


def _page(primaries, points):
    rows = ""
    for i, (p, pts) in enumerate(zip(primaries, points), 1):
        m1, m2 = p.split("-", 1)
        rows += (f"<tr><td>{i}</td><td>{i}</td><td>Honda</td>"
                 f"<td>Rider {i}</td><td>{m1}</td><td>{m2}</td>"
                 f"<td>{pts}</td></tr>")
    return f"<html><table>{_HEAD}{rows}</table></html>"


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _scrape(monkeypatch, html, race_id):
    """Run one Overall page through the parser with no network and no DB."""
    from src.api import main

    main._SESSIONS_CACHE.clear()
    put = []
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: _Resp(html))
    monkeypatch.setattr(main, "_db_cache_get", lambda key: None)
    monkeypatch.setattr(main, "_db_cache_put",
                        lambda key, payload: put.append(key))
    payload = main.live_session_results(race_id, p="view_multi_main_result")
    return payload["results"], put


def test_the_half_round_is_never_written_to_the_db(monkeypatch):
    """This is the bug. The board is fine to show — moto 1 really did run —
    but writing it to a cache with no expiry is what made it permanent."""
    html = _page(HALF_450, ["25", "22", "20", "18", "17", "16", "15", "14",
                            "13", "12", "11", "10"])
    rows, put = _scrape(monkeypatch, html, 1017646)
    assert rows, "the moto-1 order should still be served"
    assert put == [], f"a half-finished Overall was persisted: {put}"


def test_the_finished_round_is_written(monkeypatch):
    html = _page(FINAL_450, ["50", "40", "40", "39", "31", "29", "27", "27",
                             "27", "26", "24", "22"])
    rows, put = _scrape(monkeypatch, html, 1019512)
    assert put == ["view_multi_main_result:1019512"]
    assert rows[0]["primary"] == "1-1"
    assert rows[0]["primary_label"] == "MOTOS"


def test_the_unrun_moto_stops_printing_as_dashes(monkeypatch):
    """"1----" was Hunter Lawrence winning moto 1 at Unadilla, rendered as
    though the dashes were part of his score. Say which moto it is instead."""
    html = _page(HALF_450, ["25"] * len(HALF_450))
    rows, _ = _scrape(monkeypatch, html, 1017646)
    assert rows[0]["primary"] == "1"
    assert rows[0]["primary_label"] == "MOTO 1"
    assert not any("--" in (r["primary"] or "") for r in rows)


def test_the_half_round_is_not_held_for_six_hours(monkeypatch):
    """It is held in memory so race-day taps do not re-scrape, but only for
    minutes — the finished board has to be able to replace it on its own."""
    from src.api import main

    html = _page(HALF_450, ["25"] * len(HALF_450))
    _scrape(monkeypatch, html, 1017646)
    expires, _ = main._SESSIONS_CACHE[("view_multi_main_result", 1017646)]
    assert expires - main.time.time() <= main._OVERALL_PROVISIONAL_TTL
    assert main._OVERALL_PROVISIONAL_TTL < main._SESSION_RESULT_TTL
