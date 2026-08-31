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
    _feed_is_stalled,
    _gate_alert_key,
    _gate_alert_worthy,
    _is_final_race_of_day,
    _race_finished,
    _race_started,
    _order_signature,
    readable_gap,
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
#
# Names below are the real ones off the feed (verified against Unadilla's
# 25-session programme), not invented shapes.

@pytest.mark.parametrize("name", ["450 Moto #1", "450 Moto #2",
                                  "250 Moto #1", "250 Moto #2",
                                  "450 Main Event", "250 Main"])
def test_every_points_race_gets_a_gate_alert(name):
    """Both motos score in each class, so both are worth rearranging an
    afternoon for — and a main is a class's whole night."""
    assert _gate_alert_worthy(name) is True


@pytest.mark.parametrize("name", ["250 Group A Qualifying 1",
                                  "450 Group B Qualifying 2",
                                  "450 Combined Qualifying Results",
                                  "250 Heat #2", "450 LCQ",
                                  "450 Overall Results"])
def test_qualifying_heats_and_overalls_do_not(name):
    """These run all morning, or land after the racing. Alerting on them trains
    people to ignore the app, which costs you the alerts that do matter."""
    assert _gate_alert_worthy(name) is False


def test_only_the_closing_wmx_moto_alerts():
    """WMX splits across the two days — Moto 1 Friday, Moto 2 Saturday. Only
    the closer earns a push."""
    assert _gate_alert_worthy("WMX Moto #2") is True
    assert _gate_alert_worthy("WMX Moto #1") is False


@pytest.mark.parametrize("name", ["WMX Practice", "WMX Qualifying 1",
                                  "WMX Qualifying 2",
                                  "WMX Combined Qualifying Results",
                                  "WMX Overall Results"])
def test_the_rest_of_the_wmx_programme_stays_quiet(name):
    assert _gate_alert_worthy(name) is False


def test_a_missing_race_name_never_alerts():
    """The feed goes blank between sessions; that must not read as a gate."""
    assert _gate_alert_worthy(None) is False
    assert _gate_alert_worthy("") is False
    assert _gate_alert_worthy("   ") is False


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


# --- has this session stopped being real? ------------------------------------
#
# Budds Creek produced two lies with the same shape. WMX Moto 1 ran out and the
# feed sat there with no checkered we could see, so the app said LIVE for three
# hours after the riders had left. And the provider publishes a grid a DAY
# early: Saturday's 8 AM qualifying was on the lock screen at 11:51 PM Friday.
# Both look identical — a clock that isn't counting and an order that has
# stopped moving — so only elapsed time tells them apart from a real race.

def grid(*, laps=0, remaining=1800, n=5):
    return {
        "riders": [{"position": i, "number": str(100 + i), "laps": laps}
                   for i in range(1, n + 1)],
        "clock": {"remaining": remaining},
    }


def test_a_running_clock_is_never_stalled():
    """The single most important guard: a real race must never be retired."""
    assert _feed_is_stalled(grid(laps=4, remaining=600), 99999, "racing") is False
    assert _feed_is_stalled(grid(remaining=1800), 99999, "staged") is False


def test_a_dead_clock_alone_does_not_end_a_race():
    """The clock hits zero before the leader takes the flag — a moto runs
    'time plus two laps'. Retiring on the clock alone would cut the finish off."""
    assert _feed_is_stalled(grid(laps=8, remaining=0), 30, "racing") is False


def test_a_dead_clock_and_a_frozen_order_ends_it():
    """WMX Moto 1: no time left, nobody moving, and the feed never said so."""
    assert _feed_is_stalled(grid(laps=8, remaining=0), 600, "racing") is True


def test_a_grid_nobody_has_touched_for_half_an_hour_is_not_a_gate():
    """Friday 11:51 PM, showing Saturday's 8 AM qualifying 'on the gate'."""
    assert _feed_is_stalled(grid(remaining=0), 3600, "staged") is True


def test_a_freshly_staged_grid_is_still_a_gate():
    """Riders really are sitting on the gate — don't hide the session."""
    assert _feed_is_stalled(grid(remaining=0), 60, "staged") is False


def test_staged_is_given_far_longer_than_racing():
    """A gate can legitimately sit for a while; a finished race cannot."""
    assert _feed_is_stalled(grid(remaining=0), 300, "racing") is True
    assert _feed_is_stalled(grid(remaining=0), 300, "staged") is False


def test_the_signature_moves_when_the_race_does():
    a = _order_signature(grid(laps=3))
    assert a == _order_signature(grid(laps=3))       # parked
    assert a != _order_signature(grid(laps=4))       # a lap completed


def test_the_signature_ignores_jittering_gaps():
    """Gaps twitch by thousandths even with the field parked. Reading those as
    movement would keep a dead feed looking alive forever."""
    a = grid(laps=2); b = grid(laps=2)
    a["riders"][0]["gap"] = "1.001"
    b["riders"][0]["gap"] = "1.002"
    assert _order_signature(a) == _order_signature(b)


def test_a_missing_clock_counts_as_dead():
    t = grid(laps=6); t["clock"] = {}
    assert _feed_is_stalled(t, 600, "racing") is True


# --- what the glanceable surfaces say beside each rider ----------------------

def test_a_lapped_rider_reads_in_english():
    """The provider writes a lapped deficit as "L1". The app has always
    translated it; the widget and lock screen never did, so the home screen
    read "Hymas L1" where the app read "1 lap down"."""
    assert readable_gap("L1") == "1 lap down"
    assert readable_gap("L2") == "2 laps down"
    assert readable_gap("L 3") == "3 laps down"


def test_a_real_gap_is_left_exactly_as_it_is():
    """Seconds are the whole point during a race — don't reformat them."""
    assert readable_gap("0.406") == "0.406"
    assert readable_gap("+1.246") == "+1.246"


def test_nothing_stays_nothing():
    assert readable_gap("") == ""
    assert readable_gap(None) == ""


def test_a_word_is_not_mistaken_for_a_lap_count():
    assert readable_gap("Leader") == "Leader"
    assert readable_gap("Lapped") == "Lapped"


# --- SMX: a format we have never ingested ------------------------------------
@pytest.mark.parametrize("race_name", [
    "450 Main Event",          # if the playoffs close on a main, as SX does
    "450 Moto #2",             # if they close on a second moto, as MX does
    "450 MAIN",
])
def test_smx_day_retires_under_either_format(race_name):
    """SMX round 1 is the first the app will ever see. If the closing race is
    not recognised the day never retires, and the lock screen and Next Race
    widget sit on a finished round — which is what happened at Ironman for a
    different reason. Match either format rather than bet on one."""
    assert _is_final_race_of_day(race_name, "SMX")


@pytest.mark.parametrize("race_name", [
    "250 Main Event", "250 Moto #2", "450 Heat #1", "450 Qualifying 2",
])
def test_smx_does_not_retire_on_an_earlier_race(race_name):
    assert not _is_final_race_of_day(race_name, "SMX")
