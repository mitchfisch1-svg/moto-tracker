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
VENUE = "MXT System Test"
RACE = "Mock Moto (system test)"

# Real names make the diff realistic — same string lengths, same surname
# rendering, same widget truncation behaviour as a genuine board.
_FIELD = [
    ("Chance Hymas", "29"), ("Julien Beaumer", "13"), ("Cole Davies", "37"),
    ("Ryder DiFrancesco", "34"), ("Levi Kitchen", "47"), ("Drew Adams", "35"),
    ("Caden Dudney", "82"), ("Landen Gordon", "180"), ("Nate Thrasher", "25"),
    ("Kayden Minear", "99"),
]


def start(minutes: int, seed: int = 7) -> dict:
    """Begin a run. Returns its status."""
    global _run
    minutes = max(1, min(int(minutes), MAX_MINUTES))
    with _lock:
        _run = {
            "started_at": time.time(),
            "duration_s": minutes * 60,
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


def _status_locked() -> dict:
    if not _run:
        return {"running": False}
    elapsed = time.time() - _run["started_at"]
    left = _run["duration_s"] - elapsed
    if left <= 0:
        return {"running": False, "expired": True}
    return {"running": True, "elapsed_s": round(elapsed),
            "remaining_s": round(left), "venue": VENUE, "race": RACE}


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
        seed, total = _run["seed"], _run["duration_s"]

    remaining = max(0, int(total - elapsed))
    # Two minutes on the gate, then green — so the staged -> racing transition
    # the lock screen missed at Ironman happens on every single run.
    staged = elapsed < 120
    return {
        "race_name": RACE,
        "race_state": "staged" if staged else "racing",
        "riders": _order(max(0.0, elapsed - 120), seed),
        "clock": {"remaining": None if staged else remaining,
                  "flag": "prestage" if staged else "green"},
        "announcements": [],
        "mock": True,
    }
