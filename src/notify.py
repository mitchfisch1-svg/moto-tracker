"""Push-notification triggers.

Runs in the pipeline (``scheduler --job notify``), NOT the API, so it stays free
of any FastAPI import. Each trigger reads current DB state and fires only what
hasn't been sent yet — deduped via the ``push_sent`` ledger — so it's safe to run
every few minutes.

Notifications are "fire once when it happens, to whoever's subscribed right
then." The ledger is marked even when there are zero subscribers, so a user who
installs later never gets a burst of stale alerts for things that already
happened.
"""

import json
import logging

import requests

from .db import get_connection

log = logging.getLogger("moto.notify")

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_SERIES_LONG = {"SX": "Supercross", "MX": "Pro Motocross", "SMX": "SuperMotocross"}


def send_push(tokens, title, body, data=None):
    """Deliver one notification to many Expo tokens (batched ≤100 per request)."""
    tokens = list(dict.fromkeys(t for t in tokens if t))   # de-dupe, drop blanks
    if not tokens:
        return 0
    sent = 0
    for i in range(0, len(tokens), 100):
        batch = tokens[i:i + 100]
        messages = [{"to": t, "title": title, "body": body,
                     "sound": "default", "data": data or {}} for t in batch]
        try:
            resp = requests.post(_EXPO_PUSH_URL, json=messages, timeout=15)
            resp.raise_for_status()
            sent += len(batch)
        except requests.RequestException:
            log.warning("push batch failed", exc_info=True)
    return sent


# --- ledger + targeting helpers ----------------------------------------------
def _seen(cur, key):
    """Return None if the key is unseen, else the stored (value,) row."""
    cur.execute("SELECT value FROM push_sent WHERE key = %s", (key,))
    return cur.fetchone()


def _mark(cur, key, value=None):
    cur.execute(
        """
        INSERT INTO push_sent (key, value, sent_at) VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, sent_at = now()
        """,
        (key, value),
    )


def _tokens_following(cur, rider_ids, pref):
    """Tokens that follow any given rider AND have the `pref` toggle on."""
    ids = [int(r) for r in rider_ids]
    if not ids:
        return []
    cur.execute(
        """
        SELECT DISTINCT token FROM push_tokens
        WHERE COALESCE((prefs ->> %s)::boolean, true)
          AND EXISTS (SELECT 1 FROM jsonb_array_elements(rider_ids) e
                      WHERE (e #>> '{}')::int = ANY(%s))
        """,
        (pref, ids),
    )
    return [r[0] for r in cur.fetchall()]


def _all_tokens(cur, pref):
    """Every token with the `pref` toggle on (for broadcast alerts)."""
    cur.execute(
        "SELECT token FROM push_tokens WHERE COALESCE((prefs ->> %s)::boolean, true)",
        (pref,),
    )
    return [r[0] for r in cur.fetchall()]


def _last_name(full):
    return (full or "").split(" ")[-1]


# --- triggers ----------------------------------------------------------------
def _rider_results(cur):
    """A followed rider wins/podiums a points session (main or moto)."""
    cur.execute(
        """
        SELECT r.session_id, r.rider_id, r.position, ri.full_name,
               sess.label, e.venue
        FROM results r
        JOIN sessions sess ON sess.id = r.session_id
        JOIN events e ON e.id = sess.event_id
        JOIN riders ri ON ri.id = r.rider_id
        WHERE r.position BETWEEN 1 AND 3
          AND sess.type IN ('main', 'moto')
          AND e.event_date >= (now() - interval '2 days')::date
        """
    )
    for session_id, rider_id, pos, name, label, venue in cur.fetchall():
        key = f"result:{session_id}:{rider_id}"
        if _seen(cur, key):
            continue
        tokens = _tokens_following(cur, [rider_id], 'results')
        if tokens:
            race = (label or "").replace("#", "").strip()
            verb = "won" if pos == 1 else f"finished P{pos} in"
            send_push(
                tokens,
                "🏁 Winner!" if pos == 1 else "🏁 Podium!",
                f"{name} {verb} {race} at {venue}.",
                {"rider_id": rider_id, "rider_name": name},
            )
        _mark(cur, key)


def _gate_drop(cur):
    """~15-minute heads-up before a main race's gate drop (broadcast)."""
    cur.execute(
        """
        SELECT e.id, e.venue, s.abbrev, e.broadcast,
               EXTRACT(EPOCH FROM (e.start_time_utc - now())) / 60 AS mins
        FROM events e
        JOIN seasons se ON se.id = e.season_id
        JOIN series s ON s.id = se.series_id
        WHERE e.start_time_utc IS NOT NULL
          AND e.start_time_utc > now()
          AND e.start_time_utc <= now() + interval '20 minutes'
        """
    )
    for eid, venue, abbrev, broadcast, mins in cur.fetchall():
        key = f"gate:{eid}"
        if _seen(cur, key):
            continue
        providers = ""
        try:
            bc = json.loads(broadcast) if broadcast else None
            gd = next((b for b in (bc or [])
                       if "gate drop" in (b.get("label") or "").lower()),
                      (bc or [None])[0])
            if gd and gd.get("providers"):
                providers = " · 📺 " + " · ".join(gd["providers"])
        except (TypeError, ValueError):
            pass
        send_push(
            _all_tokens(cur, 'gate'),
            "🟢 Race starting soon",
            f"{_SERIES_LONG.get(abbrev, abbrev)} {venue} — gate drop in "
            f"~{int(mins)} min{providers}",
        )
        _mark(cur, key)


def _championship(cur):
    """The points leader in a class changes (broadcast). Seeds silently first."""
    cur.execute(
        """
        SELECT st.season_id, st.class, st.rider_id, ri.full_name, s.abbrev
        FROM standings st
        JOIN seasons se ON se.id = st.season_id
        JOIN series s ON s.id = se.series_id
        JOIN riders ri ON ri.id = st.rider_id
        WHERE st.position = 1
        """
    )
    for season_id, cls, rider_id, name, abbrev in cur.fetchall():
        key = f"leader:{season_id}:{cls}"
        row = _seen(cur, key)
        if row is None:
            _mark(cur, key, str(rider_id))       # first time: seed, don't alert
            continue
        if row[0] == str(rider_id):
            continue                              # same leader, nothing to say
        send_push(
            _all_tokens(cur, 'leader'),
            "👑 New points leader",
            f"{name} now leads the {cls} {_SERIES_LONG.get(abbrev, abbrev)} "
            f"championship.",
            {"rider_id": rider_id, "rider_name": name},
        )
        _mark(cur, key, str(rider_id))


def _rider_news(cur):
    """Breaking news mentioning a rider someone follows (targeted)."""
    cur.execute(
        """
        SELECT DISTINCT (e #>> '{}')::int
        FROM push_tokens, jsonb_array_elements(rider_ids) e
        """
    )
    followed = [r[0] for r in cur.fetchall()]
    if not followed:
        return
    cur.execute("SELECT id, full_name FROM riders WHERE id = ANY(%s)", (followed,))
    names = {rid: full for rid, full in cur.fetchall()}
    cur.execute(
        """
        SELECT id, title, summary, url FROM news_articles
        WHERE COALESCE(published_at, fetched_at) >= now() - interval '24 hours'
        ORDER BY COALESCE(published_at, fetched_at) DESC
        LIMIT 40
        """
    )
    for aid, title, summary, url in cur.fetchall():
        key = f"news:{aid}"
        if _seen(cur, key):
            continue
        hay = f"{title or ''} {summary or ''}".lower()
        matched = [rid for rid, full in names.items()
                   if len(_last_name(full)) > 2 and _last_name(full).lower() in hay]
        if matched:
            tokens = _tokens_following(cur, matched, 'news')
            if tokens:
                send_push(tokens, f"📰 {names[matched[0]]}",
                          title or "New story", {"url": url})
        _mark(cur, key)


def notify_work():
    """Run every trigger once. Called by the scheduler (--job notify)."""
    log.info("notify: starting")
    with get_connection() as conn:
        with conn.cursor() as cur:
            _gate_drop(cur)
            _rider_results(cur)
            _championship(cur)
            _rider_news(cur)
    log.info("notify: done")
