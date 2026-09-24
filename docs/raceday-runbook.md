# Race-day runbook — SMX World Championship Final

**Sat Sep 26 2026 · Thunder Ridge Nature Arena, Ridgedale MO · TRIPLE POINTS**

One ordered page, so race day is execution rather than improvisation. The
reasoning lives in the handoff; this is just the sequence. Rewritten 09-24 for
the final — Columbus's version assumed push-to-start, which is now off.

```
KEY  = $(grep "^MXT_MOCK_KEY=" .env | cut -d= -f2-)   (git-ignored .env ONLY — never write it here: this repo is public)
API  = https://moto-tracker-api.onrender.com
```

## The day's shape

The venue is on **Central** time; everything below is **ET**.

| ET | what |
|---|---|
| **Fri Sep 25** | practice on track — **the results site switches to the final today; adopt it by hand (section 2)** |
| **12:30 PM** | race window OPENS (6 h before the stored start). The app shows live timing from here |
| 1:00 PM | Race Day Live (Peacock) — qualifying runs through the afternoon |
| 5:00 PM | Rig Riot |
| 6:30 PM | Opening Ceremonies — this is the stored `start_time_utc` |
| **7:00 PM** | **Gate** (countdown target) |
| ~7:06 PM | **250 Moto 1** — 🔔 **lock-screen cards start here**, see below |
| ~7:43 PM | 450 Moto 1 |
| ~8:27 PM | SMX Next Main Event |
| ~8:51 PM | 250 Moto 2 |
| **~9:29 PM** | **450 Moto 2** — the last race, and the title decider |
| 10:00 PM | Post-Race |
| 3:30 AM Sun | race window CLOSES (9 h after the start) |

⚠️ **The ~ times are ESTIMATES** — Columbus's gaps after its gate, applied to a
7 PM gate. The series has only published the gate. Columbus and LA both ran
this four-moto programme; the final has not said otherwise.

🔔 **Cards appear only when someone opens MXT and taps Race Day** — push-to-start
has been OFF for real events since 09-13. **Nobody gets a card without opening
the app.** And the SMX rule still holds: a card is wanted from the **first points
race** (250 Moto 1), not qualifying.

⏳ **iOS ends a Live Activity 8 hours after it starts.** A card opened for
qualifying at 1 PM dies around 9 PM — **during 450 Moto 2.** So:
- **1.6.2 with OTA `01a0d386` or later** (Mitch's TestFlight phone): the app
  waits for the motos by itself — it only starts a card once `/live` carries `card`.
- **1.6.1** (the App Store build — everyone else): it starts a card the moment
  Race Day is opened. **Tell people to open Race Day at 7 PM ET or later.**
  Done 09-24 by text; worth a reminder Saturday afternoon.

🏁 **The closing card should be the six-row 250 + 450 card**, built from the
series' own published Overall (`4c2bca4`). Class label on the first row of each
block, three riders each, **points** down the right.

- [ ] If only ONE class shows (three rows), that is correct-and-waiting: the site posts each class's Overall separately and the 450's lands minutes after the last moto. It fills in on the next push.
- [ ] If it falls back to "450 Moto 2 · final", the Overall wasn't readable. Not a failure — that is the old behaviour, deliberately kept as the fallback.

---

## 1 · Before the window opens (Saturday, by 12:30 PM)

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

- [ ] **11/11 invariants hold** — including "SMX standings are the playoff table the series publishes"

## 2 · FRIDAY: adopt the final once the site switches

The results site serves Los Angeles until the final goes on track — and **SMX
runs a Friday programme**, so it switches a day before the stored start. Event
31 has **no results id and no feed id** until this is done. The automation's
−8 h guard refuses a Friday switch, and GitHub's scheduler fires late, so adopt
by hand:

```bash
python scripts/adopt_round.py                    # dry run: read the venue it names
python scripts/adopt_round.py --write --any-day  # only once it says Thunder Ridge
```

- [ ] It named **Thunder Ridge**, not Los Angeles, and wrote to **event 31**
- [ ] **Keep the `run_results --smx-id …` line it prints at the end** — that is section 5½'s command

**Two guards make the write safe**, and both are tested: it refuses any round we
have already closed (the homepage keeps serving the previous round for days), and
without `--any-day` it refuses unless the event is actually racing.

**Why this is not optional.** Until it is adopted, `/live` falls back to the most
recently cached feed id. The feed is series-wide (Columbus's own id was the same
`7478` as Ironman's), so live timing works on the fallback — but the round has no
results id, so nothing ingests and nothing retires it properly.

```bash
curl -s "$API/live/sessions" | python -c "import sys,json;[print(x['label'],x['status']) for x in json.load(sys.stdin)['sessions']]"
```

- [ ] It names **Thunder Ridge** sessions, not Los Angeles

**Optional: SMX "still to come" chips** — the app has MX and SX lists but none
for SMX. Columbus and LA both published exactly `250 Moto #1`, `450 Moto #1`,
`SMX Next Main Event`, `250 Moto #2`, `450 Moto #2`. If Friday's labels match,
add an `else if (series === 'SMX')` branch to `upcomingSessions` (App.js) and
`eas update`. **Do not guess ahead of the real labels** — phantom chips are the
failure this was built to remove.

## 3 · From 12:30 PM, the loop is awake

A healthy reading looks like this:

```
"commit": "…"                     the build you expect
"mock_race": {"running": false}
"live_activity": {
  "seconds_since_cycle": 4.2      <= ~10s during a race. READ THIS FIRST.
  "update_tokens": 3,             phones with a card the server can reach
  "cards_wanted": true,           false until 250 Moto 1 — correct
  "pushes": 240, "failed": 0,
  "starts": 0                     push-to-start is OFF — 0 all day is correct
}
```

🚨 **`seconds_since_cycle` first, always.** Every other field is written once per
cycle and never cleared, so they all survive the loop dying.

🚨 **`update_tokens` is the number that matters.** Columbus ran all day with
`pushes` climbing, `failed: 0` and every card frozen — Apple accepts pushes to
cards that no longer exist. **`update_tokens: 0` during a moto means nobody has
a live card.** It only rises as people open Race Day.

🚨 **Read `pushes` PER TOKEN, not per minute.** One push goes to each update
token per round: `pushes ÷ update_tokens ÷ elapsed_minutes ≈ 3` is healthy.

## 4 · Around the first gate drop (~7:00 PM)

- [ ] `cards_wanted` turns **true** when 250 Moto 1 goes on track (backstop: 8:00 PM, 90 min after the stored start). **False all afternoon is correct.**
- [ ] Open **Race Day** on your own phone. Within ~5 s the card reads **250 Moto 1 · on the gate**, riders listed, gaps blank. `update_tokens` goes up by one.
- [ ] The card drops "· on the gate" within ~5 s of the flag. **Measured 09-24: 3–4 s**, with the phone locked.
- [ ] **Lock the phone and leave it.** Verified 09-24 on build 83: it kept updating while locked through the finish and ended on Final results.
- [ ] `failed` stays near zero. A few `Unregistered` are dead tokens from old installs; the loop deletes them itself.

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

## 5½ · After the last moto

- [ ] **SMX standings moved.** They are read straight off the official playoff
  table (`/standings?series=SMX`, refreshed every 5 min) — no ingest involved,
  so this says nothing about whether results landed. If they look stale, compare
  https://www.supermotocross.com/results/standings/smx/450/ — if that hasn't
  moved either, the series hasn't published yet.
- [ ] **Re-ingest the round once Moto 2 is posted — every time, don't wait for a
  symptom.** Los Angeles kept only Moto 1 for five days: `/live` retires a round
  the moment its last race ends, and the catch-up only retries rounds with NO
  results, so a round caught between motos is never revisited. Idempotent, and
  it sends no stale podium pushes (those are gated on the race time):

```bash
python -m src.pipeline.run_results --smx-id <the final's results id>
```

## 6 · Between sessions

- [ ] Results hold, then the next race appears **on the gate with a new name**
- [ ] On the gate the right-hand column is **blank** — no gaps, no "Leader". Nobody has raced yet.
- [ ] On a finished board P1 reads **"Winner"**, not "Leader"

## 7 · After the last race

- [ ] Card becomes **`Final results`** — top three of 250 and 450, six rows, class labels, points
- [ ] It **stays for an hour**, then dismisses itself. Persisting is correct, not a fault.
- [ ] `update` tokens drop to 0 in the database — that is the teardown having run
- [ ] **SMX standings show the champions** within ~5 min of the series publishing (`/standings?series=SMX`, off the official playoff table). Compare https://www.supermotocross.com/results/standings/smx/450/ — the hourly audit does this rider by rider.

## If it all goes long

The window closes at **3:30 AM ET**, six hours after the last moto should end,
so a delay has to be enormous to matter. The limit that bites first is iOS's
**8 hours per card**: a card started at 7 PM lasts until 3 AM, fine — but a card
started for qualifying does not, which is why the app now waits for the motos.

**If the window does close on a live feed:** the server ends activities on the
window's open→closed edge (`c45198f`), so it self-heals on the next pass. If it
does not, the manual clear is for each user to open the app.

## Emergency: shipping a fix mid-race

**JavaScript / JSX / strings** — minutes, no review:

```bash
cd C:\Users\mitch\moto-tracker-mobile
eas update --branch main --message "what you fixed"
```

Two app relaunches to apply (`fallbackToCacheTimeout: 0` means launch never
waits on the network). Verified working 09-01.

⚠️ **An update reaches ONE app version** (`runtimeVersion` is the appVersion
policy). The repo is at **1.6.2**, so `eas update` from it reaches only 1.6.2
builds — Mitch's TestFlight phone. **Everyone else is on 1.6.1** and gets nothing
unless the fix is also published from the 1.6.1 code. Weigh that before
counting on an OTA to reach the audience.

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
