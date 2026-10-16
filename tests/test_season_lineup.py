"""A class's coming-season lineup: confirmed riders, last season's riders not
confirmed yet, retirements. Nothing guessed.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import build_lineup  # noqa: E402


def std(rid, cls, pos, bike, name):
    return {"rider_id": rid, "class": cls, "position": pos, "bike": bike,
            "full_name": name, "headshot_url": None}


def cur(rid, team, make, cls, name, series=None, kind=None, best=None):
    return {"rider_id": rid, "team": team, "manufacturer": make, "class": cls,
            "announced_class": cls, "series": series, "kind": kind, "source": "news",
            "full_name": name, "headshot_url": None, "best_last": best}


STD_SX = [
    std(1, "450", 1, "Suzuki", "Ken Roczen"),
    std(2, "450", 3, "Yamaha", "Cooper Webb"),
    std(3, "450", 4, "KTM", "Eli Tomac"),
    std(4, "250 West", 1, "Yamaha", "Haiden Deegan"),
    std(5, "250 West", 9, "Kawasaki", "Josh Varize"),
    std(6, "250 East", 2, "Kawasaki", "Seth Hammaker"),
]
CUR = [
    cur(2, "Team Tedder Husqvarna", "Husqvarna", "450", "Cooper Webb", series="SX", best=3),
    cur(5, "Grindstone Friesen Group Kawasaki", "Kawasaki", "250 West", "Josh Varize", best=9),
    cur(6, "Monster Energy Pro Circuit Kawasaki", "Kawasaki", None, "Seth Hammaker", best=2),
    cur(7, "Red Bull KTM Factory Racing", "KTM", "250", "Sacha Coenen", kind="arrival"),
]
PREV = {2: {"team": "Monster Energy Star Racing Yamaha", "manufacturer": "Yamaha"}}


def lineup(ser, cls, std_rows=STD_SX):
    return build_lineup(ser, cls, std_rows, CUR, PREV, {}, retired_ids={3})


def names(rows):
    return [r["full_name"] for r in rows]


def test_450_confirmed_then_not_yet_then_retired():
    out = lineup("SX", "450")
    assert names(out["confirmed"]) == ["Cooper Webb"]
    assert out["confirmed"][0]["moved"] and out["confirmed"][0]["from_make"] == "Yamaha"
    assert names(out["not_confirmed"]) == ["Ken Roczen"]
    assert names(out["retired"]) == ["Eli Tomac"]


def test_an_unconfirmed_class_move_stays_where_he_raced():
    """Deegan's 450SX move is reported, not announced: he is listed in 250
    West as not confirmed, never moved up on a guess."""
    out = lineup("SX", "250 West")
    assert "Haiden Deegan" in names(out["not_confirmed"])


def test_an_announced_region_puts_him_on_one_list_only():
    assert "Josh Varize" in names(lineup("SX", "250 West")["confirmed"])
    assert "Josh Varize" not in names(lineup("SX", "250 East")["confirmed"])


def test_a_250_rider_with_no_region_announced_is_on_both_lists_marked_so():
    for cls in ("250 East", "250 West"):
        row = next(r for r in lineup("SX", cls)["confirmed"] if r["full_name"] == "Sacha Coenen")
        assert row["region_known"] is False and row["moved"]   # an arrival


def test_a_supercross_only_deal_is_not_in_pro_motocross():
    std_mx = [std(2, "450", 5, "Yamaha", "Cooper Webb"), std(1, "450", 7, "Suzuki", "Ken Roczen")]
    out = lineup("MX", "450", std_mx)
    assert "Cooper Webb" not in names(out["confirmed"]) + names(out["not_confirmed"])


def test_a_new_deal_naming_no_class_is_said_as_such():
    row = next(r for r in lineup("SX", "250 East")["not_confirmed"]
               if r["full_name"] == "Seth Hammaker")
    assert row["new_team"] == "Monster Energy Pro Circuit Kawasaki"


def test_the_other_series_top_riders_are_candidates_too():
    """Jett Lawrence raced no 2026 Supercross; the 2027 SX 450 list without
    him read as "not racing"."""
    other = [dict(std(9, "450", 2, "Honda", "Jett Lawrence"), series="MX"),
             dict(std(10, "450", 40, "KTM", "Back Marker"), series="MX")]
    out = build_lineup("SX", "450", STD_SX, CUR, PREV, {}, {3}, other)
    row = next(r for r in out["not_confirmed"] if r["full_name"] == "Jett Lawrence")
    assert row["last_series"] == "MX" and row["last_position"] == 2
    assert "Back Marker" not in names(out["not_confirmed"])     # outside the top 20


def test_re_signed_only_when_the_team_is_the_same():
    prev = {**PREV, 6: {"team": "Monster Energy Pro Circuit Kawasaki", "manufacturer": "Kawasaki"}}
    out = build_lineup("SX", "250 East", STD_SX, CUR, prev, {}, {3})
    row = next(r for r in out["not_confirmed"] if r["full_name"] == "Seth Hammaker")
    assert row["re_signed"] is True
    moved = [dict(c, team="Some Other Team") if c["rider_id"] == 6 else c for c in CUR]
    out = build_lineup("SX", "250 East", STD_SX, moved, prev, {}, {3})
    row = next(r for r in out["not_confirmed"] if r["full_name"] == "Seth Hammaker")
    assert row["re_signed"] is False



def test_an_a1_entry_list_confirms_supercross_not_pro_motocross():
    """From the A1 entry list to May, the 2027 Motocross lineup called every
    Supercross entrant confirmed for Motocross."""
    roczen = dict(cur(1, "Pipes Motorsports Group Suzuki", "Suzuki", "450", "Ken Roczen"),
                  source="entry_list", announced_class=None)
    std_mx = [std(1, "450", 7, "Suzuki", "Ken Roczen")]
    entered = {1: {"SX": "450"}}
    sx = build_lineup("SX", "450", [std(1, "450", 1, "Suzuki", "Ken Roczen")], [roczen],
                      {}, {}, set(), None, entered)
    assert names(sx["confirmed"]) == ["Ken Roczen"]
    mx = build_lineup("MX", "450", std_mx, [roczen], {}, {}, set(), None, entered)
    assert names(mx["confirmed"]) == []
    row = mx["not_confirmed"][0]
    assert row["full_name"] == "Ken Roczen" and row["entered_other"] == ["SX"]


def test_an_announcement_still_counts_after_an_entry_list_outranks_it():
    """Prado's announced 450 stays confirmed for Pro Motocross after the A1
    entry list overwrites his row's source."""
    prado = dict(cur(43, "Red Bull KTM Factory Racing", "KTM", "450", "Jorge Prado"),
                 source="entry_list")
    mx = build_lineup("MX", "450", [std(43, "450", 4, "KTM", "Jorge Prado")], [prado],
                      {}, {}, set(), None, {43: {"SX": "450"}})
    assert names(mx["confirmed"]) == ["Jorge Prado"]


def test_an_entry_list_region_puts_him_on_one_250_list():
    rider = dict(cur(5, "Team X", "Honda", "250", "Some Rider"), announced_class=None,
                 source="entry_list")
    west = build_lineup("SX", "250 West", [], [rider], {}, {}, set(), None, {5: {"SX": "250 West"}})
    east = build_lineup("SX", "250 East", [], [rider], {}, {}, set(), None, {5: {"SX": "250 West"}})
    assert names(west["confirmed"]) == ["Some Rider"] and names(east["confirmed"]) == []
