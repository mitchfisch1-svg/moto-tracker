"""Creating a rider who has never raced before must not crash the ingest.

Ironman finished with 27 sessions published and ZERO in our database. The
finale — the round that decided the 450 title — had no results page at all.

The cause was one missing import. `576284b` taught `display_name` to use
`titlecase_name` so a rider would stop being called "Iii", but `riders.py`
imports only `fold`, so `display_name` raised NameError. It is reached on
exactly one path: creating a rider nobody has seen before. Every existing
rider resolves by alias or exact match and never touches it, so the whole
suite stayed green and the next race weekend ingested nothing.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.resolve.riders import display_name, normalize_name  # noqa: E402


def test_a_brand_new_rider_gets_a_name():
    """The path the whole finale died on."""
    assert display_name("LANDEN GORDON") == "Landen Gordon"


def test_the_suffix_that_started_all_this():
    assert display_name("WILL CANAGUIER III") == "Will Canaguier III"
    assert display_name("JESSON TURNER JR") == "Jesson Turner JR"


def test_mc_prefixes_survive_creation():
    assert display_name("BAYLER MCKELLAR") == "Bayler McKellar"


def test_a_holeshot_tag_is_stripped_not_stored():
    assert display_name("HUNTER LAWRENCE Holeshot") == "Hunter Lawrence"


def test_non_ascii_names_are_not_destroyed():
    """normalize_name maps anything outside A-Z to a space; display_name must
    never go through it, or the rider is stored as "Cornelius T Ndel"."""
    assert display_name("CORNELIUS TØNDEL") == "Cornelius Tøndel"


def test_a_source_that_already_capitalised_is_left_alone():
    assert display_name("Cornelius Tøndel") == "Cornelius Tøndel"


def test_matching_is_unaffected_by_how_we_display_it():
    assert normalize_name("Will Canaguier Iii") == normalize_name("Will Canaguier III")
