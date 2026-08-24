"""Rejecting a lap time the feed should never have published.

At Budds Creek, 250 qualifying was correct at 10:24 — Kitchen 1:56.283, Davies
+0.313, DiFrancesco +0.661, matching the broadcast exactly. At 10:25:14 race
control reported "#180 Landen Gordon new fastest lap of 1:45.742" and the app
took it: Gordon jumped to P1, Kitchen was shown +10.541 behind, and the session
was posted COMPLETE with the wrong winner. The official 250 overall has Gordon
fourth on 1:57.173.

Every number below is from that session or the broadcast.

    python -m pytest tests/ -q
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _credible_lap_floor  # noqa: E402

# The real 250 combined board, in seconds. Group A up front, Group B behind —
# the spread that makes a whole-field mean useless here.
REAL = [116.283, 116.596, 116.944, 117.173, 117.325, 117.934, 118.009,
        118.075, 118.159, 118.187, 120.548, 120.747, 121.492, 121.695,
        122.287, 123.172, 123.524, 123.585]
BOGUS = 105.742          # the 1:45.742
LEGIT_POLE = 116.283     # Levi Kitchen's real 1:56.283


def test_the_bogus_lap_is_rejected():
    floor = _credible_lap_floor(REAL + [BOGUS])
    assert floor is not None
    assert BOGUS < floor


def test_the_real_pole_survives_comfortably():
    """The point is not just that it passes — it has to pass with room, or the
    check will start eating genuine laps on a day someone is quick."""
    floor = _credible_lap_floor(REAL + [BOGUS])
    assert LEGIT_POLE > floor
    assert LEGIT_POLE - floor > 5          # seconds of headroom


def test_a_dominant_but_real_lap_is_kept():
    """Deegan was half a second clear in 450 qualifying. Even three seconds
    clear — an enormous margin in this sport — must still publish."""
    dominant = LEGIT_POLE - 3
    assert dominant > _credible_lap_floor(REAL + [dominant])


def test_the_median_is_not_dragged_by_the_outlier():
    """A mean would be pulled toward the bogus lap and help it clear the bar.
    The floor should barely move whether the outlier is present or not."""
    a = _credible_lap_floor(REAL)
    b = _credible_lap_floor(REAL + [BOGUS])
    assert abs(a - b) < 1.0


def test_slow_privateers_do_not_drag_the_bar_down():
    """A combined board merges factory riders with privateers seconds slower.
    Judging against the whole field would drop the bar far enough for the bogus
    lap to look reasonable."""
    tail = [130.0 + i for i in range(20)]      # a long slow back half
    assert BOGUS < _credible_lap_floor(REAL + tail + [BOGUS])


def test_too_small_a_field_is_not_judged():
    """Early in a session there is nothing to judge against, and rejecting the
    first real lap would be worse than publishing an odd one."""
    assert _credible_lap_floor([116.3, 117.0]) is None
    assert _credible_lap_floor([]) is None


def test_junk_values_are_ignored():
    floor = _credible_lap_floor(REAL + [None, 0, -5, "1:45.742"])
    assert floor is not None and LEGIT_POLE > floor
