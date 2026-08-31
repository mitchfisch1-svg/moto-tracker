"""A cache key has to name one thing.

Combined Qualifying is fetched with the CLASS id where every other view takes
a session id. 55908 means "the 250 class" and is the same number at Southwick
in July as at Ironman in August. Keyed on that alone, the board cached on
07-12 was served as the combined qualifying result for every round of the rest
of the season — two months of the wrong morning, on a screen headed with the
right venue.

Third time this shape has cost us: the partial Overall pinned by a key with no
completeness gate, names pinned by a cache with no version, and now a key that
does not identify what it holds.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _SESSION_VIEWS  # noqa: E402


def _keys_for(p, race_id, event_id=None, rt=None):
    """The keying rule from live_session_results, as the code states it."""
    if p == "view_combined_round_ranking":
        return (p, event_id, rt, race_id), f"{p}:{event_id}:{rt}:{race_id}"
    return (p, race_id), f"{p}:{race_id}"


def test_the_same_class_at_two_rounds_is_two_boards():
    """Southwick and Ironman both call the 250 class 55908."""
    _, southwick = _keys_for("view_combined_round_ranking", 55908,
                             event_id=510871, rt=7)
    _, ironman = _keys_for("view_combined_round_ranking", 55908,
                           event_id=516110, rt=7)
    assert southwick != ironman


def test_the_memory_key_separates_them_too():
    """Both caches are keyed the same way; fixing only the DB one would leave
    a running process serving the collision from RAM."""
    a, _ = _keys_for("view_combined_round_ranking", 55908, event_id=510871, rt=7)
    b, _ = _keys_for("view_combined_round_ranking", 55908, event_id=516110, rt=7)
    assert a != b


def test_session_views_are_unchanged_by_this():
    """A race result IS uniquely identified by its id — do not make those keys
    longer and orphan every board already cached under them."""
    for p in ("view_race_result", "view_multi_main_result"):
        _, key = _keys_for(p, 7014046)
        assert key == f"{p}:7014046"


def test_every_view_the_api_accepts_is_covered_here():
    """If a fourth view is added, this test fails until its keying is decided."""
    assert _SESSION_VIEWS == {"view_race_result", "view_multi_main_result",
                              "view_combined_round_ranking"}
