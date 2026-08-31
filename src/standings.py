"""Championship points and standings.

`points_for()` maps a finishing position to championship points.
`recompute_standings()` rebuilds the standings table from scratch off the stored
results, so it can never drift — re-run it any time results change.

Points come only from the championship-scoring sessions:
  - Supercross: the Main Event
  - Pro Motocross: each Moto (both motos count, summed)

`apply_official_standings()` then overlays the series' own published points on
top, because the provider applies manual penalties we cannot derive.
"""

import datetime
import re

# The points a finishing position is worth.
#
# This is NOT the classic AMA table (25-22-20-18-16-15-14-...-1 for the top 20)
# that this file used to carry. The real one goes flat 25/22/20/18 for the
# podium-plus-one and then simply pays 22 minus your position all the way down
# to 21st, which still earns a point. The difference is invisible at the front —
# a race winner scores 25 either way — and grows as you go back, which is why a
# wrong table looked almost right: after nine rounds the leader was 2 points off
# while the midfield was 16 short, and the order of 6th and 7th was reversed.
#
# Derived from, and checked against, the official standings the timing provider
# publishes at ?p=view_series_points. Recomputing every stored result with this
# table reproduces the official total for **every rider** in MX 450 (88), MX 250
# (95) and SX 450 (34) — the only remaining differences are the official
# POINT ADJUSTMENTS column (see below).
#
# 5th is the giveaway: 17, not 16. Unadilla 450 Overall, Garrett Marchbanks
# went 5-4 for an official 35 — the old table said 34.
_SMX_POINTS = {
    1: 25, 2: 22, 3: 20, 4: 18,
    5: 17, 6: 16, 7: 15, 8: 14, 9: 13, 10: 12,
    11: 11, 12: 10, 13: 9, 14: 8, 15: 7, 16: 6, 17: 5, 18: 4, 19: 3, 20: 2,
    21: 1,          # 21st still scores; 22nd and back score nothing
}

# All three series score identically — verified against the official SX 450 and
# Pro Motocross standings, not assumed. Kept per-series so a divergence is a
# one-line change.
POINTS_TABLES = {"SX": _SMX_POINTS, "MX": _SMX_POINTS, "SMX": _SMX_POINTS}

# ⚠️ NOT MODELLED: the official standings carry a POINT ADJUSTMENTS column —
# manual penalties that cannot be derived from finishing positions (e.g. Michael
# Mosiman -5 in MX 250, Jorge Prado -3 in SX 450). Ten riders across the three
# championships are affected. Everyone else matches exactly. The fix, if it ever
# matters, is to ingest ?p=view_series_points directly rather than compute.

# Session types that award championship points.
SCORING_TYPES = ("main", "moto")


def points_for(series_abbrev: str, position) -> int:
    if not position:
        return 0
    table = POINTS_TABLES.get(series_abbrev, _SMX_POINTS)
    return table.get(int(position), 0)


def standings_class(series_abbrev, cls, home_region):
    """The championship a result counts toward.

    Supercross 250 is two regional championships; everything else keeps its class.
    """
    if series_abbrev == "SX" and cls == "250":
        return {"E": "250 East", "W": "250 West"}.get(home_region, "250")
    return cls


def recompute_standings(conn, season_id: int | None = None) -> int:
    """Rebuild standings (points/wins/podiums/position) from results.

    If season_id is given, only that season is recomputed; otherwise all.
    Returns the number of standings rows written.

    Supercross 250 results are split into '250 East' / '250 West' by each rider's
    home region (the region of the non-showdown rounds they raced), so showdown
    points land in the right regional championship.
    """
    where_season = "AND se.id = %s" if season_id is not None else ""
    params = (season_id,) if season_id is not None else ()

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT se.id, s.abbrev, sess.class, e.region_250,
                   r.rider_id, r.position, r.points
            FROM results r
            JOIN sessions sess ON sess.id = r.session_id
            JOIN events   e    ON e.id    = sess.event_id
            JOIN seasons  se   ON se.id   = e.season_id
            JOIN series   s    ON s.id    = se.series_id
            WHERE sess.type = ANY(%s)
              AND r.rider_id IS NOT NULL
              AND r.points IS NOT NULL
              -- WMX standings come from the official series-points page (which
              -- includes penalty adjustments); recomputing here would disagree.
              AND sess.class <> 'WMX'
              {where_season}
            """,
            (list(SCORING_TYPES), *params),
        )
        rows = cur.fetchall()

    # Home region: the E/W of a rider's non-showdown SX 250 rounds.
    home_region: dict[int, str] = {}
    for _sid, abbrev, cls, region, rider, _pos, _pts in rows:
        if abbrev == "SX" and cls == "250" and region in ("E", "W"):
            home_region[rider] = region

    # Aggregate per (season, standings_class, rider).
    agg: dict[tuple, list] = {}
    for sid, abbrev, cls, _region, rider, position, points in rows:
        sclass = standings_class(abbrev, cls, home_region.get(rider))
        bucket = agg.setdefault((sid, sclass, rider), [0, 0, 0])
        bucket[0] += points
        bucket[1] += 1 if position == 1 else 0
        bucket[2] += 1 if position and position <= 3 else 0

    # Rank within each (season, class) by points.
    position_of: dict[tuple, int] = {}
    by_group: dict[tuple, list] = {}
    for key in agg:
        by_group.setdefault((key[0], key[1]), []).append(key)
    for group_keys in by_group.values():
        group_keys.sort(key=lambda k: agg[k][0], reverse=True)
        for rank, key in enumerate(group_keys, start=1):
            position_of[key] = rank

    with conn.cursor() as cur:
        if season_id is not None:
            cur.execute("DELETE FROM standings WHERE season_id = %s", (season_id,))
        else:
            cur.execute("DELETE FROM standings")

        for (sid, sclass, rider), (points, wins, podiums) in agg.items():
            cur.execute(
                """
                INSERT INTO standings
                    (season_id, class, rider_id, points, position, wins, podiums,
                     updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                """,
                (sid, sclass, rider, points, position_of[(sid, sclass, rider)],
                 wins, podiums),
            )

    return len(agg)


def _rider_index(conn):
    """match_key -> rider_id, from riders and their aliases.

    Ambiguous keys are dropped rather than guessed: two riders reducing to the
    same key means we cannot tell them apart, and awarding a championship total
    to the wrong one is far worse than leaving a computed figure in place.
    """
    from .adapters.official_standings import match_key
    idx, dupes = {}, set()
    with conn.cursor() as cur:
        cur.execute("SELECT id, full_name FROM riders")
        for rid, name in cur.fetchall():
            k = match_key(name)
            if not k:
                continue
            if k in idx and idx[k] != rid:
                dupes.add(k)
            idx.setdefault(k, rid)
        cur.execute("SELECT rider_id, alias FROM rider_aliases")
        for rid, alias in cur.fetchall():
            k = match_key(alias)
            if k and k not in idx:
                idx[k] = rid
    for k in dupes:
        idx.pop(k, None)
    return idx


def apply_official_standings(conn, season_id: int | None = None) -> dict:
    """Overlay the series' own published points onto our computed standings.

    Mostly an OVERLAY, not a replacement. recompute_standings() still runs
    first and still owns wins and podiums, which are unambiguous from results.
    This only corrects `points` and `position` — the two things the provider can
    state and we can only infer, because of point adjustments.

    Nothing here raises. If the provider is slow, unreachable or mid-update on a
    race afternoon, every championship simply keeps its computed figure, which
    is correct to within an adjustment. Standings going stale beats standings
    going blank.
    """
    from .adapters.official_standings import CHAMPIONSHIPS, fetch_standings, match_key

    with conn.cursor() as cur:
        # Any recent event anchors the page; the provider returns the whole
        # season for whichever championship is asked for.
        cur.execute(
            """
            SELECT e.source_url FROM events e
            JOIN seasons se ON se.id = e.season_id
            WHERE e.source_url LIKE '%%view_event%%' AND e.status = 'final'
            ORDER BY e.event_date DESC LIMIT 1
            """
        )
        row = cur.fetchone()
    if not row:
        return {"anchor": None, "applied": 0, "championships": {}}
    m = re.search(r"[?&]id=(\d+)", row[0] or "")
    if not m:
        return {"anchor": None, "applied": 0, "championships": {}}
    anchor = m.group(1)

    index = _rider_index(conn)
    report, total = {}, 0

    for abbrev, cls, sid in CHAMPIONSHIPS:
        rows = fetch_standings(sid, anchor)
        if not rows:
            report[f"{abbrev} {cls}"] = "unavailable"
            continue
        changed = matched = unmatched = inserted = 0
        with conn.cursor() as cur:
            for r in rows:
                rider_id = index.get(match_key(r["rider"]))
                if rider_id is None:
                    unmatched += 1
                    continue
                matched += 1
                cur.execute(
                    """
                    UPDATE standings st SET points = %s, position = %s
                    FROM seasons se, series s
                    WHERE st.season_id = se.id AND se.series_id = s.id
                      AND s.abbrev = %s AND st.class = %s AND st.rider_id = %s
                      AND (st.points IS DISTINCT FROM %s
                           OR st.position IS DISTINCT FROM %s)
                    """ + ("AND se.id = %s" if season_id is not None else ""),
                    (r["points"], r["position"], abbrev, cls, rider_id,
                     r["points"], r["position"])
                    + ((season_id,) if season_id is not None else ()),
                )
                changed += cur.rowcount
                if cur.rowcount == 0:
                    # No row to overlay. Either the figure already agreed, or
                    # this championship has no results of ours at all — which
                    # is exactly the SMX playoffs before they start: the series
                    # publishes where riders SIT going in, and we were fetching
                    # that table, matching all 112 riders against it, and
                    # throwing it away. wins/podiums stay 0 because we have no
                    # races for them, which is the truth, not a placeholder.
                    cur.execute(
                        """
                        INSERT INTO standings
                               (season_id, class, rider_id, points, position)
                        SELECT se.id, %s, %s, %s, %s
                        FROM seasons se JOIN series s ON s.id = se.series_id
                        WHERE s.abbrev = %s AND se.year = %s
                        ON CONFLICT (season_id, class, rider_id) DO NOTHING
                        """,
                        (cls, rider_id, r["points"], r["position"],
                         abbrev, datetime.date.today().year),
                    )
                    inserted += cur.rowcount
        conn.commit()
        total += changed
        report[f"{abbrev} {cls}"] = {
            "rows": len(rows), "matched": matched,
            "unmatched": unmatched, "updated": changed, "inserted": inserted,
        }

    return {"anchor": anchor, "applied": total, "championships": report}
