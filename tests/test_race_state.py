"""Race-day state logic — the part with no second chance.

Gate alerts, the lock-screen Live Activity, "day complete", and the Next Race
widget rolling over all hang off these four functions. They only run for a few
hours a week, and when they're wrong the damage is public: an alert that never
fires, or a lock screen insisting a race is on the gate hours after the
checkered (which is exactly what Unadilla produced).

    python -m pytest tests/ -q

No database or network — every case is a hand-built feed payload.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.main import (  # noqa: E402
    _GATE_ALERT_RE,
    _gate_alert_key,
    _is_final_race_of_day,
    _race_finished,
    _race_started,
    _site_shows_this_round,
)


def timing(*, laps=0, elapsed=None, remaining=None, flag=None, status=None,
           announcements=()):
    """A feed payload shaped like the one /live builds from the LRM JSON."""
    return {
        "riders": [{"laps": laps}],
        "clock": {"elapsed": elapsed, "remaining": remaining, "flag": flag},
        "race_status": status,
        "announcements": [{"m": m} for m in announcements],
    }


# --- has it gone green? ------------------------------------------------------

def test_staged_grid_is_not_racing():
    """The feed publishes the grid when a session is STAGED — everyone on zero
    laps and a full clock. Reading that as racing is what framed a staged grid
    as a race in progress, complete with a "leader"."""
    assert _race_started(timing(laps=0, elapsed=0, remaining=1800)) is False


def test_a_completed_lap_means_racing():
    assert _race_started(timing(laps=1, remaining=1700)) is True


def test_a_running_clock_means_racing():
    """Lap counts can lag on the first lap; an elapsed clock still means green."""
    assert _race_started(timing(laps=0, elapsed=12.5, remaining=1787)) is True


@pytest.mark.parametrize("msg", ["Green Flag at: 4:45:01 PM",
                                 "#96 Hunter Lawrence holeshot 11.4",
                                 "gate drop"])
def test_race_control_can_declare_green(msg):
    assert _race_started(timing(announcements=[msg])) is True


def test_garbage_clock_does_not_start_the_race():
    """The feed occasionally serves a non-numeric clock; that must not be read
    as elapsed time."""
    assert _race_started(timing(elapsed="--")) is False


# --- is it over? -------------------------------------------------------------

def test_checkered_flag_ends_it():
    assert _race_finished(timing(flag="Checkered", remaining=0)) is True


def test_official_status_ends_it():
    assert _race_finished(timing(status="Official", remaining=-10)) is True


def test_stale_checkered_cannot_end_a_race_that_is_still_running():
    """announcements accumulate across the whole program, so the previous
    moto's checkered is still sitting there while the next one runs. Retiring
    the day on that would kill live timing mid-programme."""
    t = timing(laps=3, remaining=900, announcements=["Checkered Flag at: 2:31 PM"])
    assert _race_finished(t) is False


def test_checkered_with_the_clock_expired_does_end_it():
    t = timing(laps=17, remaining=-30, announcements=["Checkered Flag at: 4:58 PM"])
    assert _race_finished(t) is True


def test_a_staged_race_is_not_finished():
    assert _race_finished(timing(laps=0, remaining=1800)) is False


# --- was that the last race of the day? --------------------------------------

@pytest.mark.parametrize("name", ["450 Moto #2", "450 Class Moto #2",
                                  "450 MOTO 2"])
def test_the_450s_second_moto_closes_a_motocross_round(name):
    assert _is_final_race_of_day(name, "MX") is True


@pytest.mark.parametrize("name", [
    "450 Moto #1",          # first moto, half the day still to come
    "250 Moto #2",          # runs BEFORE the 450s — the day is not over
    "450 Group A Qualifying 2",
    "WMX Moto #2",
])
def test_other_motocross_sessions_do_not_close_the_day(name):
    assert _is_final_race_of_day(name, "MX") is False


def test_supercross_closes_on_the_450_main():
    assert _is_final_race_of_day("450 Main Event", "SX") is True
    assert _is_final_race_of_day("250 Main Event", "SX") is False


def test_triple_crown_closes_on_race_three():
    assert _is_final_race_of_day("450 Race #3", "SX") is True
    assert _is_final_race_of_day("450 Race #1", "SX") is False


def test_unknown_series_never_closes_the_day():
    """Better to leave the day open than retire it on a series we don't model."""
    assert _is_final_race_of_day("450 Moto #2", "WSX") is False
    assert _is_final_race_of_day("450 Moto #2", None) is False


# --- which sessions are worth waking someone up for? -------------------------

@pytest.mark.parametrize("name", ["450 Moto #2", "250 Moto #1",
                                  "450 Main Event", "WMX Moto #1"])
def test_motos_and_mains_get_a_gate_alert(name):
    assert _GATE_ALERT_RE.search(name)


@pytest.mark.parametrize("name", ["250 Group A Qualifying 1",
                                  "450 Combined Qualifying Results",
                                  "250 Heat #2", "450 LCQ"])
def test_qualifying_and_heats_do_not(name):
    """These run all morning. Alerting on them trains people to ignore the app."""
    assert not _GATE_ALERT_RE.search(name)


# --- is the results site showing THIS round, or last week's? -----------------
#
# A two-day round opens its window 30h early, which puts the Friday decision on
# one signal: the program the results site is serving. The trap is that the site
# keeps the PREVIOUS round up until the new one goes on track, and a round has
# no results id of its own until race morning.

FINISHED = {"512368", "513129"}          # Washougal, Unadilla


def test_a_round_we_have_already_closed_is_a_stale_page():
    """Budds Creek has no id of its own yet, and on Friday morning the site was
    still serving Unadilla — which we ingested and closed days earlier. Reading
    that as "the round is under way" put a LIVE screen full of last week's
    results on the app, and woke the 60s push loops for a day of empty compute."""
    assert _site_shows_this_round(None, "513129", FINISHED) is False


def test_an_unknown_id_means_the_site_has_moved_on():
    """The moment the site publishes a program we've never ingested, that IS the
    new round starting — this is what has to keep working, or Friday goes dark."""
    assert _site_shows_this_round(None, "513500", FINISHED) is True


def test_our_own_id_still_wins_once_we_have_it():
    assert _site_shows_this_round("513500", "513500", FINISHED) is True


def test_our_id_against_someone_else_s_program_is_not_us():
    assert _site_shows_this_round("513500", "513129", FINISHED) is False


def test_an_empty_site_is_never_evidence_of_racing():
    assert _site_shows_this_round(None, None, FINISHED) is False
    assert _site_shows_this_round("513500", None, FINISHED) is False


def test_no_finished_rounds_yet_does_not_block_the_opener():
    """Round 1 of a season: nothing is closed out, so any program the site
    serves is genuinely new."""
    assert _site_shows_this_round(None, "487830", set()) is True


# --- one gate alert per moto, per day ----------------------------------------
#
# The event id comes from the schedule and the race name from the timing feed,
# so the two can disagree. The day in the key bounds the damage.

def test_the_same_moto_alerts_once_a_day():
    """A red flag re-stages the grid; the second staging must stay quiet."""
    a = _gate_alert_key(27, "450 Moto #2", "2026-08-22")
    b = _gate_alert_key(27, "450 Moto #2", "2026-08-22")
    assert a == b


def test_friday_cannot_suppress_saturday_on_a_two_day_round():
    """Budds Creek is ONE event id across both days. Before the day was in the
    key, anything firing on Friday — including off a stale feed — silently ate
    the real alert for the same race name on Saturday."""
    friday = _gate_alert_key(27, "450 Moto #2", "2026-08-21")
    saturday = _gate_alert_key(27, "450 Moto #2", "2026-08-22")
    assert friday != saturday


def test_each_moto_of_the_day_gets_its_own_key():
    day = "2026-08-22"
    keys = {_gate_alert_key(27, n, day) for n in
            ["250 Moto #1", "450 Moto #1", "250 Moto #2", "450 Moto #2"]}
    assert len(keys) == 4


def test_the_race_name_is_normalised():
    """The feed's spacing/case shouldn't mint a second key for one race."""
    assert (_gate_alert_key(27, "  450 Moto #2  ", "2026-08-22")
            == _gate_alert_key(27, "450 MOTO #2", "2026-08-22"))


def test_two_rounds_never_share_a_key():
    assert (_gate_alert_key(27, "450 Moto #2", "2026-08-22")
            != _gate_alert_key(28, "450 Moto #2", "2026-08-22"))
