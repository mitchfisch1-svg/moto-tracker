"""Results adapter: parse official race results into sessions + results.

For one event it:
  1. reads the event page (view_event) -> the list of races with labels
  2. keeps the championship points races (Mains / Motos in 450 & 250)
  3. parses each race's finishing-order table
  4. resolves each rider name to a canonical rider (see resolve.riders)
  5. upserts a `sessions` row and its `results` rows

See docs/live-timing-api.md for how this source was chosen.
"""

import re
import time

import requests
from bs4 import BeautifulSoup

from ..db import upsert
from ..resolve.riders import RiderResolver
from ..standings import points_for

USER_AGENT = "MotoTracker/0.1 (personal project; +https://github.com/)"
REQUEST_DELAY_SECONDS = 1.0

BASE = "https://results.supermotocross.com/results/"

# Classes we score, and the session types we pull for points.
# 'tc_race' = one race of a Supercross Triple Crown (combined into an overall).
SCORING_CLASSES = {"450", "250"}
POINTS_TYPES = {"main", "moto", "tc_race"}

_RESULT_HEADER = ["POS", "#", "BIKE", "RIDER"]
_POS_RE = re.compile(r"^(\d+|DNF|DNS|DSQ|DNQ)$", re.I)
_LAPS_RE = re.compile(r"^(\d+)\s*/")
_TC_RACE_RE = re.compile(r"RACE\s*#?\s*\d")

# Bike makes we recognize inside team names. When several appear ("Troy Lee
# Designs Red Bull GasGas"), the make is conventionally last, so we keep the
# one that appears furthest into the string.
MANUFACTURERS = [
    "KTM", "Honda", "Yamaha", "Kawasaki", "Suzuki", "GasGas", "GASGAS",
    "Husqvarna", "Ducati", "Triumph", "Beta", "Stark",
]


def manufacturer_from_team(team):
    """Best-effort bike make from a team name, or None."""
    if not team:
        return None
    up = team.upper()
    best, best_pos = None, -1
    for make in MANUFACTURERS:
        pos = up.rfind(make.upper())
        if pos > best_pos:
            best, best_pos = make, pos
    return "GasGas" if best == "GASGAS" else best


def classify(label: str):
    """Map a race label to (class, type). Returns (None, None) if unrecognized."""
    up = label.upper()
    if up.startswith("450"):
        cls = "450"
    elif up.startswith("250"):
        cls = "250"
    elif "WMX" in up:
        cls = "WMX"
    elif "SMX NEXT" in up:
        cls = "SMX Next"
    else:
        cls = None

    if "MAIN" in up:
        typ = "main"
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


class ResultsHTMLAdapter:
    name = "results_html"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    # --- fetching ----------------------------------------------------------
    def _get_soup(self, url):
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def list_points_races(self, smx_id):
        """Return [(race_id, label, class, type)] for the scoring races only."""
        soup = self._get_soup(f"{BASE}?p=view_event&id={smx_id}")
        seen, races = set(), []
        for a in soup.find_all("a", href=re.compile(r"view_race_result&id=\d+")):
            href = a.get("href", "")
            if "export=pdf" in href:
                continue
            rid = re.search(r"id=(\d+)", href).group(1)
            if rid in seen:
                continue
            seen.add(rid)
            label = a.get_text(" ", strip=True)
            cls, typ = classify(label)
            if cls in SCORING_CLASSES and typ in POINTS_TYPES:
                races.append((rid, label, cls, typ))
        return races

    def parse_race_results(self, race_id):
        """Return a list of finishing rows for one race."""
        soup = self._get_soup(f"{BASE}?p=view_race_result&id={race_id}")
        table = self._find_results_table(soup)
        if table is None:
            return []

        rows = []
        for tr in table.find_all("tr"):
            cells = [
                c.get_text(" ", strip=True)
                for c in tr.find_all(["th", "td"], recursive=False)
            ]
            if len(cells) < 9 or not _POS_RE.match(cells[0] or ""):
                continue
            pos_raw = cells[0]
            laps_match = _LAPS_RE.match(cells[6] or "")
            rows.append(
                {
                    "position": int(pos_raw) if pos_raw.isdigit() else None,
                    "bike_number": (cells[1] or "").strip() or None,
                    "name": cells[3],
                    "laps": int(laps_match.group(1)) if laps_match else None,
                    "status": "finished" if pos_raw.isdigit() else pos_raw.lower(),
                    "hometown": re.sub(r"\s+", " ", cells[7]).strip() or None,
                    "team": (cells[8] or "").strip() or None,
                }
            )
        return rows

    @staticmethod
    def _find_results_table(soup):
        for table in soup.find_all("table"):
            first = table.find("tr")
            if not first:
                continue
            header = [
                c.get_text(" ", strip=True).upper()
                for c in first.find_all(["th", "td"])
            ]
            if header[:4] == _RESULT_HEADER:
                return table
        return None

    # --- ingest one event --------------------------------------------------
    def ingest_event(self, conn, event, resolver=None):
        """Parse + store every points race for one event.

        `event` is a dict with: event_id, season_id, series_abbrev, smx_id, label.
        Pass a shared `resolver` to reuse the rider cache across events.
        """
        resolver = resolver or RiderResolver(conn)
        races = self.list_points_races(event["smx_id"])
        normal = [r for r in races if r[3] in ("main", "moto")]
        tc_races = [r for r in races if r[3] == "tc_race"]
        total_results = 0
        profiles = {}  # rider_id -> (team, manufacturer), latest wins

        for i, (race_id, label, cls, typ) in enumerate(normal):
            parsed = self.parse_race_results(race_id)
            session_id = self._upsert_session(
                conn, event["event_id"], cls, typ, label,
                f"{BASE}?p=view_race_result&id={race_id}",
            )
            result_rows = []
            for r in parsed:
                rider_id, _action = resolver.resolve(
                    r["name"], r["bike_number"], context=f"{event['label']} {label}"
                )
                if rider_id is not None and r["team"]:
                    profiles[rider_id] = (
                        r["team"], manufacturer_from_team(r["team"]), r["hometown"]
                    )
                result_rows.append(
                    {
                        "session_id": session_id,
                        "rider_id": rider_id,
                        "position": r["position"],
                        "points": points_for(event["series_abbrev"], r["position"]),
                        "laps": r["laps"],
                        "status": r["status"],
                    }
                )
            written = upsert(
                conn, "results", result_rows,
                conflict_cols=["session_id", "rider_id"],
                update_cols=["position", "points", "laps", "status"],
            )
            total_results += written
            print(f"  [{event['label']}] {label}: {written} results")
            time.sleep(REQUEST_DELAY_SECONDS)

        # Triple Crown: combine each class's three races into one overall result.
        by_class = {}
        for race_id, label, cls, typ in tc_races:
            by_class.setdefault(cls, []).append((race_id, label))
        for cls, race_list in by_class.items():
            total_results += self._ingest_triple_crown(
                conn, event, cls, race_list, resolver, profiles
            )

        self._update_rider_profiles(conn, profiles)
        return total_results

    @staticmethod
    def _update_rider_profiles(conn, profiles):
        """Refresh riders' team + manufacturer from the latest parsed results."""
        if not profiles:
            return
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE riders SET team = %s, manufacturer = %s, hometown = %s "
                "WHERE id = %s",
                [(team, make, home, rid)
                 for rid, (team, make, home) in profiles.items()],
            )

    def _ingest_triple_crown(self, conn, event, cls, race_list, resolver, profiles):
        """Combine the 3 TC races for a class into one overall main result.

        AMA scoring: rank riders by total of finishing positions across the
        three races (lowest wins), tie-broken by finish in the last race; then
        award championship points by that overall position.
        """
        race_list.sort(key=lambda rl: rl[1])  # Race #1, #2, #3
        per_race = []  # list of {rider_id: position}
        for race_id, label in race_list:
            parsed = self.parse_race_results(race_id)
            pos_by_rider = {}
            for r in parsed:
                rider_id, _ = resolver.resolve(
                    r["name"], r["bike_number"],
                    context=f"{event['label']} {label}",
                )
                if rider_id is not None and r["team"]:
                    profiles[rider_id] = (
                        r["team"], manufacturer_from_team(r["team"]), r["hometown"]
                    )
                if rider_id is not None and r["position"]:
                    pos_by_rider[rider_id] = r["position"]
            per_race.append(pos_by_rider)
            time.sleep(REQUEST_DELAY_SECONDS)

        if not per_race:
            return 0

        field_size = max((len(p) for p in per_race), default=0)
        penalty = field_size + 1
        riders = set().union(*[p.keys() for p in per_race])
        ranking = []
        for rider in riders:
            positions = [p.get(rider, penalty) for p in per_race]
            last = per_race[-1].get(rider, penalty)
            ranking.append((sum(positions), last, rider))
        ranking.sort()

        session_id = self._upsert_session(
            conn, event["event_id"], cls, "main",
            f"{cls} Triple Crown Overall",
            f"{BASE}?p=view_event&id={event['smx_id']}",
        )
        result_rows = [
            {
                "session_id": session_id,
                "rider_id": rider,
                "position": i,
                "points": points_for(event["series_abbrev"], i),
                "laps": None,
                "status": "finished",
            }
            for i, (_score, _last, rider) in enumerate(ranking, start=1)
        ]
        written = upsert(
            conn, "results", result_rows,
            conflict_cols=["session_id", "rider_id"],
            update_cols=["position", "points", "laps", "status"],
        )
        print(f"  [{event['label']}] {cls} Triple Crown Overall: {written} results")
        return written

    @staticmethod
    def _upsert_session(conn, event_id, cls, typ, label, source_url):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (event_id, class, type, label)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (event_id, class, type, label)
                DO UPDATE SET label = EXCLUDED.label
                RETURNING id
                """,
                (event_id, cls, typ, label),
            )
            return cur.fetchone()[0]
