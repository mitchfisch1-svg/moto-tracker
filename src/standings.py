"""Championship points and standings.

`points_for()` maps a finishing position to championship points.
`recompute_standings()` rebuilds the standings table from scratch off the stored
results, so it can never drift — re-run it any time results change.

Points come only from the championship-scoring sessions:
  - Supercross: the Main Event
  - Pro Motocross: each Moto (both motos count, summed)
"""

# Standard AMA points table (position -> points), positions 1..20.
# This is the long-standing Pro Motocross / Supercross table. If any series
# uses a different value, adjust here — standings recompute from results.
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
