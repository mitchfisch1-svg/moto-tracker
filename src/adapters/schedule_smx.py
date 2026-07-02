"""Schedule adapter for supermotocross.com.

Also captures each event's US broadcast listings (label, ET time, providers)
from the card's `.broadcast-options` block into events.broadcast as JSON.

Pulls the full SX / MX / SMX calendar from the official schedule page and
upserts each event into the `events` table, keyed on (season_id, round_number).

The page is server-rendered HTML (WordPress). Each event is a `div.event-item`
whose CSS classes encode the discipline, e.g.:

    <div class="event-item upcoming mx"> ... </div>

with child fields: p.round, h3.venue, p.location, h3.date, h4.time, and an
optional results link (a[href*=view_event]).
"""

import datetime
import json
import re
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ..db import upsert
from .base import BaseAdapter

SCHEDULE_URL = "https://www.supermotocross.com/schedule/"

# A real, identifiable User-Agent (polite scraping for a personal project).
USER_AGENT = "MotoTracker/0.1 (personal project; +https://github.com/)"

# Map the discipline CSS token on each card to our series abbreviation.
SERIES_BY_DISCIPLINE = {"sx": "SX", "mx": "MX", "smx": "SMX"}

# Card status token -> events.status value.
STATUS_BY_TOKEN = {
    "past": "final",
    "completed": "final",
    "live": "live",
    "upcoming": "scheduled",
}
_STATUS_TOKENS = {"past", "completed", "live", "upcoming"}
_KNOWN_CARD_TOKENS = {"event-item"} | _STATUS_TOKENS

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Broadcast timezone abbreviations -> IANA zones (zoneinfo handles DST).
TZ_BY_ABBR = {
    "et": "America/New_York",
    "ct": "America/Chicago",
    "mt": "America/Denver",
    "pt": "America/Los_Angeles",
}

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)\s*([a-z]{2})", re.I)
_DATE_RE = re.compile(r"(\d{1,2})\s*([a-z]{3})", re.I)
_INT_RE = re.compile(r"\d+")
_REGION_E_RE = re.compile(r"250\s*(?:SX)?\s*E\b", re.I)
_REGION_W_RE = re.compile(r"250\s*(?:SX)?\s*W\b", re.I)


# Provider logo filename fragment -> display name.
PROVIDERS_BY_LOGO = {
    "peacock": "Peacock",
    "logo-nbc": "NBC",
    "cnbc": "CNBC",
    "logo-usa": "USA Network",
    "sirius": "SiriusXM",
    "logo-smx-vp": "SMX Video Pass",
}

_ET_TIME_RE = re.compile(r"(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\s*ET", re.I)


def parse_broadcast(card):
    """Extract US broadcast listings from an event card, as a JSON string.

    Returns e.g. '[{"label": "Gate Drop", "time_et": "1 p.m.",
    "providers": ["Peacock", "SiriusXM"]}]' or None when the card has no
    broadcast block (e.g. SMX playoffs before info is announced).
    """
    block = card.find(class_="broadcast-options")
    if not block:
        return None

    listings = []
    for opt in block.find_all(class_="broadcast-option"):
        classes = opt.get("class", [])
        if "us-only" not in classes:
            continue  # keep the US listings (they carry the ET times)
        text = opt.get_text(" ", strip=True)
        m = _ET_TIME_RE.search(text)
        label = text[: m.start()].strip(" |") if m else text.strip(" |")
        label = re.sub(r"^English only\s*\|?\s*", "", label, flags=re.I).strip()
        providers = []
        for img in opt.find_all("img"):
            src = (img.get("src") or "").lower()
            for frag, name in PROVIDERS_BY_LOGO.items():
                if frag in src and name not in providers:
                    providers.append(name)
        listings.append(
            {
                "label": label or None,
                "time_et": m.group(1).replace(".", "").lower() if m else None,
                "providers": providers,
            }
        )
    return json.dumps(listings) if listings else None


def parse_250_region(race_type):
    """From a card's race-type text, find the 250 region: 'E', 'W', 'EW', or None.

    e.g. '450SX / 250SX W' -> 'W'; '450SX / 250 E/W SHOWDOWN' -> 'EW'.
    """
    if not race_type:
        return None
    up = race_type.upper()
    if "E/W" in up or "SHOWDOWN" in up:
        return "EW"
    if _REGION_E_RE.search(up):
        return "E"
    if _REGION_W_RE.search(up):
        return "W"
    return None


class ScheduleSMXAdapter(BaseAdapter):
    name = "schedule_smx"

    def __init__(self, url: str = SCHEDULE_URL):
        self.url = url

    # --- fetch -------------------------------------------------------------
    def fetch(self) -> str:
        resp = requests.get(
            self.url, headers={"User-Agent": USER_AGENT}, timeout=30
        )
        resp.raise_for_status()
        resp.encoding = resp.encoding or "utf-8"
        return resp.text

    # --- normalize ---------------------------------------------------------
    def normalize(self, raw: str) -> list[dict]:
        soup = BeautifulSoup(raw, "html.parser")
        cards = soup.select(".event-item")
        if not cards:
            raise RuntimeError(
                "No .event-item cards found — the page structure may have "
                "changed. Consider the racerxonline.com fallback."
            )

        parsed: list[dict] = []
        for card in cards:
            classes = card.get("class", [])
            discipline = next(
                (c for c in classes if c in SERIES_BY_DISCIPLINE), None
            )
            if not discipline:
                continue  # not a race card we recognize

            status_token = next(
                (c for c in classes if c in STATUS_BY_TOKEN), "upcoming"
            )

            round_label = self._text(card, "p", "round")
            venue = self._text(card, "h3", "venue")
            location = self._text(card, "p", "location")
            date_text = self._text(card, None, "date")
            time_text = self._text(card, None, "time")
            race_type = self._text(card, None, "race-type")

            city, state = self._split_location(location)
            event_date = self._parse_date(date_text)
            start_utc = self._parse_start_utc(event_date, time_text)

            link = card.find("a", href=lambda h: h and "view_event" in h)
            source_url = link["href"] if link else self.url

            int_match = _INT_RE.search(round_label or "")
            round_int = int(int_match.group()) if int_match else None

            parsed.append(
                {
                    "series_abbrev": SERIES_BY_DISCIPLINE[discipline],
                    "round_int": round_int,
                    "round_label": round_label,
                    "region_250": parse_250_region(race_type),
                    "broadcast": parse_broadcast(card),
                    "venue": venue,
                    "city": city,
                    "state": state,
                    "event_date": event_date,
                    "start_time_utc": start_utc,
                    "status": STATUS_BY_TOKEN[status_token],
                    "source_url": source_url,
                }
            )

        self._assign_missing_round_numbers(parsed)
        return parsed

    # --- upsert ------------------------------------------------------------
    def upsert(self, conn, rows: list[dict]) -> int:
        # Resolve series abbrev -> season_id for the current year.
        season_by_abbrev = self._season_ids(conn)

        now = datetime.datetime.now(datetime.timezone.utc)
        db_rows = []
        for r in rows:
            season_id = season_by_abbrev.get(r["series_abbrev"])
            if season_id is None:
                continue  # series not seeded — skip rather than fail
            db_rows.append(
                {
                    "season_id": season_id,
                    "round_number": r["round_number"],
                    "round_label": r["round_label"],
                    "region_250": r["region_250"],
                    "broadcast": r["broadcast"],
                    "venue": r["venue"],
                    "city": r["city"],
                    "state": r["state"],
                    "event_date": r["event_date"],
                    "start_time_utc": r["start_time_utc"],
                    "status": r["status"],
                    "source_url": r["source_url"],
                    "updated_at": now,
                }
            )

        return upsert(
            conn,
            "events",
            db_rows,
            conflict_cols=["season_id", "round_number"],
            update_cols=[
                "round_label", "region_250", "broadcast", "venue", "city",
                "state", "event_date", "start_time_utc", "status",
                "source_url", "updated_at",
            ],
        )

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _text(card, tag, cls):
        el = card.find(tag, class_=cls) if tag else card.find(class_=cls)
        return el.get_text(" ", strip=True) if el else None

    @staticmethod
    def _split_location(location):
        if not location or "," not in location:
            return location, None
        city, state = location.rsplit(",", 1)
        return city.strip(), state.strip()

    @staticmethod
    def _parse_date(date_text):
        if not date_text:
            return None
        m = _DATE_RE.search(date_text)
        if not m:
            return None
        day = int(m.group(1))
        month = MONTHS.get(m.group(2).lower())
        if not month:
            return None
        # The schedule page has no year; the championship runs within one
        # calendar year, so use the current year.
        year = datetime.date.today().year
        try:
            return datetime.date(year, month, day)
        except ValueError:
            return None

    @staticmethod
    def _parse_start_utc(event_date, time_text):
        if not event_date or not time_text:
            return None
        m = _TIME_RE.search(time_text)
        if not m:
            return None
        hour, minute, ampm, tz_abbr = m.groups()
        hour, minute = int(hour), int(minute)
        if ampm.lower() == "pm" and hour != 12:
            hour += 12
        elif ampm.lower() == "am" and hour == 12:
            hour = 0
        zone = TZ_BY_ABBR.get(tz_abbr.lower())
        if not zone:
            return None
        local = datetime.datetime(
            event_date.year, event_date.month, event_date.day,
            hour, minute, tzinfo=ZoneInfo(zone),
        )
        return local.astimezone(datetime.timezone.utc)

    @staticmethod
    def _assign_missing_round_numbers(parsed):
        """Give every event a unique round_number within its series.

        Most labels contain an integer ('Round 22' -> 22). A few don't
        ('World Championship Final'); those get the next free number after the
        max integer seen in that series, in document order.
        """
        from collections import defaultdict

        groups = defaultdict(list)
        for row in parsed:
            groups[row["series_abbrev"]].append(row)

        for rows in groups.values():
            max_int = max(
                (r["round_int"] for r in rows if r["round_int"] is not None),
                default=0,
            )
            next_num = max_int + 1
            for r in rows:
                if r["round_int"] is not None:
                    r["round_number"] = r["round_int"]
                else:
                    r["round_number"] = next_num
                    next_num += 1

    @staticmethod
    def _season_ids(conn):
        """Return {series_abbrev: season_id} for the current year."""
        year = datetime.date.today().year
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.abbrev, se.id
                FROM seasons se
                JOIN series s ON s.id = se.series_id
                WHERE se.year = %s
                """,
                (year,),
            )
            return {abbrev: sid for abbrev, sid in cur.fetchall()}
