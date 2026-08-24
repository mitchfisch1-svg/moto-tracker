"""Re-casing names the results feed shouts at us, without wrecking them.

Results arrive upper-cased, so every name has to be re-cased for display. A
plain .title() gets the ordinary cases right and the interesting ones wrong,
and Budds Creek showed both: the app listed "Will Canaguier Iii", and the lock
screen — which has room for one word — listed a rider called "III".

    python -m pytest tests/ -q
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.names import display_surname, titlecase_name  # noqa: E402


@pytest.mark.parametrize("shouted,expected", [
    ("HAIDEN DEEGAN", "Haiden Deegan"),
    ("WILL CANAGUIER III", "Will Canaguier III"),   # was "Will Canaguier Iii"
    ("TRE FIERRO III", "Tre Fierro III"),
    ("CHANCE HYMAS JR", "Chance Hymas JR"),
    ("L MCGRATH", "L McGrath"),                     # was "L Mcgrath"
    ("SHANE O'BRIEN", "Shane O'Brien"),
])
def test_a_shouted_name_is_re_cased(shouted, expected):
    assert titlecase_name(shouted) == expected


def test_a_name_that_already_has_case_is_left_alone():
    """Mixed case means the source knew what it meant — don't second-guess it."""
    assert titlecase_name("Jett Lawrence") == "Jett Lawrence"
    assert titlecase_name("R.J. Hampshire") == "R.J. Hampshire"
    assert titlecase_name("Cornelius Tøndel") == "Cornelius Tøndel"


def test_empty_input_survives():
    assert titlecase_name("") == ""
    assert titlecase_name(None) is None


# --- the one word a lock screen has room for ---------------------------------

def test_a_suffix_brings_the_real_surname_with_it():
    """The lock screen listed a rider called "III" — the kind of detail that
    makes a whole board look untrustworthy."""
    assert display_surname("Tre Fierro III") == "Fierro III"
    assert display_surname("Will Canaguier III") == "Canaguier III"


@pytest.mark.parametrize("full,expected", [
    ("Haiden Deegan", "Deegan"),
    ("Jett Lawrence", "Lawrence"),
    ("R.J. Hampshire", "Hampshire"),
    ("Cornelius Tøndel", "Tøndel"),
])
def test_an_ordinary_name_gives_its_last_word(full, expected):
    assert display_surname(full) == expected


def test_a_single_word_is_its_own_surname():
    assert display_surname("Deegan") == "Deegan"


def test_nothing_in_nothing_out():
    assert display_surname("") == ""
    assert display_surname(None) == ""
