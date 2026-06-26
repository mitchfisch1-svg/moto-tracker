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
_AMA_POINTS = {
    1: 25, 2: 22, 3: 20, 4: 18, 5: 16, 6: 15, 7: 14, 8: 13, 9: 12, 10: 11,
    11: 10, 12: 9, 13: 8, 14: 7, 15: 6, 16: 5, 17: 4, 18: 3, 19: 2, 20: 1,
}

# Per-series tables (all the same today; kept separate so they're easy to tweak).
POINTS_TABLES = {"SX": _AMA_POINTS, "MX": _AMA_POINTS, "SMX": _AMA_POINTS}

# Session types that award championship points.
SCORING_TYPES = ("main", "moto")


def points_for(series_abbrev: str, position) -> int:
    if not position:
        return 0
    table = POINTS_TABLES.get(series_abbrev, _AMA_POINTS)
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
