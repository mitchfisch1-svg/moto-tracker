# Race-day runbook — SMX Playoff Round 1

**Sat Sep 12 2026 · Historic Crew Stadium, Columbus OH**

One ordered page, so race day is execution rather than improvisation. Everything
here is drawn from what was actually measured 08-31 → 09-02; the reasoning lives
in the handoff, this is just the sequence.

```
KEY  = mxt-mock-fb7b3cdf0c346eb3f5c6a67bfad2af6d   (also in .env, git-ignored)
API  = https://moto-tracker-api.onrender.com
```

## The day's shape

| ET | what |
|---|---|
| **10:30 AM** | race window OPENS (start − 4 h). The Live Activity loop wakes here. |
| 9:00 AM | Race Day Live (Peacock) |
| **2:30 PM** | Pre-Race Show — this is `start_time_utc`, so it is what the app counts down to |
| **3:00 PM** | **Gate Drop** |
| 6:00 PM | Post-Race |
| **8:30 PM** | race window CLOSES (start + 6 h) |

⏱️ **2½ hours of margin** between racing ending and the window closing. Only a
delay past **8:30 PM** puts the teardown at risk — see *If it all goes long*.

---

## 1 · Morning, before 10:30

```bash
curl -s https://moto-tracker-api.onrender.com/health
```

- [ ] `commit` matches what you last pushed. **If not, your fix is not running**, whatever the dashboard says. Read it twice — during a deploy Render serves old and new side by side.
- [ ] `db: true`, `apns: true`
- [ ] `mock_race.running` is **false**. A mock left running would show every user a fake race.
- [ ] `seconds_since_cycle` is a number, not null

```bash
python scripts/raceday_check.py
```

- [ ] ends **ALL CLEAR**. The line that matters: `LIVE says <venue> but the site is serving <other>` — that is the false-LIVE bug.

```bash
python scripts/audit.py
```

- [ ] **10/10 invariants hold**

## 2 · Once the site switches to Columbus

The results site serves last week's event until this one goes on track. When it
flips, three things become possible. **Run this every half hour from mid-morning
— it tells you whether the switch has happened, and does the work when it has:**

```bash
python scripts/adopt_round.py
```

It is a **dry run by default** and prints what it would do. When it reports the
site is still serving Ironman, that is the normal pre-race state and there is
nothing to do — check back later. When it names Columbus:

```bash
python scripts/adopt_round.py --write
```

- [ ] It reports **ALL CLEAR** and writes `source_url` + `lrm_id` onto Playoff 1
- [ ] Any `WARN` about the S3 feed not answering is fine before cars are on track — re-run once qualifying starts

**Why this is not optional.** Until that write happens the round has no results
id and no Live Race Media id, so `/live` falls back to the most recently cached
id — `ORDER BY event_date DESC`, which today is **Ironman's `7478`, an MX feed**.
The fallback is documented as safe because the feed is series-wide, but it has
never been exercised across a series boundary, and SMX is not MX. This was fixed
by hand at RedBud, Southwick and Denver; the script is that dig, scripted. It
**refuses to adopt a round we have already closed**, so it cannot publish
Ironman under Columbus's name.

```bash
curl -s "$API/live/sessions" | python -c "import sys,json;[print(x['label'],x['status']) for x in json.load(sys.stdin)['sessions']]"
```

- [ ] It names **Columbus** sessions, not Ironman

**Then ship the SMX session chips** — 5 minutes, no build, from real labels:

1. Read the labels above
2. Add an `else if (series === 'SMX')` branch to `upcomingSessions` (App.js, search for `function upcomingSessions` — around line 1496) listing them
3. `eas update --branch main --message "SMX session chips"`
4. Two app relaunches to pick it up

⚠️ **Do not pre-guess the format.** Phantom chips for sessions that never run is the failure this whole month was spent removing.

## 3 · From 10:30 AM, the loop is awake

A healthy reading looks like this:

```
"commit": "…"                     the build you expect
"mock_race": {"running": false}
"live_activity": {
  "seconds_since_cycle": 4.2      <= ~10s during a race. READ THIS FIRST.
  "tokens": 40, "pushes": 1200, "skipped": 900, "failed": 0,
  "starts": 40, "starts_failed": 0
}
```

🚨 **`seconds_since_cycle` first, always.** Every other field is written once per
cycle and never cleared, so they all survive the loop dying. A `tokens: 36,
cycle_ms: 87` that was five hours stale once read exactly like a healthy loop.

🚨 **Read `pushes` PER TOKEN, not per minute.** One push goes to each update
token per round. One phone = 3/min. Forty phones = 120/min and that is fine.
**The number that matters is `pushes ÷ tokens ÷ elapsed_minutes ≈ 3.**

## 4 · Around the first gate drop

- [ ] `starts` climbs **once**, early, to roughly the start-token count. That is push-to-start remote-launching cards onto closed apps. Verified working 09-02.
- [ ] `starts_failed` small or zero. A few `Unregistered` are dead tokens from old installs; the loop deletes them itself. **Non-zero `starts_failed` is not automatically bad** — check whether the tokens that failed were ones that should still exist.
- [ ] The card drops "· on the gate" within ~10s of the flag. Measured 4–9s.

## 5 · While racing

| what you see | what it means |
|---|---|
| `pushes ÷ tokens` ≈ **3/min**, `skipped` rising | ✅ working — this is the measured-good state |
| `pushes ÷ tokens` ≈ **6/min** | the clock is counting as news again; the throttle is coming |
| `failed` climbing, `pushes` flat | **Apple is refusing us** — read `last_error`; different problem entirely |
| `live_call_ms` near `push_interval_s` | the loop is the bottleneck, not Apple |
| `seconds_since_cycle` large or null | **the loop thread is dead.** Every lock screen is frozen. Redeploy. |
| card stale but `pushes` climbing, `failed=0` | Apple accepts pushes to activities that no longer exist. Only a human looking at a phone can tell. |

**If the card lags:** raise `_LA_MIN_GAP_S` 20 → 30 (main.py). ~60 pushes/moto
instead of ~89. **Transitions do NOT get slower** — they are bounded by the 10s
loop interval, not the floor. Backend only, live in minutes.

## 6 · Between sessions

- [ ] Results hold, then the next race appears **on the gate with a new name**
- [ ] On the gate the right-hand column is **blank** — no gaps, no "Leader". Nobody has raced yet.
- [ ] On a finished board P1 reads **"Winner"**, not "Leader"

## 7 · After the last race

- [ ] Card becomes **`Final results`** — top three of 250 and 450, six rows, class labels, points
- [ ] It **stays for an hour**, then dismisses itself. Persisting is correct, not a fault.
- [ ] `update` tokens drop to 0 in the database — that is the teardown having run

## If it all goes long

Only if racing runs past **8:30 PM ET** does the window close before the feed
goes quiet. Then the loop sleeps without ending anything and every locked phone
keeps a frozen card.

**Fix in the moment:** the server ends activities on the window's open→closed
edge (`c45198f`), so it self-heals on the next pass. If it does not, the only
manual clear is for each user to open the app — and only the Race Day tab
triggers it on builds before 1.6.0.

## Emergency: shipping a fix mid-race

**JavaScript / JSX / strings** — minutes, no review:

```bash
cd C:\Users\mitch\moto-tracker-mobile
eas update --branch main --message "what you fixed"
```

Two app relaunches to apply (`fallbackToCacheTimeout: 0` means launch never
waits on the network). Verified working 09-01.

**Backend** — push to main, Render deploys, confirm `commit` **twice**.

**Anything in `targets/widgets/*.swift` or `modules/*/ios/*.swift`** — needs a
full build and App Store review. Not available on race day. Plan accordingly.

🚫 **Never deploy while a mock is running** — the restart wipes the run, resets
`was_open` (suppressing teardown) and zeroes every counter you are reading.

## Testing any of this beforehand

```bash
# a full programme + the end-of-day card, ~12 min
curl -X POST "$API/debug/mock-race?minutes=4&sessions=2&key=$KEY"

# add push-to-start (launches a card on EVERY registered phone — ask first)
curl -X POST "$API/debug/mock-race?minutes=3&sessions=1&push_to_start=true&key=$KEY"

# stop early
curl -X POST "$API/debug/mock-race?stop=true&key=$KEY"
```

⚠️ A run shows a live "MXT System Test" race to every user. Keep them short.
⚠️ You must open the app **and tap the Race Day tab** — that is what starts the
Live Activity. Opening to Standings registers nothing.

---

**The one instruction that has found more bugs than the test suite:** run a
mock, then *read the card out loud*. Two of the last three bugs were caught that
way — a staged grid claiming gaps for a race nobody had started, and a finished
board still calling someone the leader. Neither was visible to 250 tests.
