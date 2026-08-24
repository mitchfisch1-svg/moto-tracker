"""Turning "1 pm" into a moment a phone can count down to.

At 10am on race day, watching qualifying, there was no way in the app to tell
that the motos were at 1:00 and 3:30. The times were there all along —
`events.broadcast` carries the day's windows — but as strings. A string cannot
be counted down to, cannot be marked "on now", and cannot be known to have
passed. So resolve them against the event's own date, in Eastern, where the
series publishes them.

Every block below is a real one from the 2026 season.

    python -m pytest tests/ -q
"""

import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import _stamp_broadcast  # noqa: E402

IRONMAN = [{"label": "Race Day Live", "time_et": "10 am", "providers": ["Peacock"]},
           {"label": "Gate Drop", "time_et": "1 pm",
            "providers": ["Peacock", "SiriusXM"]}]
# Budds Creek added a network window for the second motos — this is the 3:30
# that nobody could find in the app.
BUDDS = IRONMAN + [{"label": "Racing Action", "time_et": "3:30 pm",
                    "providers": ["NBC"]}]
# SMX round 1 lists a replay that airs the NEXT day, in among Saturday's blocks.
SMX_R1 = [{"label": "Race Day Live", "time_et": "9 am", "providers": ["Peacock"]},
          {"label": "Gate Drop", "time_et": "3 pm", "providers": ["Peacock"]},
          {"label": "Post-Race", "time_et": "6 pm", "providers": ["Peacock"]},
          {"label": "Sunday Encore", "time_et": "4 pm", "providers": ["NBC"]}]


def _stamp(blocks, date):
    return _stamp_broadcast([dict(b) for b in blocks], date)


def test_the_gate_drop_becomes_an_instant():
    out = _stamp(IRONMAN, datetime.date(2026, 8, 29))
    # 1 PM Eastern in August is 17:00 UTC — daylight time, not standard.
    assert out[1]["start_utc"] == "2026-08-29T17:00:00+00:00"
    assert out[1]["time_label"] == "1:00 PM"


def test_a_half_hour_window_survives():
    """3:30 is the one Mitch went looking for."""
    out = _stamp(BUDDS, datetime.date(2026, 8, 22))
    assert out[2]["start_utc"] == "2026-08-22T19:30:00+00:00"
    assert out[2]["time_label"] == "3:30 PM"


def test_morning_and_afternoon_are_not_confused():
    out = _stamp(IRONMAN, datetime.date(2026, 8, 29))
    assert out[0]["start_utc"] == "2026-08-29T14:00:00+00:00"   # 10 AM ET
    assert out[0]["time_label"] == "10:00 AM"


def test_a_block_that_belongs_to_another_day_gets_no_time():
    """"Sunday Encore 4 pm" sits after a 6 pm block. Stamping it on the event's
    own date would put a replay before the race it replays — a confident,
    wrong answer where no answer is the honest one."""
    out = _stamp(SMX_R1, datetime.date(2026, 9, 12))
    assert "start_utc" not in out[-1]
    assert out[-1]["time_et"] == "4 pm"        # still shown, just untimed
    assert all("start_utc" in b for b in out[:-1])


def test_blocks_we_cannot_parse_are_left_alone():
    odd = [{"label": "Gate Drop", "time_et": "TBA", "providers": []},
           {"label": "Encore", "time_et": None, "providers": []}]
    out = _stamp(odd, datetime.date(2026, 8, 29))
    assert all("start_utc" not in b for b in out)


def test_no_broadcast_and_no_date_are_not_errors():
    assert _stamp_broadcast(None, datetime.date(2026, 8, 29)) is None
    assert _stamp_broadcast([], datetime.date(2026, 8, 29)) == []
    assert _stamp(IRONMAN, None) == IRONMAN
