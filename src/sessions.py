"""What a race label means: its class, and whether it scores.

Pure string logic, and deliberately importing nothing but `re`.

It used to live in adapters/results_html.py, which is the right home for a
scraper but the wrong one for anything the API needs: importing it drags in
RiderResolver and therefore RapidFuzz, which is not in requirements-api.txt.
The web service is a deliberately slim install ("no scrapers"), so a single
import of `classify` from the API failed the whole deploy on 09-11 — Render
kept the previous build alive, which is the only reason it was survivable.

So it lives here, one implementation, importable from either side.
"""

import re

# '450 Race #1' — a Supercross Triple Crown race, which is a main by another
# name and would otherwise classify as nothing at all.
_TC_RACE_RE = re.compile(r"RACE\s*#?\s*\d")

# The session types that award championship points. Everything else — practice,
# qualifying, heats, LCQs, the SMX wildcards — is racing that settles nothing.
POINTS_TYPES = ("moto", "main")


def classify(label: str):
    """Map a race label to (class, type). Returns (None, None) if unrecognized."""
    up = (label or "").upper()
    if up.startswith("450"):
        cls = "450"
    elif up.startswith("250"):
        cls = "250"
    elif "WMX" in up:
        cls = "WMX"
    elif "SMX NEXT" in up:
        cls = "SMX Next"
    elif "250" in up and "SHOWDOWN" in up:
        # Sponsor-prefixed finale, e.g. "Dave Coombs Sr. 250 East West Showdown"
        cls = "250"
    else:
        cls = None

    if "MAIN" in up:
        typ = "main"
    elif "SHOWDOWN" in up:
        typ = "main"   # the East/West Showdown is that night's 250 main event
    elif "MOTO" in up:
        typ = "moto"
    elif "LCQ" in up:
        typ = "lcq"
    elif "HEAT" in up:
        typ = "heat"
    elif "PRACTICE" in up:
        typ = "practice"
    elif "QUALIF" in up:
        typ = "qualifying"
    elif _TC_RACE_RE.search(up):  # '450 Race #1' — a Triple Crown race
        typ = "tc_race"
    else:
        typ = None
    return cls, typ


def scores_points(label: str) -> bool:
    """Does this session award championship points?"""
    return classify(label)[1] in POINTS_TYPES
