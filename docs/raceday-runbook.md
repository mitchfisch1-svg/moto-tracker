# Race-day runbook

One ordered page, so race day is execution rather than improvisation. The
reasoning lives in the handoff; this is just the sequence. Rewritten 10-08
for the 2027 season: the lock-screen Live Activity is gone, and the widgets
show no live timing, so race day is the app and the data behind it.

```
KEY  = $(grep "^MXT_MOCK_KEY=" .env | cut -d= -f2-)   (git-ignored .env ONLY — never write it here: this repo is public)
API  = https://moto-tracker-api.onrender.com
```

## 0 · Before the season (once, by early January)

The official tables have NEW ids every season. Nothing to type in any more:
the hourly results job reads each new table's heading ("2027 SX 450
Championship") and files it (`src/series_tables.py`). Supercross's tables are
due with A1, motocross's with round 1, WMX's once WMX races, the playoff
tables with the playoffs.

- [ ] Two days after A1: `python scripts/audit.py` → every invariant holds.
  If it says **"no 2027 X table found yet"**, open
  `results.supermotocross.com/results/?p=view_series_points&id=N` for the
  ids just above last season's and read the heading. Either the provider
  worded it in a way the classifier refuses (on purpose: any word it doesn't
  know leaves a table unfiled rather than misfiled) — teach `classify()` the
  wording, or add the id to `SEED` by hand — or the table isn't posted yet.
  **"two tables claim …"** means pick the real one and put it in `SEED`.
- [ ] 1.7.0 is on the App Store and people have updated (1.6.x still has the
  old lock-screen code, which now gets no pushes).

## 1 · Race week

- [ ] **Entry lists synced.** The hourly results job reads the 450 / 250 / WMX
  entry lists for any round in the next 10 days (team changes, numbers,
  rookies). Check a rider who moved over the winter shows the new team on
  his rider page, or run it by hand:

```bash
python -m src.pipeline.sync_entry_lists
```

## 2 · Race morning

```bash
curl -s https://moto-tracker-api.onrender.com/health
```

- [ ] `commit` matches what you last pushed. **If not, your fix is not running**, whatever the dashboard says. Read it twice — during a deploy Render serves old and new side by side.
- [ ] `db: true`
- [ ] `mock_race.running` is **false**. A mock left running would show every user a fake race.

```bash
python scripts/raceday_check.py
python scripts/audit.py
```

- [ ] `raceday_check` ends **ALL CLEAR**. The line that matters: `LIVE says <venue> but the site is serving <other>` — the false-LIVE bug.
- [ ] `audit` — every invariant holds.

## 3 · Once the results site switches to this round: adopt it

The results site serves last week's round until this one goes on track. **SX
is single-day; MX and SMX run a Friday programme**, so the site switches a day
early, which the automation's −8 h guard refuses — and GitHub's scheduler
fires late. Adopt by hand once the site shows this round:

```bash
python scripts/adopt_round.py                    # dry run: read the venue it names
python scripts/adopt_round.py --write --any-day  # only once it names THIS round
```

- [ ] It named the right venue and wrote to the right event
- [ ] **Keep the `run_results --smx-id …` line it prints** — section 6 uses it

```bash
curl -s "$API/live/sessions" | python -c "import sys,json;[print(x['label'],x['status']) for x in json.load(sys.stdin)['sessions']]"
```

- [ ] It names **this round's** sessions, not last week's

## 4 · While qualifying runs

- [ ] Open Race Day, then **Combined Qualifying**: it should change after each
  session (it is a running total). Results inside a race weekend are re-read
  every minute, and the phone keeps nothing for good until the server says
  `final` — the fix for the wrong board a team rep saw at the 2026 final.
- [ ] Spot-check one board against the official page
  (`results.supermotocross.com`) — position, rider, best lap.

## 5 · Between sessions

- [ ] Results hold, then the next race appears **on the gate with a new name**
- [ ] On the gate the right-hand column is **blank** — no gaps, no "Leader". Nobody has raced yet.
- [ ] On a finished board P1 reads **"Winner"**, not "Leader"

## 6 · After the last race

- [ ] **Every race stored.** The catch-up now re-ingests any round that finished
  in the last three days and is short of races, and the hourly audit checks it
  — but run it once anyway; it is idempotent and sends no stale alerts:

```bash
python -m src.pipeline.run_results --smx-id <this round's results id>
```

- [ ] **Standings moved** and match the official tables (the audit compares
  SMX rider by rider; for SX/MX, compare the top of each class by eye).
- [ ] The Rundown says the right thing. After the season's last round it names
  the champion ("… is the 2027 … champion"), not a title fight.

## Emergency: shipping a fix mid-race

**JavaScript / JSX / strings** — minutes, no review:

```bash
cd C:\Users\mitch\moto-tracker-mobile
eas update --branch main --message "what you fixed"
```

Two app relaunches to apply (`fallbackToCacheTimeout: 0` means launch never
waits on the network).

⚠️ **An update reaches ONE app version** (`runtimeVersion` is the appVersion
policy). It reaches only phones on the version in app.json (1.7.0 from the
10-08 build on). Weigh that before counting on an OTA to reach the audience.

**Backend** — push to main, Render deploys, confirm `commit` **twice**.

**Anything in `targets/widgets/*.swift`** — needs a full build and App Store
review. Not available on race day.

🚫 **Never deploy while a mock is running** — the restart wipes the run.

## Testing beforehand

```bash
# a full programme, ~12 min (warmup=N adds N seconds of qualifying first)
curl -X POST "$API/debug/mock-race?minutes=4&sessions=2&key=$KEY"

# stop early
curl -X POST "$API/debug/mock-race?stop=true&key=$KEY"
```

⚠️ A run shows a live "MXT System Test" race to every user. Keep them short.

---

**The one instruction that has found more bugs than the test suite:** run a
mock, or watch a real session, and *read the screen out loud* against the
official page. A finished board calling someone "Leader", a grid claiming gaps
before the flag, a qualifying board from the wrong round: none was visible to
the tests.
