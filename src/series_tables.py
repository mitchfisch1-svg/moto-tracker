"""Which of the provider's championship tables is which, found by its heading.

The results site numbers every championship table —
results.supermotocross.com/results/?p=view_series_points&id=N — and the
numbers are NEW EVERY SEASON. Until 10-08 they were typed into the code once a
year in four places (CHAMPIONSHIPS, _MFR_POINTS, _SMX_PLAYOFF_POINTS,
_WMX_SEASON), and a season without them quietly fell back to standings we
compute ourselves — the numbers that were wrong all of 2026 until they were
checked against these very tables.

Now the hourly pipeline reads the heading of each new table ("2027 SX 450
Championship"), files it under (year, key), and stores the map in
scraped_session_cache under CACHE_KEY. Everything else asks by (year, key):
the overlay, the API and the audit. SEED keeps the ids a person checked by
hand, and they win over anything discovered.

The provider hands ids out in order (2025's tables are 4-13, 2026's 14-32), so
a new season's tables appear just above the highest id seen. Probing only
happens while a series that has started is missing a table, at most hourly,
and stops at a run of empty ids (which the site answers with HTTP 404).

Stdlib + requests only: the API imports this (tests/test_api_deps.py).
"""

import datetime
import json
import logging
import re
import time

import requests

log = logging.getLogger("moto.series_tables")

RESULTS_HOME = "https://results.supermotocross.com/results/"
CACHE_KEY = "series_tables"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; MotoTracker/1.0; "
                     "+https://motoxtracker.com)"}

# Checked by hand against each page, 2026. Anything for a season listed here
# that discovery disagrees with is the classifier's mistake, not the seed's.
# A table the classifier cannot file goes here too, by hand.
SEED = {2026: {
    "SX 450": 16, "SX 250 West": 14, "SX 250 East": 15, "SX MFR": 17,
    "MX 450": 21, "MX 250": 22, "MX WMX": 25, "MX MFR": 23,
    # NOT the SMX championship: the "Combined" tables are season-long SX + MX
    # points that only SEED the playoffs. The title is SMX PLAYOFF.
    "SMX 450": 19, "SMX 250": 18, "SMX MFR": 24,
    "SMX PLAYOFF 450": 30, "SMX PLAYOFF 250": 31,
}}

# What the overlay writes into `standings`: (series, our class, key).
_OVERLAY = [
    ("MX", "450", "MX 450"), ("MX", "250", "MX 250"), ("MX", "WMX", "MX WMX"),
    ("SX", "450", "SX 450"), ("SX", "250 West", "SX 250 West"),
    ("SX", "250 East", "SX 250 East"),
    ("SMX", "450", "SMX 450"), ("SMX", "250", "SMX 250"),
]

# The tables a season must have once a stage of it is under way. Only what is
# certain to exist by then: by 2026's ids SX's four and the SMX seeding tables
# came with Supercross; WMX's table only once WMX has raced (round 2 in 2026);
# and the SMX makes table came with motocross in 2026 but only with the
# playoffs in 2025, so it is required from the playoffs. A table that turns up
# earlier is filed anyway — this only decides when its absence is a fault.
_NEEDED_WHEN = {
    "SX": ["SX 450", "SX 250 West", "SX 250 East", "SX MFR", "SMX 450", "SMX 250"],
    "MX": ["MX 450", "MX 250", "MX MFR"],
    "WMX": ["MX WMX"],
    "SMX": ["SMX PLAYOFF 450", "SMX PLAYOFF 250", "SMX MFR"],
}

_PROBE_EVERY_S = 55 * 60      # hourly, whatever calls it more often
_MISSES_TO_STOP = 25          # consecutive empty ids above the highest seen
_PROBE_CAP = 80               # never more pages than this in one run

# Every word a heading of ours may contain. Anything else — Futures, Rookie,
# Team, Holeshot, Triple Crown, Seeding, Next, Amateur — and the table is left
# unfiled for a person (the audit names it), because the year matching is no
# protection: a misfiled 2027 table passes every season check there is.
_ALLOWED = {
    "SX", "SUPERCROSS", "MX", "MOTOCROSS", "SMX", "WMX", "PRO", "AMA",
    "MONSTER", "ENERGY", "450", "250", "WEST", "EAST", "CHAMPIONSHIP",
    "CHAMPIONSHIPS", "POINTS", "CLASS", "COMBINED", "PLAYOFF", "PLAYOFFS",
    "WORLD", "MANUFACTURER", "MANUFACTURERS", "S", "SERIES",
}


def _words(text):
    """Upper-case words, with the AMA's "450SX"/"250MX" split into class and
    series and "SuperMotocross" spelled the way the rest of the site does."""
    out = []
    for t in re.findall(r"[A-Z0-9]+", (text or "").upper()):
        m = re.fullmatch(r"(450|250)(SX|MX)", t)
        if m:
            out += [m.group(1), m.group(2)]
        elif t == "SUPERMOTOCROSS":
            out.append("SMX")
        else:
            out.append(t)
    return set(out)


def classify(title):
    """(year, key) for a table's heading, or None if it is not one we use.

    The provider's wording drifts between seasons — 2025 says "SMX 450
    Championship Playoffs" and "Manufacturers Points", 2026 says "SMX
    Playoffs 450 Championship" and "Manufacturers Championship" — so this
    reads words, not phrases. Anything it is unsure of is None: a table left
    unfiled costs a computed number until a person adds it to SEED; a table
    misfiled puts one championship's points on another's riders.
    """
    m = re.match(r"^\s*(20\d\d)\s+(.+)$", title or "")
    if not m:
        return None
    year = int(m.group(1))
    words = _words(m.group(2))
    if words - _ALLOWED:
        return None
    sx = bool(words & {"SX", "SUPERCROSS"})
    mx = bool(words & {"MX", "MOTOCROSS"})
    smx = "SMX" in words
    cls = [c for c in ("450", "250") if c in words]
    if words & {"MANUFACTURER", "MANUFACTURERS"}:
        # One makes table per series; a per-class or WMX one is not it.
        if cls or "WMX" in words:
            return None
        if smx:
            return year, "SMX MFR"
        if sx and not mx:
            return year, "SX MFR"
        if mx and not sx:
            return year, "MX MFR"
        return None
    if "WMX" in words:
        return (year, "MX WMX") if not (cls or sx or smx) else None
    if len(cls) != 1:
        return None
    cls = cls[0]
    if smx:
        playoff = bool(words & {"PLAYOFF", "PLAYOFFS"})
        if playoff and "COMBINED" in words:
            return None        # "Combined" is the seeding; never the title
        return year, (f"SMX PLAYOFF {cls}" if playoff else f"SMX {cls}")
    if words & {"COMBINED", "PLAYOFF", "PLAYOFFS", "WORLD"}:
        return None            # SMX words on a table that does not say SMX
    if sx and not mx:
        if cls == "450":
            return (year, "SX 450") if not words & {"WEST", "EAST"} else None
        if "WEST" in words and "EAST" not in words:
            return year, "SX 250 West"
        if "EAST" in words and "WEST" not in words:
            return year, "SX 250 East"
        return None            # a 250 table naming no one region
    if mx and not sx and not words & {"WEST", "EAST"}:
        return year, f"MX {cls}"
    return None


# --- reading the map ----------------------------------------------------------
def tables(year, found=None) -> dict:
    """key -> table id for one season: discovered, with SEED on top."""
    if year is None:
        return {}
    out = {}
    for k, v in ((found or {}).get("tables") or {}).get(str(year), {}).items():
        out[k] = int(v)
    out.update(SEED.get(int(year), {}))
    return out


def years(found=None) -> set:
    """Every season with at least one known table."""
    ys = set(SEED)
    for y, t in ((found or {}).get("tables") or {}).items():
        if t:
            ys.add(int(y))
    return ys


def table_id(year, key, found=None):
    return tables(year, found).get(key)


def championships(year, found=None) -> list:
    """(series, our class, id) for each championship the overlay applies."""
    t = tables(year, found)
    return [(s, c, t[k]) for s, c, k in _OVERLAY if k in t]


def smx_playoff_ids(year, found=None) -> dict:
    """{'450': id, '250': id} — whichever of the playoff tables exist."""
    t = tables(year, found)
    return {c: t[f"SMX PLAYOFF {c}"] for c in ("450", "250")
            if f"SMX PLAYOFF {c}" in t}


def needed(started) -> list:
    """The keys a season must have, given which stages have started
    ('SX', 'MX', 'WMX', 'SMX')."""
    out = []
    for s in ("SX", "MX", "WMX", "SMX"):
        if s in started:
            out += _NEEDED_WHEN[s]
    return out


# --- finding them (pipeline side) ---------------------------------------------
def load(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM scraped_session_cache WHERE cache_key = %s",
                    (CACHE_KEY,))
        row = cur.fetchone()
    data = row[0] if row else None
    if isinstance(data, str):
        data = json.loads(data)
    return data or {}


def _save(conn, data) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scraped_session_cache (cache_key, payload, updated_at)
            VALUES (%s, %s::jsonb, now())
            ON CONFLICT (cache_key)
            DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
            """,
            (CACHE_KEY, json.dumps(data)),
        )
    conn.commit()


def started_stages(conn, year, today=None, lead_days=7, settled_days=None) -> set:
    """Which stages of `year` are under way: 'SX', 'MX', 'SMX' by their
    rounds, and 'WMX' once a WMX race exists.

    For probing (`lead_days`): a round within a week, so the tables are looked
    for as they are posted. For judging (`settled_days`): a round final that
    many days ago, so a table posted on race night is not a fault by morning.
    """
    today = today or datetime.date.today()
    if settled_days is not None:
        cutoff, final_only = today - datetime.timedelta(days=settled_days), True
    else:
        cutoff, final_only = today + datetime.timedelta(days=lead_days), False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT s.abbrev FROM events e
            JOIN seasons se ON se.id = e.season_id
            JOIN series  s  ON s.id  = se.series_id
            WHERE se.year = %s AND e.event_date <= %s
              AND (NOT %s OR e.status = 'final')
            """,
            (year, cutoff, final_only),
        )
        out = {r[0] for r in cur.fetchall()}
        cur.execute(
            """
            SELECT 1 FROM sessions sess
            JOIN events  e  ON e.id  = sess.event_id
            JOIN seasons se ON se.id = e.season_id
            WHERE se.year = %s AND sess.class = 'WMX' AND e.event_date <= %s
            LIMIT 1
            """,
            (year, cutoff),
        )
        if cur.fetchone():
            out.add("WMX")
    return out


def _heading(html: str) -> str:
    m = re.search(r"<h[123][^>]*>(.*?)</h[123]>", html or "", re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""


def _fetch_title(table_id, timeout=20):
    """(heading, rows) for one id; ('', 0) when the id is empty. The site
    answers an empty id with HTTP 404; anything else that fails RAISES, so a
    timeout is never mistaken for "no table here"."""
    resp = requests.get(f"{RESULTS_HOME}?p=view_series_points&id={table_id}",
                        headers=_UA, timeout=timeout)
    if resp.status_code == 404:
        return "", 0
    resp.raise_for_status()
    title = _heading(resp.text)
    if not title or "404" in title or "NOT FOUND" in title.upper():
        return "", 0
    return title, resp.text.count("<tr")


def _file_into(tables_, conflicts, table_id, title):
    hit = classify(title)
    if not hit:
        return None
    year, key = hit
    season = tables_.setdefault(str(year), {})
    have = season.get(key)
    if have is not None and int(have) != int(table_id):
        pair = sorted([int(have), int(table_id)])
        if not any(x.get("ids") == pair for x in conflicts):
            conflicts.append({"year": year, "key": key, "ids": pair})
        return None
    season[key] = int(table_id)
    return hit


def file_table(data, table_id, title, rows=0):
    """Record one probed table in `data`. Returns (year, key) if it filed it.

    Two ids claiming one (year, key) keep the lower and are listed under
    `conflicts` for the audit: guessing which is real is how a championship
    ends up showing another's table.
    """
    data.setdefault("titles", {})[str(table_id)] = title
    return _file_into(data.setdefault("tables", {}),
                      data.setdefault("conflicts", []), table_id, title)


def _refile(data) -> list:
    """File every stored heading again with today's classify(), so a fix to
    the classifier takes effect without anyone editing the stored map.
    Returns [(year, key, id)] filed now that were not before."""
    old = data.get("tables") or {}
    tables_, conflicts = {}, []
    for tid in sorted((data.get("titles") or {}), key=int):
        _file_into(tables_, conflicts, tid, data["titles"][tid])
    data["tables"], data["conflicts"] = tables_, conflicts
    return [(int(y), k, i) for y, t in tables_.items() for k, i in t.items()
            if (old.get(y) or {}).get(k) != i]


def _probe(data, fetch) -> list:
    """Fetch the ids that could hold a table we have not seen: holes below the
    highest id seen, then upwards until a run of empty ids. Stops at the first
    real failure without recording anything past it, so the next run starts
    from there instead of stepping over a table it never read."""
    seed_max = max(i for t in SEED.values() for i in t.values())
    seen = {int(i) for i in (data.get("titles") or {})}
    top = max(seen | {seed_max})
    holes = [i for i in range(seed_max + 1, top) if i not in seen]
    new, probed = [], 0

    def look(tid):
        title, rows = fetch(tid)           # raises on a real failure
        if title:
            hit = file_table(data, tid, title, rows)
            if hit:
                new.append((hit[0], hit[1], tid))
        return bool(title)

    try:
        for tid in holes:
            probed += 1
            look(tid)
        misses, tid = 0, top + 1
        while misses < _MISSES_TO_STOP and probed < _PROBE_CAP:
            probed += 1
            misses = 0 if look(tid) else misses + 1
            tid += 1
    except Exception as e:
        log.warning("series tables: stopped at an unreadable page (%s); "
                    "will resume there next run", e)
    return new


def refresh(conn, today=None, force=False, fetch=_fetch_title) -> list:
    """Look for tables this season is missing. Returns [(year, key, id)] newly
    filed. Never raises: a provider hiccup means try again next hour.

    Give it a connection of its own: it commits, and on an error it rolls
    back — neither is anything a caller's half-done ingest should be part of.
    """
    try:
        today = today or datetime.date.today()
        data = load(conn)
        new = _refile(data)
        year = today.year
        want = set(needed(started_stages(conn, year, today)))
        missing = want - set(tables(year, data))
        due = force or time.time() - (data.get("probed_at") or 0) >= _PROBE_EVERY_S
        probed = bool(missing and due)
        if probed:
            new += _probe(data, fetch)
            data["probed_at"] = time.time()
        if new or probed:
            _save(conn, data)
        for y, k, i in new:
            log.info("series tables: %s %s is id %s", y, k, i)
        still = want - set(tables(year, data))
        if probed and still:
            log.info("series tables: %s still has no %s", year,
                     ", ".join(sorted(still)))
        return new
    except Exception:
        log.exception("series tables: refresh failed; will retry")
        try:
            conn.rollback()
        except Exception:
            pass
        return []
