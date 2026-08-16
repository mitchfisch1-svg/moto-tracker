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
    _is_final_race_of_day,
    _race_finished,
    _race_started,
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
