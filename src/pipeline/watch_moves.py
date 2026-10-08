"""Rider moves, from the news MXT already collects. Run daily.

Between seasons riders sign with new teams, step up classes, part ways and
retire. The official entry lists only arrive a few days before each round, so
until January the first word is an announcement, and the eight outlets in
`sources` carry them within hours (Hampshire to TLD Red Bull Ducati was in
four of them the day it was announced).

Every headline from the last WINDOW_DAYS days is checked for a rider we know:

    rumour    "RUMOUR:", "expected", "linked", a question mark: never applied
    signing   signs / joins / inks / re-signs / extends, AND names a team we
              recognise: applied as source 'news' once two or more outlets
              report it (src/rider_seasons.py: entry lists and results still
              outrank it)
    pending   everything else that mentions a move (a departure, a team we do
              not recognise, a single outlet): waits for a person
    known     a signing for a rider who already has that season's team (from
              a person, an entry list or results): left alone

Nothing unconfirmed ever reaches the app. Each decision is kept in
rider_move_candidates, so the pending ones can be reviewed:

    python -m src.pipeline.watch_moves            # scan and apply
    python -m src.pipeline.watch_moves --dry-run  # show what it would do
    python -m src.pipeline.watch_moves --pending  # what is waiting for a person
"""

import argparse
import datetime
import logging
import re
import sys
from collections import defaultdict

from ..adapters.results_html import manufacturer_from_team
from ..db import get_connection
from ..names import fold
from .. import rider_seasons

log = logging.getLogger("moto.moves")

WINDOW_DAYS = 45
MIN_OUTLETS = 2

_SIGN = re.compile(r"\b(signs?|signed|signing|joins?|joined|inks?|inked|"
                   r"re-?signs?|re-?signed|extends?|extension|returns? to|"
                   r"moves? to|headed to)\b", re.I)
_LEAVE = re.compile(r"\b(parts? ways|part ways|leav(es|ing)|depart(s|ure|ing)?|"
                    r"retire(s|d|ment)?|retiring|not return(ing)?|free agents?|"
                    r"without a ride)\b", re.I)
_RUMOUR = re.compile(r"\b(rumou?rs?|reportedly|expected|could|might|linked|"
                     r"likely|set to|in talks)\b|\?", re.I)

# Short names the press uses for teams we know by their full name. Only teams
# whose short name is unambiguous; anything else goes to a person.
_TEAM_ALIASES = {
    "star racing yamaha": "Monster Energy Star Racing Yamaha",
    "star yamaha": "Monster Energy Star Racing Yamaha",
    "pro circuit kawasaki": "Monster Energy Pro Circuit Kawasaki",
    "red bull ktm": "Red Bull KTM Factory Racing",
    "troy lee designs red bull ducati": "Troy Lee Designs Red Bull Ducati Factory Racing",
    "tld red bull ducati": "Troy Lee Designs Red Bull Ducati Factory Racing",
    "tld ducati": "Troy Lee Designs Red Bull Ducati Factory Racing",
    "honda hrc": "Honda HRC Progressive",
    "triumph factory racing": "5.11 Triumph Racing Factory Team",
    "triumph racing factory team": "5.11 Triumph Racing Factory Team",
    "team tedder husqvarna": "Team Tedder Husqvarna",
    "quad lock honda": "Quad Lock Honda",
    "quadlock honda": "Quad Lock Honda",
}


def _key(text):
    return re.sub(r"[^a-z0-9]", "", fold(text or ""))


def next_season(today=None):
    """Announcements from September on are about next season."""
    today = today or datetime.date.today()
    return today.year + 1 if today.month >= 9 else today.year


def _teams(cur):
    """{normalised name: canonical} for every team we have seen, plus aliases.
    Generic names ("Yamaha") are useless for recognising a team, so a known
    team must be two words and eight characters at least."""
    cur.execute("SELECT DISTINCT team FROM rider_seasons WHERE team IS NOT NULL "
                "UNION SELECT DISTINCT team FROM riders WHERE team IS NOT NULL")
    out = {}
    for (team,) in cur.fetchall():
        if len(team) >= 8 and len(team.split()) >= 2:
            out[fold(team)] = team
    # The press's short names map to the spelling the series' entry lists use,
    # even where some rider's team was once stored under the short one.
    for alias, canonical in _TEAM_ALIASES.items():
        out[alias] = canonical
    return out


def _team_in(text, teams):
    """The longest known team named in `text`, or None."""
    low = fold(text or "")
    best = None
    for name, canonical in teams.items():
        if name in low and (best is None or len(name) > len(best[0])):
            best = (name, canonical)
    return best[1] if best else None


def classify(title, summary, teams):
    """(kind, team) for one article about one rider."""
    if _RUMOUR.search(title or ""):
        return "rumour", None
    if _SIGN.search(title or ""):
        return "signing", _team_in(f"{title} {summary or ''}", teams)
    if _LEAVE.search(title or "") or _LEAVE.search(summary or ""):
        return "departure", None
    return None, None


def scan(conn, today=None, dry_run=False):
    season = next_season(today)
    with conn.cursor() as cur:
        teams = _teams(cur)
        # Riders who raced a championship class this season or last: the
        # people MXT shows. A name we have never seen race is not ours to track.
        cur.execute(
            """
            SELECT DISTINCT r.id, r.full_name, rs.class
            FROM riders r JOIN rider_seasons rs ON rs.rider_id = r.id
            WHERE rs.year >= %s
            """, (season - 1,))
        riders = {}
        for rid, name, cls in cur.fetchall():
            if len(_key(name)) >= 8:      # too short a name matches too much
                riders[rid] = (name, _key(name), cls)
        cur.execute(
            """
            SELECT id, source_id, title, summary, url
            FROM news_articles
            WHERE coalesce(published_at, fetched_at) > now() - make_interval(days => %s)
            """, (WINDOW_DAYS,))
        articles = cur.fetchall()

    found = []   # (article_id, rider_id, kind, team, class, source_id, title, url)
    for aid, source_id, title, summary, url in articles:
        tkey = _key(title)
        low = fold(title or "")
        for rid, (name, nkey, cls) in riders.items():
            # The whole name, AND the surname as a word: "Kelley" alone must
            # not make Ben Kelley's off-road deal into Derek Kelley's.
            words = [w for w in name.split() if w.lower().strip(".") not in
                     ("jr", "sr", "ii", "iii", "iv")]
            surname = fold(words[-1]) if words else ""
            if nkey not in tkey or not re.search(rf"\b{re.escape(surname)}\b", low):
                continue
            kind, team = classify(title, summary, teams)
            if not kind:
                continue
            c = re.search(r"\b(450|250)\b", title or "")
            found.append((aid, rid, kind, team, c.group(1) if c else cls,
                          source_id, title, url))

    # A signing goes in when enough different outlets report the same rider
    # joining the same team.
    outlets = defaultdict(set)
    for aid, rid, kind, team, _c, src, _t, _u in found:
        if kind == "signing" and team:
            outlets[(rid, team)].add(src)
    applied, decided = [], []
    for aid, rid, kind, team, cls, src, title, url in found:
        if kind == "rumour":
            status = "rumour"
        elif kind == "signing" and team and len(outlets[(rid, team)]) >= MIN_OUTLETS:
            status = "applied"
        else:
            status = "pending"
        decided.append((aid, rid, season, kind, team, cls, status))
        if status == "applied" and (rid, team) not in {(a[0], a[2]) for a in applied}:
            applied.append((rid, cls, team, title, url))

    # Only riders with no team yet for that season. A second move, or one that
    # disagrees with a move already recorded (by hand, from an entry list or
    # from results), is for a person to look at.
    with conn.cursor() as cur:
        cur.execute("SELECT rider_id FROM rider_seasons WHERE year = %s", (season,))
        have = {r[0] for r in cur.fetchall()}
    for i, (aid, rid, s_, kind, team, cls, status) in enumerate(decided):
        if status == "applied" and rid in have:
            decided[i] = (aid, rid, s_, kind, team, cls, "known")
    applied = [a for a in applied if a[0] not in have]

    if dry_run:
        for aid, rid, _s, kind, team, cls, status in decided:
            print(f"  {status:8} {kind:9} {riders[rid][0]:<22} {team or '-':<45} {cls}")
        return applied, decided

    with conn.cursor() as cur:
        for row in decided:
            cur.execute(
                """
                INSERT INTO rider_move_candidates
                    (article_id, rider_id, season, kind, team, class, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (article_id, rider_id) DO UPDATE
                    SET kind = EXCLUDED.kind, team = EXCLUDED.team,
                        class = EXCLUDED.class, status = EXCLUDED.status
                """, row)
    for rid, cls, team, title, url in applied:
        n = rider_seasons.record(
            conn, season, [(rid, None, team, manufacturer_from_team(team), cls)],
            "news", note=f"auto: {title} ({url})")
        if n:
            log.info("moves: %s -> %s (%s)", riders[rid][0], team, season)
    conn.commit()
    return applied, decided


def pending(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.full_name, c.kind, c.team, a.title, a.url, c.seen_at::date
            FROM rider_move_candidates c
            JOIN riders r ON r.id = c.rider_id
            JOIN news_articles a ON a.id = c.article_id
            WHERE c.status = 'pending'
            ORDER BY c.seen_at DESC
            """)
        return cur.fetchall()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pending", action="store_true")
    args = ap.parse_args()
    with get_connection() as conn:
        if args.pending:
            for row in pending(conn):
                print("  ", " | ".join(str(x) for x in row))
            return
        applied, decided = scan(conn, dry_run=args.dry_run)
    print(f"moves: {len(decided)} mention(s), {len(applied)} applied")


if __name__ == "__main__":
    main()
