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

from ..makes import bike_in_cell
from ..names import fold

RESULTS_HOME = "https://results.supermotocross.com/results/"
_URL = RESULTS_HOME + "?p=view_series_points&id={sid}&event_id={eid}"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; MotoTracker/1.0; "
                     "+https://motoxtracker.com)"}

# Which table id is which championship, per season, lives in src/series_tables.py:
# found by each table's own heading, with 2026's hand-checked ids as a seed.
# fetch_standings still refuses a page whose heading names another season.


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


_ROUND_COL_RE = re.compile(r"^\d+:")
_FINISH_RE = re.compile(r"^(\d+)(st|nd|rd|th)$", re.I)


def parse_series_points(html: str):
    """Rows of {position, rider, points, adjustment, wins, podiums, finishes,
    bike} from a standings page.

    Pure, so it can be tested without the network. Columns are located by their
    header rather than by index — the provider varies the leading columns
    between views, and a fixed index is how a parser starts quietly reading the
    wrong number.

    wins/podiums are ROUND results: each round cell reads "<points> <round
    finish> <race lines…>". In motocross that is the Overall, not a moto —
    counting moto wins gave Hunter Lawrence 12 "wins" in an 11-round season
    (the official count is 6). None when the page has no round columns.
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
        round_idx = [i for i, h in enumerate(header) if _ROUND_COL_RE.match(h)]
        bi = header.index("BIKE") if "BIKE" in header else None
        out = []
        for tr in rows[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all("td")]
            # Each round cell nests its own table of moto lines, so only the
            # row's direct cells line up with the header.
            tds = tr.find_all(["th", "td"], recursive=False)
            if len(cells) <= max(ri, pi):
                continue
            points = _cell_int(cells[pi])
            name = cells[ri].strip()
            if points is None or not name:
                continue          # section headers and spacer rows
            finishes = []
            for i in round_idx:
                toks = tds[i].get_text(" ", strip=True).split() if i < len(tds) else []
                m = _FINISH_RE.match(toks[1]) if len(toks) >= 2 else None
                if m:
                    finishes.append(int(m.group(1)))
            out.append({
                "_finishes": finishes,
                "bike": bike_in_cell(tds[bi]) if bi is not None and bi < len(tds) else None,
                "position": _cell_int(cells[0]) if cells else None,
                "rider": name,
                "points": points,
                "adjustment": (_cell_int(cells[ai]) or 0)
                              if ai is not None and len(cells) > ai else 0,
            })
        # No round finish anywhere on the page: the counts are unknown, not 0.
        known = any(r["_finishes"] for r in out)
        for r in out:
            f = r.pop("_finishes")
            r["wins"] = sum(1 for x in f if x == 1) if known else None
            r["podiums"] = sum(1 for x in f if x <= 3) if known else None
            r["finishes"] = f if known else None     # each round's finish
        if out:
            return out
    return []


def page_title(html: str) -> str:
    """The page's own heading, e.g. "2026 SX 450 Championship"."""
    h = BeautifulSoup(html or "", "html.parser").find(["h1", "h2", "h3"])
    return h.get_text(" ", strip=True) if h else ""


def fetch_standings(series_points_id: int, event_id, season=None,
                    timeout: int = 30):
    """One championship's official table. Returns [] rather than raising, so a
    provider hiccup degrades to our computed standings instead of an outage.
    With `season`, a page whose heading does not name that season is refused:
    last year's table under this year's id is the wrong answer, not a stale
    one."""
    try:
        resp = requests.get(
            _URL.format(sid=series_points_id, eid=event_id),
            headers=_UA, timeout=timeout)
        resp.raise_for_status()
        if season is not None and str(season) not in page_title(resp.text):
            return []
        return parse_series_points(resp.text)
    except Exception:
        return []
