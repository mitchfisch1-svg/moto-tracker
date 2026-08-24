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


def _blk(primaries):
    """A block as it comes back out of the cache: rows, and no record of what
    the parser made of them."""
    return {"label": "450 Overall Results", "race_id": 1,
            "rows": _board(primaries)}


def test_the_half_round_is_not_a_result():
    assert not _overall_block_is_settled(_blk(HALF_450))


def test_the_finished_round_is():
    assert _overall_block_is_settled(_blk(FINAL_450))


def test_an_empty_board_is_not_a_result():
    """No rows means the Overall has not been posted, not that it is settled.
    all([]) is True, and that is exactly how you cache an empty round."""
    assert not _overall_is_settled([])
    assert not _overall_block_is_settled(_blk([]))


def test_one_missing_moto_deep_in_the_top_ten_still_blocks_it():
    """The lie does not have to be at the top. A board whose tenth row is a
    moto short is still a board taken before the race ended."""
    mixed = FINAL_450[:9] + ["10----"] + FINAL_450[10:]
    assert not _overall_block_is_settled(_blk(mixed))


def test_the_check_stops_at_the_top_ten():
    """Riders outside the top ten sit out motos all the time, and a round must
    not be re-scraped forever because someone in 30th packed up early. Nobody
    who misses a moto finishes the round in the top ten."""
    tail_short = FINAL_450[:_OVERALL_SETTLED_ROWS] + ["21----", "22----"]
    assert _overall_block_is_settled(_blk(tail_short))


def test_a_short_field_must_still_be_complete():
    """WMX ran 34 riders; a class could run fewer than ten. Every row there is
    has to be scored — a two-row board is not settled by running out of rows."""
    assert _overall_block_is_settled(_blk(["1-1", "2-2"]))
    assert not _overall_block_is_settled(_blk(["1-1", "2----"]))


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


# --- the event board ---------------------------------------------------------
# Budds Creek ran three classes, so /events/27 owes three Overalls. The site
# posts each class's link when that class's second moto is scored — and the
# last of those lands minutes after the event is already over.
_SMX = 515268
_LINKS = {"250": 1019488, "450": 1019512, "WMX": 1019066}


def _event_page(classes):
    return "".join(
        f'<a href="/results/?p=view_multi_main_result&amp;id={_LINKS[c]}">'
        f'{c} Overall Results</a>' for c in classes)


def _fetch(monkeypatch, posted, *, stored=None, status="final",
           expected_classes=3, half=()):
    """One /events/{id} Overall build. `posted` is the classes the site links;
    `half` is the subset whose second moto has not run."""
    from src.api import main

    main._SESSIONS_CACHE.clear()
    put = {}
    by_race = {_LINKS[c]: c for c in posted}

    def fake_results(race_id, p=None, **kw):
        cls = by_race[race_id]
        done = cls not in half
        return {"results": _board(FINAL_450 if done else HALF_450),
                "settled": done}

    monkeypatch.setattr(main.requests, "get",
                        lambda *a, **k: _Resp(_event_page(posted)))
    monkeypatch.setattr(main, "live_session_results", fake_results)
    monkeypatch.setattr(main, "_db_cache_get", lambda key: stored)
    monkeypatch.setattr(main, "_db_cache_put",
                        lambda key, payload: put.setdefault(key, payload))
    out = main._event_overall(
        f"https://results.supermotocross.com/results/?p=view_event&id={_SMX}",
        status, expected_classes)
    return out, put


def test_a_class_the_site_has_not_posted_yet_is_not_the_final_board(monkeypatch):
    """The 450's Overall link lands after the event is already final. Storing
    the board in that gap would lose the 450 — on the round that decides it."""
    out, put = _fetch(monkeypatch, ["250", "WMX"])
    assert [b["label"] for b in out] == ["250 Overall Results",
                                         "WMX Overall Results"]
    assert put == {}


def test_the_late_class_joins_the_board_instead_of_replacing_it(monkeypatch):
    """250 and WMX were already banked. When the 450 finally appears the board
    is all three — not the one class this scrape happened to see."""
    banked = [{"label": f"{c} Overall Results", "race_id": _LINKS[c],
               "rows": _board(FINAL_450), "settled": True} for c in ("250", "WMX")]
    out, put = _fetch(monkeypatch, ["450"], stored=banked)
    assert [b["label"] for b in out] == ["250 Overall Results",
                                         "450 Overall Results",
                                         "WMX Overall Results"]
    assert f"overall:{_SMX}" in put


def test_the_whole_board_is_stored_once_the_round_is_done(monkeypatch):
    out, put = _fetch(monkeypatch, ["250", "450", "WMX"])
    assert len(out) == 3
    assert len(put[f"overall:{_SMX}"]) == 3


def test_one_half_finished_class_holds_the_whole_board_back(monkeypatch):
    """This is what /events/27 served: two real Overalls and WMX a moto short,
    stored together as the round's result."""
    out, put = _fetch(monkeypatch, ["250", "450", "WMX"], half=("WMX",))
    assert len(out) == 3, "the moto-1 order should still be shown"
    assert put == {}


def test_nothing_is_stored_while_the_round_is_still_running(monkeypatch):
    out, put = _fetch(monkeypatch, ["250", "450", "WMX"], status="live")
    assert len(out) == 3
    assert put == {}


def test_a_complete_stored_board_is_served_without_scraping(monkeypatch):
    from src.api import main

    banked = [{"label": f"{c} Overall Results", "race_id": _LINKS[c],
               "rows": _board(FINAL_450), "settled": True} for c in ("250", "450", "WMX")]
    main._SESSIONS_CACHE.clear()
    monkeypatch.setattr(main, "_db_cache_get", lambda key: banked)

    def boom(*a, **k):
        raise AssertionError("re-scraped a board that was already complete")

    monkeypatch.setattr(main.requests, "get", boom)
    out = main._event_overall(
        f"https://results.supermotocross.com/results/?p=view_event&id={_SMX}",
        "final", 3)
    assert len(out) == 3


def test_the_stored_board_survives_the_site_going_down(monkeypatch):
    """Two classes banked, the third still missing, and the results site is
    unreachable. Show what we have rather than nothing."""
    from src.api import main

    banked = [{"label": f"{c} Overall Results", "race_id": _LINKS[c],
               "rows": _board(FINAL_450), "settled": True} for c in ("250", "WMX")]
    main._SESSIONS_CACHE.clear()
    monkeypatch.setattr(main, "_db_cache_get", lambda key: banked)

    def down(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(main.requests, "get", down)
    out = main._event_overall(
        f"https://results.supermotocross.com/results/?p=view_event&id={_SMX}",
        "final", 3)
    assert len(out) == 2


# --- SX Triple Crown ---------------------------------------------------------
# Three races, not two, and the round goes to the LOWEST total: Webb's 4-2-3
# for 9 beats Hunter's 7-1-2 for 10. Reading two moto columns and calling the
# third one the point total gave "4-2 · 3 pts" — a race finish sold as points,
# with a whole race missing. Anaheim 2, 2026-01-31, 450 Overall.
_TC_HEAD = "<tr><th>Pos</th><th>#</th><th>Bike</th><th>Rider</th>" \
           "<th>Moto 1</th><th>Moto 2</th><th>Moto 3</th>" \
           "<th>Total Points</th></tr>"
_TC_ROWS = [("1", "Cooper Webb", "4", "2", "3", "9"),
            ("2", "Hunter Lawrence", "7", "1", "2", "10"),
            ("3", "Ken Roczen", "1", "5", "4", "10")]


def _triple_crown_page(rows):
    body = "".join(
        f"<tr><td>{pos}</td><td>1</td><td>KTM</td><td>{name}</td>"
        f"<td>{a}</td><td>{b}</td><td>{c}</td><td>{tot}</td></tr>"
        for pos, name, a, b, c, tot in rows)
    return f"<html><table>{_TC_HEAD}{body}</table></html>"


def test_all_three_races_are_shown(monkeypatch):
    rows, _ = _scrape(monkeypatch, _triple_crown_page(_TC_ROWS), 493648)
    assert [r["primary"] for r in rows] == ["4-2-3", "7-1-2", "1-5-4"]


def test_the_total_is_read_from_its_own_column(monkeypatch):
    """With three races the total sits one column further right. Assuming its
    position turned Webb's third-race finish into his points."""
    rows, _ = _scrape(monkeypatch, _triple_crown_page(_TC_ROWS), 493648)
    assert [r["secondary"] for r in rows] == ["9 pts", "10 pts", "10 pts"]


def test_a_triple_crown_with_a_race_to_go_is_not_stored(monkeypatch):
    """Same trap as a motocross round, one race deeper in."""
    part = [(p, n, a, b, "---", "-") for p, n, a, b, _c, _t in _TC_ROWS]
    rows, put = _scrape(monkeypatch, _triple_crown_page(part), 493648)
    assert put == []
    assert rows[0]["primary"] == "4-2"
    assert rows[0]["primary_label"] == "MOTO 1 + MOTO 2"


def test_the_parsers_verdict_beats_the_rows():
    """Out of context "4-2" is a finished motocross round AND a Triple Crown
    with a race still to run. Only the parser saw how many moto columns there
    were, so a block carrying its verdict is judged on that, not re-read."""
    ambiguous = ["4-2", "7-1", "1-5"]
    assert _overall_block_is_settled(_blk(ambiguous))     # cached: rows only
    assert not _overall_block_is_settled(dict(_blk(ambiguous), settled=False))
    assert _overall_block_is_settled(dict(_blk(ambiguous), settled=True))
    assert _overall_block_is_settled(_blk(["4-2-3", "7-1-2", "1-5-4"]))
