"""Who a rider raced for, season by season.

riders.team / manufacturer / number are "the latest we know". That is right
for a rider page and wrong for history: over the winter riders change teams,
step up from 250 to 450, change numbers, retire or arrive, and last season's
standings must keep showing what each rider actually raced that season. So
those facts are also kept per season, in rider_seasons, with where each came
from:

    news        an announced move, hand-checked, before any entry list
    entry_list  the series' own entry list for a round
    results     what the rider actually raced under (most trusted)

A less trusted source never overwrites a more trusted one, so an announcement
cannot undo an entry list and neither can undo a result.

Dependency-free beyond the DB connection, so the API could read it too.
"""

RANK = {"news": 1, "entry_list": 2, "results": 3}

_UPSERT = """
    INSERT INTO rider_seasons
        (rider_id, year, number, team, manufacturer, class, source, note, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
    ON CONFLICT (rider_id, year) DO UPDATE SET
        number       = COALESCE(EXCLUDED.number, rider_seasons.number),
        team         = COALESCE(EXCLUDED.team, rider_seasons.team),
        manufacturer = COALESCE(EXCLUDED.manufacturer, rider_seasons.manufacturer),
        class        = COALESCE(EXCLUDED.class, rider_seasons.class),
        source       = EXCLUDED.source,
        note         = EXCLUDED.note,
        updated_at   = now()
    WHERE (CASE rider_seasons.source WHEN 'news' THEN 1 WHEN 'entry_list' THEN 2
                                     ELSE 3 END)
       <= (CASE EXCLUDED.source WHEN 'news' THEN 1 WHEN 'entry_list' THEN 2
                                ELSE 3 END)
"""


def record(conn, year, rows, source, note=None):
    """Record (rider_id, number, team, manufacturer, class) for one season.

    Also moves riders' "latest" fields forward when this is the newest season
    we know for that rider, so a rider page shows the current team. Returns
    the number of rows written or changed.
    """
    if source not in RANK:
        raise ValueError(f"unknown source {source!r}")
    rows = [r for r in rows if r and r[0]]
    if not rows:
        return 0
    n = 0
    with conn.cursor() as cur:
        for rider_id, number, team, make, cls in rows:
            cur.execute(_UPSERT, (rider_id, year, number, team, make, cls,
                                  source, note))
            n += cur.rowcount
            # The rider page shows the newest season we know. Never let an
            # older season's row move it backwards.
            cur.execute(
                """
                UPDATE riders r SET
                    team = COALESCE(rs.team, r.team),
                    manufacturer = COALESCE(rs.manufacturer, r.manufacturer),
                    number = COALESCE(rs.number, r.number),
                    team_changed_at = CASE WHEN r.team IS DISTINCT FROM rs.team
                                           AND rs.team IS NOT NULL
                                           THEN now() ELSE r.team_changed_at END
                FROM rider_seasons rs
                WHERE rs.rider_id = r.id AND r.id = %s AND rs.year = %s
                  AND rs.year = (SELECT max(year) FROM rider_seasons
                                 WHERE rider_id = %s)
                """,
                (rider_id, year, rider_id),
            )
    return n
