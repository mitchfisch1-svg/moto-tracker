# Moto Tracker — feature ideas & roadmap

A study of what makes the best sports apps (ESPN, Bleacher Report) great, mapped
to Moto Tracker — plus the specific features you asked for. Written as a planning
doc; nothing here is built yet.

Legend — **Data?** = do we already collect the data · **Effort** = rough size.

---

## 1. Your requested features (next up)

### a. Bike manufacturer (KTM, Honda, Yamaha, …)
- **Data?** Yes — the results source already exposes it. The live-timing JSON has
  a `Manufacturer` field per rider, and the HTML results give team names like
  "Red Bull KTM Factory Racing" / "Honda HRC Progressive" we can map to a make.
  There is even an official **Manufacturers championship** in the data.
- **Plan:** add `manufacturer` to `riders` (and/or a `manufacturer_standings`),
  populate it during results ingest, expose via API, show a small make badge/logo
  on standings + rider screens. **Effort: Low–Med.**

### b. Race times in Eastern + "how to watch" (TV / streaming channel)
- **ET time — Data?** Yes. We store `start_time_utc`; convert to
  `America/Eastern` in the API (or app). **Effort: Low.**
- **TV / streaming — Data?** Partial. The supermotocross.com schedule page already
  carries broadcast info (Peacock / USA / NBC etc. — the page has "broadcast
  option" blocks). We'd extend the schedule scraper to capture the channel(s) per
  event into a new column, expose via API, and show "Sat 1:00 PM ET · Peacock" on
  the schedule + a race detail screen. **Effort: Med.**

---

## 2. What the best sports apps do — and the Moto Tracker version

| Their pattern | What it is | Moto Tracker version | Data? | Effort |
|---|---|---|---|---|
| **Favorites / Following** (ESPN + B/R core) | Pick favorite teams → personalized home | Follow riders + a manufacturer; a "My Riders" home tab | Yes | Med |
| **Alerts / Notifications** (both apps' #1 engagement driver — "fastest alerts") | Push for scores, breaking news | Push: race starting soon, results posted, a followed rider podiums/wins, breaking news | Yes | Med–High |
| **Gamecast / live game view** (ESPN) | Deep, auto-updating live screen | **Live timing screen**: running order, gaps, fastest lap, updating during a race | Yes (Live Race Media JSON) | Med |
| **Personalized newsfeed** (B/R) | News filtered to your teams | News filtered to followed riders/manufacturers | Yes | Low–Med |
| **Rich detail pages** (ESPN player/team) | Deep profiles | Rider profile (season stats, results, bio) + event page (full results) | Yes (API done) | Low–Med |
| **Predictions / gamification** (B/R Pick'em, W2E) | Pick outcomes, earn points | **Pick the podium** before each race; points + leaderboard vs friends | Needs accounts | High |
| **Social / community** (B/R reactions, sharing, polls) | React, share, discuss | Reactions, share a result card, race-day polls, comments | Needs accounts | High |
| **Video / highlights** (ESPN Verts, SC For You) | Short highlight reels | Embed/link race highlights & recaps | No (would need a source) | Med–High |
| **Standings depth** (ESPN) | Tables, tie-breakers | Points-gap-to-leader, "magic number" to clinch, trend arrows | Yes | Low–Med |

---

## 3. Moto-specific superpowers (things ESPN/B/R can't do — but you can)

- **Live timing screen** — the standout. During a live event, `riders.json` from
  Live Race Media gives real-time position, lap count, fastest lap, gaps. This is
  our "Gamecast," and the data pipeline for it is already documented
  (`docs/live-timing-api.md`).
- **Championship battle view** — points gap over the season as a chart; "X can
  clinch with N points." Motocross title fights are perfect for this.
- **Manufacturer standings** — a real championship the mainstream apps ignore.
- **Head-to-head rider comparison** — e.g. Jett vs Hunter, season side-by-side.
- **Track maps & venue info** — Live Race Media serves sector maps per event.
- **"How to watch" hub** — next race countdown + ET time + channel (your request),
  the thing every fan checks on race day.

---

## 4. Suggested build order

**Tier 1 — quick wins, data already exists (great next session):**
1. ET time + "how to watch" on the schedule (your ask).
2. Bike manufacturer badges (your ask).
3. Tap-through detail screens (rider profile, event results) — API already serves them.
4. Points-gap-to-leader on standings.

**Tier 2 — one scraper/API addition each:**
5. Broadcast/channel scraping (feeds #1).
6. Live timing screen (the showcase feature).
7. Personalized "My Riders" (local favorites first — no login needed).

**Tier 3 — needs user accounts + backend (later, real product):**
8. Push notifications (start with a simple service like ntfy.sh, later Firebase).
9. Pick-the-podium predictions + leaderboard.
10. Social: reactions, sharing, polls.

> Tip: favorites and notifications are the two features both ESPN and Bleacher
> Report lean on hardest for daily engagement — they're what turn a "check it
> sometimes" app into a "check it every day" app. Worth prioritizing once the
> Tier 1 basics are in.

## Sources
- ESPN app (personalization, StreamCenter/Gamecast, alerts): espnpressroom.com, espnfrontrow.com
- Bleacher Report app (fastest alerts, personalized feed, social, gamification/W2E): shortyawards.com, support.bleacherreport.com
