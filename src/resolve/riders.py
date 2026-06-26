"""Rider entity resolution.

Turns a rider name parsed off a results sheet (e.g. "HAIDEN DEEGAN") into a
single canonical `riders.id`, using the cascade:

    1. known alias        (rider_aliases)         -> link
    2. exact name match   (riders.full_name)      -> link, remember alias
    3. fuzzy match         (RapidFuzz)
         score >= HIGH       -> link, remember alias
         LOW <= score < HIGH -> flag to rider_match_review (unless bike # confirms)
         score < LOW         -> create a new rider

The RiderResolver caches riders/aliases in memory and updates both the cache and
the database as it learns, so repeated names resolve instantly and the matcher
improves over a run (and across runs).
"""

import re

from rapidfuzz import fuzz, process

# Fuzzy score thresholds (0-100, RapidFuzz WRatio).
HIGH_CONFIDENCE = 90   # >= this: treat as the same rider
LOW_CONFIDENCE = 75    # below this: treat as a brand-new rider

# Award/marker tokens that appear appended to names on result sheets.
_TAG_RE = re.compile(r"\b(HOLESHOT|HOLE SHOT)\b", re.I)


def normalize_name(raw: str) -> str:
    """Canonical form used for matching: upper-case, de-tagged, punctuation-light."""
    if not raw:
        return ""
    s = raw.upper()
    s = _TAG_RE.sub(" ", s)
    s = re.sub(r"[^A-Z0-9'\- ]", " ", s)  # keep letters, digits, apostrophe, hyphen
    s = re.sub(r"\s+", " ", s).strip()
    return s


def display_name(raw: str) -> str:
    """Readable stored form, e.g. 'HUNTER LAWRENCE Holeshot' -> 'Hunter Lawrence'."""
    return normalize_name(raw).title()


class RiderResolver:
    def __init__(self, conn):
        self.conn = conn
        self.alias_to_id: dict[str, int] = {}
        self.name_to_id: dict[str, int] = {}
        self.rider_number: dict[int, str | None] = {}
        self.choices: list[str] = []      # normalized names, parallel to choice_ids
        self.choice_ids: list[int] = []
        # Counters for a run summary.
        self.created = 0
        self.linked = 0
        self.flagged = 0
        self._load()

    def _load(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT id, full_name, number FROM riders")
            for rid, full_name, number in cur.fetchall():
                norm = normalize_name(full_name)
                self.name_to_id[norm] = rid
                self.rider_number[rid] = number
                self.choices.append(norm)
                self.choice_ids.append(rid)
            cur.execute("SELECT alias, rider_id FROM rider_aliases")
            for alias, rid in cur.fetchall():
                self.alias_to_id[alias] = rid

    # --- public ------------------------------------------------------------
    def resolve(self, raw_name, bike_number=None, context=None):
        """Return (rider_id, action). rider_id is None when flagged for review."""
        norm = normalize_name(raw_name)
        if not norm:
            return None, "empty"

        if norm in self.alias_to_id:
            self.linked += 1
            return self.alias_to_id[norm], "alias"

        if norm in self.name_to_id:
            self.linked += 1
            return self.name_to_id[norm], "exact"

        if self.choices:
            choice, score, idx = process.extractOne(
                norm, self.choices, scorer=fuzz.WRatio
            )
            rid = self.choice_ids[idx]
            if score >= HIGH_CONFIDENCE:
                self._add_alias(norm, rid)
                self.linked += 1
                return rid, f"fuzzy {score:.0f}"
            if score >= LOW_CONFIDENCE:
                # A matching bike number turns a maybe into a yes.
                if bike_number and self.rider_number.get(rid) == str(bike_number):
                    self._add_alias(norm, rid)
                    self.linked += 1
                    return rid, f"fuzzy+bike {score:.0f}"
                # Too close to be sure, too far to merge: create a distinct rider
                # (so no result is lost) AND flag it as a possible duplicate of the
                # suggested rider for a human to confirm/merge later.
                self._flag_review(raw_name, rid, score, context)
                self.flagged += 1
                new_rid = self._create_rider(raw_name, bike_number)
                return new_rid, f"new+review {score:.0f}"

        rid = self._create_rider(raw_name, bike_number)
        self.created += 1
        return rid, "new"

    # --- internals ---------------------------------------------------------
    def _create_rider(self, raw_name, bike_number):
        name = display_name(raw_name)
        norm = normalize_name(raw_name)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO riders (full_name, number) VALUES (%s, %s) RETURNING id",
                (name, str(bike_number) if bike_number else None),
            )
            rid = cur.fetchone()[0]
        self.name_to_id[norm] = rid
        self.rider_number[rid] = str(bike_number) if bike_number else None
        self.choices.append(norm)
        self.choice_ids.append(rid)
        self._add_alias(norm, rid)
        return rid

    def _add_alias(self, norm, rid):
        if norm in self.alias_to_id:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rider_aliases (rider_id, alias) VALUES (%s, %s) "
                "ON CONFLICT (alias) DO NOTHING",
                (rid, norm),
            )
        self.alias_to_id[norm] = rid

    def _flag_review(self, raw_name, suggested_rid, score, context):
        # Don't pile up duplicates for the same unresolved name.
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rider_match_review (parsed_name, suggested_rider, score, context)
                SELECT %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM rider_match_review
                    WHERE parsed_name = %s AND resolved = FALSE
                )
                """,
                (raw_name, suggested_rid, score, context, raw_name),
            )
