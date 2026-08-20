"""Championship standings straight from the timing provider.

We used to derive standings entirely from finishing positions, which is fine
right up until it isn't: the series applies **point adjustments** — manual
penalties that no arithmetic over results can reproduce — and a single wrong
value in the points table silently mis-scored every championship for a whole
season (see src/standings.py).

The provider publishes the real thing as ordinary HTML:

    results.supermotocross.com/results/?p=view_series_points&id={N}&event_id={E}

``event_id`` only anchors *as of when*; any recent event works, and asking an MX
event for the SX championship returns the full SX season. So one anchor serves
every championship.

This adapter only reads and parses. Deciding what to do with the numbers is
``standings.apply_official_standings``.
"""

import re

import requests
from bs4 import BeautifulSoup

from ..names import fold

RESULTS_HOME = "https://results.supermotocross.com/results/"
_URL = RESULTS_HOME + "?p=view_series_points&id={sid}&event_id={eid}"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; MotoTracker/1.0; "
                     "+https://motoxtracker.com)"}

# (series abbrev, our standings class, the provider's series-points id).
# Ids read off the links on any event page; they are stable across the season.
# Manufacturers (23/17/24) are deliberately absent — /standings/manufacturers
# computes those from results and has no adjustments to miss.
CHAMPIONSHIPS = [
    ("MX", "450", 21),
    ("MX", "250", 22),
    ("MX", "WMX", 25),
    ("SX", "450", 16),
    ("SX", "250 West", 14),
    ("SX", "250 East", 15),
    # The SMX playoffs don't run until September; harmless until they do.
    ("SMX", "450", 19),
    ("SMX", "250", 18),
]


def match_key(name: str) -> str:
    """A name reduced to what two sources can be expected to agree on.

    Punctuation and spacing are exactly what they disagree about: results give
    us "R J Hampshire" while the standings page writes "R.J. Hampshire". Strip
    everything that isn't a letter or digit and both become "rjhampshire".
    ``fold`` first, so "Cornelius Tøndel" matches "Cornelius Tondel".
    """
    return re.sub(r"[^a-z0-9]", "", fold(name or ""))


def _cell_int(text):
    try:
        return int((text or "").replace(",", "").strip())
    except ValueError:
        return None


def parse_series_points(html: str):
    """Rows of {position, rider, points, adjustment} from a standings page.

    Pure, so it can be tested without the network. Columns are located by their
    header rather than by index — the provider varies the leading columns
    between views, and a fixed index is how a parser starts quietly reading the
    wrong number.
    """
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header = [c.get_text(strip=True).upper()
                  for c in rows[0].find_all(["th", "td"])]
        if "RIDER" not in header or "POINTS" not in header:
            continue
        ri, pi = header.index("RIDER"), header.index("POINTS")
        ai = header.index("POINT ADJUSTMENTS") if "POINT ADJUSTMENTS" in header else None
        out = []
        for tr in rows[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all("td")]
            if len(cells) <= max(ri, pi):
                continue
            points = _cell_int(cells[pi])
            name = cells[ri].strip()
            if points is None or not name:
                continue          # section headers and spacer rows
            out.append({
                "position": _cell_int(cells[0]) if cells else None,
                "rider": name,
                "points": points,
                "adjustment": (_cell_int(cells[ai]) or 0)
                              if ai is not None and len(cells) > ai else 0,
            })
        if out:
            return out
    return []


def fetch_standings(series_points_id: int, event_id, timeout: int = 30):
    """One championship's official table. Returns [] rather than raising, so a
    provider hiccup degrades to our computed standings instead of an outage."""
    try:
        resp = requests.get(
            _URL.format(sid=series_points_id, eid=event_id),
            headers=_UA, timeout=timeout)
        resp.raise_for_status()
        return parse_series_points(resp.text)
    except Exception:
        return []
