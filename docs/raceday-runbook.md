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

The published schedule, which is a **TWO-MOTO** programme, not a single main:

| ET | what |
|---|---|
| **8:30 AM** | race window OPENS. The loop wakes; the app shows live timing from here |
| 8:50 AM | Qualifying Practice (250, 450, SMX Next) |
| 12:00 PM | 250 & 450 Wildcard Races |
| 2:30 PM | Opening Ceremonies — this is the stored `start_time_utc` |
| **3:00 PM** | **Gate** (countdown target) |
| **3:06 PM** | **250 Moto 1** — 🔔 **lock-screen cards launch here**, see below |
| 3:43 PM | 450 Moto 1 |
| 4:27 PM | SMX Next Main Event |
| 4:51 PM | 250 Moto 2 |
| **5:29 PM** | **450 Moto 2** — the last race |
| **8:30 PM** | race window CLOSES |

🔔 **Cards launch at the FIRST POINTS RACE — 250 Moto 1 — not when the window
opens.** For SMX the loop waits for a session that scores (`moto` or `main`);
practice, qualifying and the wildcards don't count. SX and MX still launch at
the window, which is when qualifying starts.

Two reasons, both permanent if you get it wrong: iOS ends a Live Activity after
~8 hours, so a card launched at 8:30 AM would die around 4:30 PM — **before
both Moto 2s** — and each phone is launched once per event, so it would never
come back. A card parked there all morning also just gets swiped, same result.

Reading it off the feed rather than the clock means **a rain delay carries the
launch with it**: if racing slips to 5 PM, so do the cards.
- [ ] So on the day: `starts` should stay **0 all morning** and jump when 250 Moto 1 goes on track. Morning zero is correct, not a fault.

🏁 **The closing card should be the six-row 250 + 450 card**, built from the
series' own published Overall (fixed 09-11, `4c2bca4`). Expect: class label on
the first row of each block, three riders each, **points** down the right.

- [ ] If only ONE class shows (three rows), that is correct-and-waiting: the site posts each class's Overall separately and the 450's lands minutes after the last moto. It fills in on the next push.
- [ ] If it falls back to "450 Moto 2 · final", the Overall wasn't readable. Not a failure — that is the old behaviour, deliberately kept as the fallback.

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

The results site serves last week's event until this one goes on track.

✅ **COLUMBUS IS ALREADY ADOPTED** — done by hand Friday 09-11 (event 29 →
`517544`, feed `7478`). Nothing to do for this round.

⚠️ **Don't lean on the automation for future rounds.** `.github/workflows/adopt-round.yml`
runs `adopt_round.py --write` every 15 minutes in theory, but GitHub's scheduler
was firing hourly jobs every **5–6 hours** on 09-11 — so it may fire once, late.
And **SMX runs a Friday programme**: the site switches a day before the stored
start, which the script's −8h guard refuses. For SMX, adopt by hand as soon as
the site switches:

```bash
python scripts/adopt_round.py --write --any-day
```

— after reading the venue it names and confirming it's the right round.

- [ ] Confirm it landed: the [adopt-round workflow](https://github.com/mitchfisch1-svg/moto-tracker/actions/workflows/adopt-round.yml) has a green run whose log says **"written to event 29"**

To check by hand, or if the workflow is failing:

```bash
python scripts/adopt_round.py          # dry run, prints what it would do
python scripts/adopt_round.py --write  # commit it
```

**Two guards make the unattended write safe**, and both are tested:
- it refuses any round we have **already closed** — the results homepage keeps
  serving the previous round for days after it finishes;
- it refuses to write unless the target event is **actually racing** (−8 h to
  +9 h around its start), so an id we have simply never seen cannot be adopted
  onto the wrong event. Override by hand with `--any-day`.

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

- [ ] `starts` stays **0 all morning**, then jumps to roughly the start-token count when **250 Moto 1** goes on track (~3:06 PM). That is push-to-start remote-launching cards onto closed apps. Verified 09-02, 09-07 and 09-11. **A zero at noon is correct.**
- [ ] **After that it may keep ticking up by one at a time — that's healthy.** Since 09-11 (`45ade30`) push-to-start is per PHONE, so each person who installs during the race gets their own card on the next push (~20 s while racing, up to 2 min during a hold). **What would be wrong is `starts` climbing by the whole token count again** — that would mean cards stacking on phones that already have one. Verified it doesn't: 12+ cycles flat after the burst.

🚨 **BEFORE RACE DAY: every phone should be on 1.6.1 AND have been opened once since updating.**

**1.6.1 went live on the App Store 09-08.** Do this on each of the four installs
in the days beforehand, not on race morning:

1. Update to **1.6.1** from the App Store
2. **Open MXT once** afterwards — any tab

Step 2 is a precaution, not a proven requirement: a new binary registers its own
push-to-start token when it first runs, and it is not established that the old
build's token survives an update. If it does not, and the app never runs, that
phone gets **no card at all**. Ten seconds removes the doubt.

| the phone runs | on race day |
|---|---|
| **1.6.1 (build 82)** | **nothing.** The card adopts itself and tracks the day untouched. Proven twice on 09-07 with the phone never touched. |
| **1.6.0 (build 80)** | **open MXT once** after push-to-start fires. Any tab — Standings is enough. |

Check which one a phone is on: **Settings → the bottom line.**

On 1.6.0 a remotely-launched card is **frozen on its launch frame until the app runs**. Verified 09-07: a card read "on the gate" for two full minutes while the race went green and finished, then came alive 12 seconds after the app was opened. Skip this on a 1.6.0 phone and it shows the gate all afternoon.

⚠️ **Until 1.6.1 is released and everyone has updated, assume 1.6.0 and do the pass.**
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

## 5½ · After the last main

- [ ] **Standings moved within an hour of the checkered.** Results ingest runs in
  GitHub's `results.yml`, which is throttled (see section 2). If they haven't:

```bash
python -m src.pipeline.run_results --smx-id 517544
```

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
