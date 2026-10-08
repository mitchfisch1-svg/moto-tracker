"""Finding a season's championship tables by their headings.

Every heading below is real: the provider's series-points pages, ids 4-32,
read on 2026-10-08. 2026's are the ids a person checked by hand (SEED), so
classifying them must reproduce SEED exactly — that is the test that the
classifier can be trusted with 2027's, which nobody will check by hand.

    python -m pytest tests/ -q
"""

import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import series_tables as st  # noqa: E402

REAL = {
    4: "2025 Pro Motocross 450 Championship",
    5: "2025 Pro Motocross 250 Championship",
    6: "2025 Pro Motocross Manufacturers Points",
    7: "2025 SMX 250 Championship",
    8: "2025 SMX 450 Championship",
    9: "2025 SMX 250 Championship Playoffs",
    10: "2025 SMX 450 Championship Playoffs",
    12: "2025 SMX Manufacturers Points",
    13: "2025 SMX Next Championship",
    14: "2026 SX 250 West Championship",
    15: "2026 SX 250 East Championship",
    16: "2026 SX 450 Championship",
    17: "2026 SX Manufacturers Championship",
    18: "2026 SMX 250 Combined Championship",
    19: "2026 SMX 450 Combined Championship",
    21: "2026 Pro Motocross 450 Championship",
    22: "2026 Pro Motocross 250 Championship",
    23: "2026 Pro Motocross Manufacturers Points",
    24: "2026 SMX Manufacturers Championship",
    25: "2026 WMX Motocross Championship",
    30: "2026 SMX Playoffs 450 Championship",
    31: "2026 SMX Playoffs 250 Championship",
    32: "2026 SMX Next Playoffs Championship",
}


def _filed(year):
    data = {}
    for i, t in REAL.items():
        st.file_table(data, i, t, 10)
    return data["tables"].get(str(year), {}), data


def test_2026s_headings_reproduce_the_hand_checked_ids():
    filed, data = _filed(2026)
    assert filed == st.SEED[2026]
    assert not data.get("conflicts")


def test_2025s_older_wording_files_correctly():
    """2025 says "Championship Playoffs" and "Manufacturers Points"."""
    filed, _ = _filed(2025)
    assert filed == {"MX 450": 4, "MX 250": 5, "MX MFR": 6, "SMX 250": 7,
                     "SMX 450": 8, "SMX PLAYOFF 250": 9, "SMX PLAYOFF 450": 10,
                     "SMX MFR": 12}


def test_smx_next_is_never_filed():
    assert st.classify("2026 SMX Next Playoffs Championship") is None
    assert st.classify("2025 SMX Next Championship") is None


def test_a_250_table_naming_no_region_is_left_alone():
    """Misfiling East as West puts one championship's points on the other's
    riders; leaving it unfiled only costs a computed number for a while."""
    assert st.classify("2027 SX 250 Championship") is None
    assert st.classify("2027 SX 250 East/West Showdown") is None


def test_wording_drift_still_files():
    assert st.classify("2027 Supercross 450 Championship") == (2027, "SX 450")
    assert st.classify("2027 Monster Energy Supercross 250 West Championship") \
        == (2027, "SX 250 West")
    assert st.classify("2027 SMX Playoff 250 Championship") == (2027, "SMX PLAYOFF 250")
    assert st.classify("2027 Supercross Manufacturers Championship") == (2027, "SX MFR")
    assert st.classify("") is None
    assert st.classify("404 Not Found") is None


def test_two_ids_for_one_championship_are_flagged_not_guessed():
    data = {}
    st.file_table(data, 33, "2027 SX 450 Championship", 40)
    assert st.file_table(data, 40, "2027 SX 450 Championship", 40) is None
    assert data["tables"]["2027"]["SX 450"] == 33
    assert data["conflicts"] == [{"year": 2027, "key": "SX 450", "ids": [33, 40]}]


def test_seed_beats_discovery():
    found = {"tables": {"2026": {"SX 450": 99, "SX 999": 5}}}
    assert st.table_id(2026, "SX 450", found) == 16
    assert st.table_id(2026, "SX 999", found) == 5


def test_lookups_for_a_discovered_season():
    found = {"tables": {"2027": {"SX 450": 33, "SX 250 West": 34,
                                 "SMX PLAYOFF 450": 50}}}
    assert st.championships(2027, found) == [("SX", "450", 33),
                                             ("SX", "250 West", 34)]
    assert st.smx_playoff_ids(2027, found) == {"450": 50}
    assert st.smx_playoff_ids(2027, {}) == {}
    assert 2027 in st.years(found) and 2026 in st.years(found)


# --- refresh(), against a fake provider and a fake database -------------------
class _Cur:
    def __init__(self, db):
        self.db, self.rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        if "FROM scraped_session_cache" in sql:
            v = self.db["cache"].get(params[0])
            self.rows = [(v,)] if v is not None else []
        elif "INSERT INTO scraped_session_cache" in sql:
            self.db["cache"][params[0]] = json.loads(params[1])
        elif "FROM events" in sql:
            self.rows = [(s,) for s in self.db["started"] if s != "WMX"]
        elif "FROM sessions" in sql:
            self.rows = [(1,)] if "WMX" in self.db["started"] else []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, started):
        self.db = {"cache": {}, "started": started}

    def cursor(self):
        return _Cur(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass


def _provider(pages, fail=()):
    calls = []

    def fetch(i):
        calls.append(i)
        if i in fail:
            raise TimeoutError(f"id {i} timed out")
        return (pages[i], 30) if i in pages else ("", 0)
    return fetch, calls


JAN = datetime.date(2027, 1, 10)


def test_a_new_season_is_found_just_above_the_last_one():
    conn = _Conn({"SX"})
    fetch, calls = _provider({32: REAL[32], 33: "2027 SX 250 West Championship",
                              34: "2027 SX 250 East Championship",
                              35: "2027 SX 450 Championship",
                              36: "2027 SX Manufacturers Championship",
                              37: "2027 SMX 250 Combined Championship",
                              38: "2027 SMX 450 Combined Championship"})
    new = st.refresh(conn, today=JAN, fetch=fetch)
    assert {(k, i) for _, k, i in new} == {
        ("SX 250 West", 33), ("SX 250 East", 34), ("SX 450", 35), ("SX MFR", 36),
        ("SMX 250", 37), ("SMX 450", 38)}
    assert calls[0] == 32                         # starts above 2026's tables
    found = conn.db["cache"][st.CACHE_KEY]
    assert st.championships(2027, found) == [
        ("SX", "450", 35), ("SX", "250 West", 33), ("SX", "250 East", 34),
        ("SMX", "450", 38), ("SMX", "250", 37)]


def test_nothing_is_fetched_when_nothing_is_missing():
    conn = _Conn({"SX", "MX", "WMX", "SMX"})      # October: SEED has all of 2026
    fetch, calls = _provider({})
    assert st.refresh(conn, today=datetime.date(2026, 10, 8), fetch=fetch) == []
    assert calls == []


def test_probing_is_hourly_and_stops_at_a_run_of_empty_ids():
    conn = _Conn({"SX"})
    fetch, calls = _provider({})                  # 2027's tables not posted yet
    assert st.refresh(conn, today=JAN, fetch=fetch) == []
    assert len(calls) == st._MISSES_TO_STOP
    st.refresh(conn, today=JAN, fetch=fetch)      # again within the hour
    assert len(calls) == st._MISSES_TO_STOP       # nothing more fetched


def test_a_provider_outage_never_raises():
    conn = _Conn({"SX"})

    def down(i):
        raise OSError("connection refused")
    assert st.refresh(conn, today=JAN, fetch=down) == []


# --- what the review found ------------------------------------------------------
def test_words_it_does_not_know_leave_a_table_unfiled():
    """Futures, Rookie, Team, Holeshot, Seeding... each would otherwise have
    been filed as the pro championship, and a misfiled 2027 table passes every
    season check there is."""
    for t in ("2027 SX Futures 250 East Championship",
              "2027 SX 450 Triple Crown Championship",
              "2027 SX 450 Rookie Championship",
              "2027 SX 450 Team Championship",
              "2027 SX 450 Holeshot Award",
              "2027 Pro Motocross 450 Rookie of the Year",
              "2027 AMA Amateur National Motocross 450 B",
              "2027 SMX 450 Team Standings",
              "2027 SMX 450 Playoff Seeding",
              "2027 SMX 450 Playoff Points (Combined)",
              "2027 Pro Motocross 250 Manufacturers",
              "2027 WMX Manufacturers Championship"):
        assert st.classify(t) is None, t


def test_the_amas_own_class_names_file():
    cases = {
        "2027 Monster Energy AMA Supercross 450SX Championship": (2027, "SX 450"),
        "2027 SX 250SX West Championship": (2027, "SX 250 West"),
        "2027 SuperMotocross World Championship 450 Playoffs": (2027, "SMX PLAYOFF 450"),
        "2027 SuperMotocross 450 Combined": (2027, "SMX 450"),
    }
    for title, want in cases.items():
        assert st.classify(title) == want, title


def test_a_timeout_never_steps_over_a_table():
    """Id 33 timing out on race night, with 34-38 read, used to skip the SX
    250 West table for the whole season."""
    pages = {33: "2027 SX 250 West Championship", 34: "2027 SX 250 East Championship",
             35: "2027 SX 450 Championship", 36: "2027 SX Manufacturers Championship",
             37: "2027 SMX 250 Combined Championship",
             38: "2027 SMX 450 Combined Championship"}
    conn = _Conn({"SX"})
    fetch, calls = _provider(pages, fail={33})
    st.refresh(conn, today=JAN, fetch=fetch)
    found = conn.db["cache"][st.CACHE_KEY]
    assert st.tables(2027, found) == {}            # nothing past the failure
    fetch, calls = _provider(pages)
    st.refresh(conn, today=JAN, fetch=fetch, force=True)
    found = conn.db["cache"][st.CACHE_KEY]
    assert st.tables(2027, found)["SX 250 West"] == 33
    assert len(st.tables(2027, found)) == 6


def test_a_classifier_fix_refiles_what_was_already_read():
    conn = _Conn(set())
    conn.db["cache"][st.CACHE_KEY] = {
        "titles": {"33": "2027 Monster Energy AMA Supercross 450SX Championship"},
        "tables": {}}                                  # read by an older classify
    new = st.refresh(conn, today=datetime.date(2026, 10, 8), fetch=None)
    assert new == [(2027, "SX 450", 33)]
    assert conn.db["cache"][st.CACHE_KEY]["tables"]["2027"]["SX 450"] == 33


def test_holes_below_the_highest_id_are_looked_at_again():
    conn = _Conn({"SX"})
    conn.db["cache"][st.CACHE_KEY] = {
        "titles": {"32": REAL[32], "34": "2027 SX 250 East Championship"},
        "tables": {"2027": {"SX 250 East": 34}}}
    fetch, calls = _provider({33: "2027 SX 250 West Championship"})
    st.refresh(conn, today=JAN, fetch=fetch)
    assert calls[0] == 33
    assert conn.db["cache"][st.CACHE_KEY]["tables"]["2027"]["SX 250 West"] == 33


def test_stages_decide_what_is_missing():
    assert "MX WMX" not in st.needed({"SX", "MX"})     # WMX first raced at round 2
    assert "MX WMX" in st.needed({"SX", "MX", "WMX"})
    assert "SMX MFR" not in st.needed({"SX", "MX"})    # 2025's came with the playoffs
    assert "SMX MFR" in st.needed({"SMX"})


def test_an_empty_id_is_a_404_not_an_error(monkeypatch):
    class R:
        status_code = 404
        text = "<h1>404 Not Found</h1>"

        def raise_for_status(self):
            raise AssertionError("a 404 is an empty id, not a failure")
    monkeypatch.setattr(st.requests, "get", lambda *a, **k: R())
    assert st._fetch_title(33) == ("", 0)


def test_a_failed_read_of_the_map_is_not_cached_as_no_tables(monkeypatch):
    from src.api import main
    main._SESSIONS_CACHE.pop("series_tables", None)
    main._TABLES_LAST["map"] = {"tables": {"2027": {"SMX PLAYOFF 450": 50}}}

    def down(*a, **k):
        raise OSError("pool timeout")
    monkeypatch.setattr(main, "query", down)
    assert main._smx_playoff_ids(2027) == {"450": 50}
    assert "series_tables" not in main._SESSIONS_CACHE
    main._TABLES_LAST.clear()


def test_a_makes_table_with_no_points_yet_is_not_an_outage(monkeypatch):
    from src.api import main
    main._SESSIONS_CACHE.clear()

    class R:
        text = ("<h2>2027 SX Manufacturers Championship</h2><table><tr><th>#</th>"
                "<th>MANUFACTURER</th><th>POINTS</th></tr></table>")

        def raise_for_status(self):
            pass
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(main, "_db_cache_get", lambda key: None)
    assert main._official_mfr_standings("SX", 2027, 36) is None
