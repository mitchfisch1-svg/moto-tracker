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


def recompute_standings(conn, season_id: int | None = None) -> int:
    """Rebuild standings (points/wins/podiums/position) from results.

    If season_id is given, only that season is recomputed; otherwise all.
    Returns the number of standings rows written.
    """
    where_season = "AND se.id = %s" if season_id is not None else ""
    params = (season_id,) if season_id is not None else ()

    with conn.cursor() as cur:
        # Clear the seasons we're about to rebuild so removed results disappear.
        if season_id is not None:
            cur.execute("DELETE FROM standings WHERE season_id = %s", (season_id,))
        else:
            cur.execute("DELETE FROM standings")

        cur.execute(
            f"""
            INSERT INTO standings
                (season_id, class, rider_id, points, wins, podiums, updated_at)
            SELECT se.id,
                   s.class,
                   r.rider_id,
                   SUM(r.points)                       AS points,
                   SUM((r.position = 1)::int)          AS wins,
                   SUM((r.position <= 3)::int)         AS podiums,
                   now()
            FROM results r
            JOIN sessions s ON s.id = r.session_id
            JOIN events   e ON e.id = s.event_id
            JOIN seasons  se ON se.id = e.season_id
            WHERE s.type = ANY(%s)
              AND r.rider_id IS NOT NULL
              AND r.points IS NOT NULL
              {where_season}
            GROUP BY se.id, s.class, r.rider_id
            """,
            (list(SCORING_TYPES), *params),
        )
        written = cur.rowcount

        # Rank within each (season, class) by points.
        cur.execute(
            f"""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY season_id, class ORDER BY points DESC
                       ) AS rn
                FROM standings se
                WHERE TRUE {('AND se.season_id = %s' if season_id is not None else '')}
            )
            UPDATE standings st
            SET position = ranked.rn
            FROM ranked
            WHERE st.id = ranked.id
            """,
            params,
        )

    return written
