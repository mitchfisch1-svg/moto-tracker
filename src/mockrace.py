"""A synthetic race, so the live path can be exercised without a race.

The Live Activity ran two minutes behind at Ironman. A cause was found and
fixed — the race clock was counting as a state change, so the guard against
Apple's update budget never fired — but "fixed" is a claim, and the only thing
that has ever settled a claim on this project is watching it happen. The next
race is Sep 12, and after Sep 26 there is nothing until January.

So generate the race. This drives a plausible running order through a moto:
the clock counts down, riders swap places, gaps open and close. Everything
downstream is real — /live serves it, the app polls it, the Live Activity loop
diffs it and pushes to actual phones through actual APNs. What we are testing
is the machinery, and the machinery cannot tell the difference.

WHAT IT IS NOT: an illusion. The venue and race name say plainly that this is a
system test. v1.5 is on the App Store, and someone opening the app during a run
must not be shown a race that is not happening — the whole month has been spent
removing exactly that. It is honest about being a test and still exercises
every line that matters.

Guarded by MXT_MOCK_KEY. Unset, the endpoint does not exist.
"""

import math
import os
import random
import threading
import time

# One run at a time, in a module-level slot the /live path can see.
_lock = threading.Lock()
_run: dict | None = None

MAX_MINUTES = 45          # a moto is 30 + 2 laps; never leave one running longer
MAX_SESSIONS = 4          # a real day is more, but a test nobody watches is waste
VENUE = "MXT System Test"
RACE = "Mock Moto (system test)"

GATE_S = 120              # on the gate before green — the transition Ironman missed
FINISH_S = 60             # the finishing order stays up before the next session

# A race day is not one session, it is a SEQUENCE of them, and the handoff
# between two is a thing the lock screen has to survive: one race ends, its
# result sits for a moment, then a different race name appears back on the
# gate. Nothing had ever tested that — the first mock ran a single moto, which
# exercises everything except the seam. These names make the seam visible: the
# race name is inside the Live Activity's change key, so a session change is
# unmissable both on the card and in the diff.
_SESSIONS = ["250 Moto 1", "450 Moto 1", "250 Moto 2", "450 Moto 2"]

# Real names make the diff realistic — same string lengths, same surname
# rendering, same widget truncation behaviour as a genuine board.
_FIELD = [
    ("Chance Hymas", "29"), ("Julien Beaumer", "13"), ("Cole Davies", "37"),
    ("Ryder DiFrancesco", "34"), ("Levi Kitchen", "47"), ("Drew Adams", "35"),
    ("Caden Dudney", "82"), ("Landen Gordon", "180"), ("Nate Thrasher", "25"),
    ("Kayden Minear", "99"),
]


def start(minutes: int, seed: int = 7, sessions: int = 1) -> dict:
    """Begin a run of `sessions` back-to-back sessions. Returns its status.

    Each session is `minutes` long (two of them on the gate, the rest racing)
    and is followed by FINISH_S showing its finishing order, which is what a
    real programme does between motos. With sessions=1 this is the original
    single-moto behaviour plus a proper finish, instead of the feed simply
    vanishing mid-race.
    """
    global _run
    minutes = max(1, min(int(minutes), MAX_MINUTES))
    sessions = max(1, min(int(sessions), MAX_SESSIONS))
    with _lock:
        _run = {
            "started_at": time.time(),
            "session_s": minutes * 60,
            "sessions": sessions,
            "duration_s": sessions * (minutes * 60 + FINISH_S),
            "seed": int(seed),
        }
        return _status_locked()


def stop() -> dict:
    global _run
    with _lock:
        _run = None
        return {"running": False}


def status() -> dict:
    with _lock:
        return _status_locked()


def _phase(elapsed: float, run: dict):
    """Where in the programme we are: (session index, offset in it, state).

    One session is `session_s` of gate-then-racing followed by FINISH_S of
    finishing order. Everything past the last one is over.
    """
    block = run["session_s"] + FINISH_S
    idx = min(int(elapsed // block), run["sessions"] - 1)
    within = elapsed - idx * block
    if within < GATE_S:
        return idx, within, "staged"
    if within < run["session_s"]:
        return idx, within, "racing"
    return idx, within, "finished"


def _session_name(idx: int) -> str:
    return f"{_SESSIONS[idx % len(_SESSIONS)]} (system test)"


def _status_locked() -> dict:
    if not _run:
        return {"running": False}
    elapsed = time.time() - _run["started_at"]
    left = _run["duration_s"] - elapsed
    if left <= 0:
        return {"running": False, "expired": True}
    idx, _, state = _phase(elapsed, _run)
    return {"running": True, "elapsed_s": round(elapsed),
            "remaining_s": round(left), "venue": VENUE,
            "race": _session_name(idx), "state": state,
            "session": idx + 1, "sessions": _run["sessions"]}


def _order(elapsed: float, seed: int):
    """A field that actually moves.

    Each rider walks a slow sine of its own period, so positions genuinely swap
    rather than jitter — which is what the Live Activity diff has to survive.
    Deterministic from the seed: two runs with the same seed produce the same
    race, so a result can be compared against a previous one.
    """
    rnd = random.Random(seed)
    phases = [(rnd.uniform(0, 6.28), rnd.uniform(70, 240), rnd.uniform(0.4, 2.2))
              for _ in _FIELD]
    scored = []
    for i, (name, num) in enumerate(_FIELD):
        phase, period, swing = phases[i]
        pace = i + swing * math.sin(phase + (elapsed / period) * 6.28)
        scored.append((pace, name, num))
    scored.sort()
    leader_pace = scored[0][0]
    out = []
    for pos, (pace, name, num) in enumerate(scored, start=1):
        # The real feed sends the gap as a STRING ("1.613"), and readable_gap
        # calls .strip() on it. A float here raised AttributeError the first
        # time this was run — which is the whole reason to run it.
        gap = None if pos == 1 else f"{(pace - leader_pace) * 1.7 + 0.2:.3f}"
        out.append({
            "position": pos, "name": name, "number": num,
            "gap": gap, "laps": int(elapsed // 110),
            "status": "running", "manufacturer": None,
            "best_lap": round(112 + (pace - leader_pace) * 0.4, 3),
            "on_track": True, "position_change": 0, "sectors": [],
        })
    return out


def timing():
    """The current synthetic state, or None when no run is active.

    Shaped exactly like the real feed's `timing` block, because everything
    downstream — the state diff, the lock-screen content, the widget — reads
    these keys and must not know the difference.
    """
    with _lock:
        if not _run:
            return None
        elapsed = time.time() - _run["started_at"]
        if elapsed >= _run["duration_s"]:
            return None
        run = dict(_run)

    idx, within, state = _phase(elapsed, run)
    # Each session gets its own seed, so the running order genuinely differs
    # from the last one. A second moto that replayed the first would test the
    # transition but not that the card actually re-renders new content.
    seed = run["seed"] + idx * 101
    racing_s = run["session_s"] - GATE_S
    if state == "staged":
        # Two minutes on the gate, then green — so the staged -> racing
        # transition the lock screen missed at Ironman happens every session,
        # not just once a run.
        remaining, flag, order_t = None, "prestage", 0.0
    elif state == "racing":
        remaining, flag = max(0, int(run["session_s"] - within)), "green"
        order_t = within - GATE_S
    else:
        # Finished: the order freezes and the clock reads zero. This is what
        # `_la_content_state` turns into "· final", and what the app shows
        # between motos. Never exercised before multi-session runs existed.
        remaining, flag, order_t = 0, "checkered", racing_s
    return {
        "race_name": _session_name(idx),
        "race_state": state,
        "riders": _order(order_t, seed),
        "clock": {"remaining": remaining, "flag": flag},
        "announcements": [],
        "mock": True,
    }
