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

Neon's compute scales to zero after **5 minutes idle**. The important
consequence is that *frequency costs more than work does*: a job that runs for
two seconds still holds the compute up for the 5-minute idle window afterwards.

Two workflows were on `*/5` crons around the clock:

- `results.yml` — self-gated to race day, but the gate is a SQL query, so a
  connection opened every 5 minutes regardless.
- `warm.yml` — pinged `/live/warm`, whose own "is it race day?" gate is also a
  SQL query.

A connection every 5 minutes against a 5-minute idle timeout means the compute
**never** suspends. At 0.25 CU that is ~186 CU-hours a month against a 100
CU-hour quota — the app takes itself down somewhere around the middle of every
month.

## The shape of the fix

1. **Fold quiet-period jobs into one wake.** `pulse.yml` runs news, results and
   notifications as sequential steps in a single hourly job, so one idle window
   is paid instead of three.
2. **Only stay awake for races.** `results.yml` and `warm.yml` run every 5 min
   inside the race window (Sat 15:00 UTC → Sun 09:00 UTC) and not at all
   outside it.

Rough monthly budget:

| Source | CU-hours/month |
| --- | --- |
| `pulse.yml` (hourly, ~6 min billed per wake) | ~18 |
| Race weekends (~18 h continuous × 4 rounds) | ~18 |
| `schedule.yml`, `recap-video.yml` (weekly) | ~1 |
| **Headroom left for real user traffic** | **~63** |

## Rules of thumb before changing a cron

- Anything more frequent than **every 6 minutes, running 24/7, will pin the
  compute on permanently** and blow the quota. Treat `*/5` as race-window-only.
- Prefer adding a step to `pulse.yml` over adding a new scheduled workflow.
  Extra steps in an existing job are nearly free; a new cron is not.
- A "cheap no-op" job is not cheap if it opens a connection. The cost is the
  wake, not the query.

## If it happens again

Check Neon's console → **Billing / Usage** for the CU-hours used and the reset
date. Either wait for the reset or upgrade the project to Launch
(pay-as-you-go, no monthly minimum) to restore compute immediately. Then work
out which schedule started holding the compute open.
