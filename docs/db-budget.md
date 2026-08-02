# Database compute budget

The app runs on Neon's Free plan: **100 CU-hours of compute per project per
month**, reset at the start of the billing period. Storage is not the
constraint — compute time is. This note exists because we exceeded it once and
the whole app went down.

## The failure it caused

On 2026-07-31 every pipeline run and every API request started failing with:

```
ERROR: Your account or project has exceeded the compute time quota.
       Upgrade your plan to increase limits.
```

Neon suspends the project's compute when the quota is gone, so `/health`
returned `503 database unavailable`, and the app sat on its loading skeletons
forever showing "Waking up the server". Nothing was wrong with Render or the
app — the database underneath was switched off.

## Why it happened: the idle-timeout tax

Neon's compute scales to zero after **5 minutes idle**. The consequence that
matters is that *frequency costs more than work does*: a query that runs for two
milliseconds still holds the compute up for the 5-minute idle window behind it.
Anything touching the database more often than that pins it on permanently.

Three things were doing exactly that. In order of damage:

**1. The API's connection pool (the big one).** `ConnectionPool(min_size=1)`
keeps one connection open to Postgres for the entire life of the process. An
open connection is not idle, so this alone meant the compute could never scale
to zero for as long as the Render service was awake — no query required.

**2. The API's background loops.** `_live_activity_loop` ran
`SELECT ... FROM live_activity_tokens` every **10 seconds**, forever — ~8,600
queries a day, all week, whether or not a race was anywhere near. `_notify_loop`
added a `notify_work()` pass every 60 seconds on the same terms.

**3. The `*/5` crons.** `results.yml` and `warm.yml` both self-gated to race
day, but each gate is itself a SQL query, so a connection opened every 5 minutes
regardless. Smaller than the loops, same failure mode.

Worth noting how these compounded: `warm.yml`'s round-the-clock ping is what
kept the *Render* service from sleeping, and a permanently awake Render service
is what kept the loops running, which is what kept *Neon* awake. Removing any
one of them helps; the fix removes all three.

## The shape of the fix

1. **Let the pool drain.** `min_size=0` with a 60s `max_idle`, so an idle API
   holds no connection and the compute can actually suspend.
2. **Gate the loops on a race window.** `_race_window_open()` in
   `src/api/main.py` answers "is anything live?" from a cache whose TTL scales
   with how far off the next race is — hours when the paddock is empty. Outside
   a window the loops sleep without touching the database at all.
3. **Fold quiet-period jobs into one wake.** `pulse.yml` runs news, results and
   notifications as sequential steps of a single hourly job, so one idle window
   is paid instead of three.
4. **Only cron hard for races.** `results.yml` and `warm.yml` run every 5 min
   inside the race window (Sat 15:00 UTC → Sun 09:00 UTC) and not at all
   outside it.

Rough monthly budget after the fix:

| Source | CU-hours/month |
| --- | --- |
| `pulse.yml` (hourly, ~6 min billed per wake) | ~18 |
| `_notify_loop` idle passes (hourly, overlaps the above) | ~16 |
| Race weekends (~18 h continuous × 4 rounds) | ~18 |
| Background-loop gate checks (a handful a day) | ~1 |
| `schedule.yml`, `recap-video.yml` (weekly) | ~1 |
| **Headroom left for real user traffic** | **~46** |

Budget every *interval* against the 5-minute scale-to-zero timeout rather than
against how fresh the data needs to be. A 15-minute poll is not "four times
cheaper" than a 5-minute one — both wake the compute on every tick, so the
honest unit is wakes per hour times ~6 minutes. Anything under about 20 minutes
is close to leaving it on permanently.

## Compute size, not just compute time

The primary compute autoscales between a **minimum and maximum CU**, and on a
pay-as-you-go plan the maximum sets how expensive a busy hour can get — 8 CU
bills 32× what 0.25 CU does for the same wall-clock hour. This workload is a
small read-only API over a few thousand rows; it has no use for 8 CU. Keep the
ceiling at 1–2 CU (Neon console → branch → compute → **Edit**) so a traffic
spike or a bad query cannot quietly run up an eye-watering bill.

Also confirm scale-to-zero stays **enabled**. Paid plans allow turning it off,
and doing so puts the compute back to billing 24/7 — the exact failure this
whole document exists to prevent.

## Rules of thumb before adding a poll or a cron

- Anything touching the database more often than **every 6 minutes, 24/7, pins
  the compute on permanently** and blows the quota. Treat `*/5` as
  race-window-only.
- A new background loop in the API is the most expensive thing you can add,
  because it runs forever. Gate it on `_race_window_open()` and give it a long
  idle sleep before it ships.
- Never raise the pool's `min_size` above 0. One parked connection is enough to
  keep the compute billing around the clock on its own.
- Prefer adding a step to `pulse.yml` over adding a new scheduled workflow.
  Extra steps in an existing job are nearly free; a new cron is not.
- A "cheap no-op" job is not cheap if it opens a connection. The cost is the
  wake, not the query.

## If it happens again

Check Neon's console → **Billing / Usage** for the CU-hours used and the reset
date. Either wait for the reset or upgrade the project to Launch
(pay-as-you-go, no monthly minimum) to restore compute immediately. Then work
out which schedule started holding the compute open.
