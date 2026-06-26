# Live timing & results — API investigation

**Status:** investigation only (Step 4). No code was built. This documents the
data surfaces behind `live.supermotocross.com` and the official results system so
we can choose an approach for Step 5 (results parsing).

**Date:** 2026-06-26. These are **unofficial, undocumented** endpoints and may
change without notice. Treat everything below as "observed", not "guaranteed".

---

## TL;DR

There are **two usable data surfaces**, both public and requiring no API key:

1. **Live JSON (real-time)** — `live.supermotocross.com` is a React app that reads
   static JSON files from an S3 bucket owned by **Live Race Media** (the timing
   provider). These update during a live event but only describe the **single race
   currently on track**.
2. **Durable results (the full record)** — `results.supermotocross.com` /
   `scoring.supermotocross.com` (same backend, "Live Time Scoring") serve a
   **server-rendered HTML results table for every race of every round**, plus a
   **PDF export** of each. This is the better source for backfilling complete
   results and computing standings.

**Recommendation:** build Step 5 against the **durable HTML results** for
completeness and standings; optionally layer the **live JSON** on top later for
real-time updates during events.

---

## 1. Live JSON API (Live Race Media)

`live.supermotocross.com` is a Create-React-App SPA. Its JS bundle fetches JSON
from a public S3 bucket, keyed by a **Live Race Media event id** (`lrm_id`):

```
https://s3.amazonaws.com/assets.liveracemedia.com/event_files/{lrm_id}/{file}.json
```

Observed with the sample `lrm_id = 7478` (2026 High Point National). All five
files returned HTTP 200 with no auth.

| File | Type | Purpose |
|---|---|---|
| `race.json` | object | Metadata for the race **currently on track** |
| `riders.json` | array | Per-rider live timing / running order (**the results data**) |
| `clock.json` | object | Race clock + flag state |
| `weather.json` | object | Trackside weather |
| `announcements.json` | array | Race-control messages (often empty) |

### race.json — selected fields

| Field | Example | Notes |
|---|---|---|
| `ClassName` | `"450 Moto #2"` | Class + session label |
| `EventName` | `"High Point National - Mount Morris, PA"` | |
| `RoundNumber` | `1` | LRM's own round numbering (not the series round) |
| `RoundType` | `3` | enum (inferred: 3 = Moto) |
| `RaceNumber` / `TotalRaces` | `6 / 6` | position within the day's race list |
| `HeatNumber` / `TotalHeats` | `2 / 2` | |
| `MainNumber` / `MultiMainCount` | `2 / 2` | |
| `RaceStatus` | `3` | enum (inferred: 3 = finished/official) |
| `RaceLengthSeconds` | `1800` | |
| `EventStartDateTime` | `"2026-06-20T00:00:00-04:00"` | ISO 8601 w/ offset |
| `NumberOfRacersInRace` | `40` | |
| `RaceLID` / `ClassLID` | `1640 / 55909` | LRM internal ids |
| `Series[]` | `[{"Name":"2026 Pro Motocross 450 Championship"}, ...]` | championships at this event |
| `SectorNames[]` / `SegmentNames[]` | named track sectors/segments | for sector timing |

### riders.json — selected fields (one object per rider, ~54 fields)

This is the running order / finishing order for the current race.

| Field | Example | Notes |
|---|---|---|
| `Position` | `1` | current/finishing position |
| `FirstName` / `LastName` | `"Hunter"` / `"Lawrence"` | |
| `BikeNumber` | `"96"` | string |
| `DriverLID` | `98` | LRM rider id (stable per rider — useful for resolution) |
| `CompletedLaps` | `17` | |
| `FastestLap` | `125.1026` | seconds |
| `FastestLapNumber` | `5` | |
| `AverageLap` | `127.07` | seconds |
| `ElapsedTime` | `2150.31` | seconds |
| `Pace` | `"17/35:50.313"` | laps / total time |
| `IsComplete` | `true` | finished the race |
| `IsDidNotStart` / `IsDidNotFinish` / `IsDisqualified` / `IsBroken` | bool | status flags → map to results.status |
| `Manufacturer` | `"Honda"` | |
| `TeamName` | `"Honda HRC Progressive"` | |
| `Hometown` | `"Landsborough, Australia"` | |
| `Age` | `27` | |
| `Country` | `null` | often null |
| `Sponsor` | long string | |
| `LatestSectors[]` / `LatestSegments[]` | nested | per-sector split times |

Top 5 from the sample (sanity check): P1 #96 Hunter Lawrence, P2 #1 Jett
Lawrence, P3 #38 Haiden Deegan, P4 #7 Aaron Plessinger, P5 #26 Jorge Prado.

### clock.json

`{ "Elapsed": 1800.0, "Remaining": 0.0, "FlagType": 6, "CautionElapsedSeconds": 0.0 }`
— `FlagType 6` inferred = checkered/finished.

### weather.json

`IconURL` (api.weather.gov), `TemperatureDegreesFahrenheit/Celsius`, `Forecast`,
`WindDirection`, `WindSpeed`, `HumidityPercentage`.

### Enum values seen (meanings INFERRED — verify before relying)

| Field | Seen | Likely meaning |
|---|---|---|
| `RaceStatus` | `3` | finished/official (clock at 0, checkered) |
| `FlagType` | `6` | checkered |
| `RoundType` | `3` | Moto (this was Pro Motocross) |
| `SortingType` | `8` | display sort order |

Other values (green/yellow/red flags, pre-race, running) were not observed
because the sample event is already finished. Capture them from a **live** event
to complete the enums.

### Limitations of the live JSON

- **Only the current race.** `race.json`/`riders.json` describe one race at a
  time and are overwritten as the program progresses. You cannot read "all results
  for the round" from here — you'd have to poll continuously during the event and
  persist each race as it completes.
- **Needs the `lrm_id`** (see §2). The S3 bucket **does not allow listing**
  (`event_files/` returns HTTP 403), so ids can't be enumerated from S3.

---

## 2. Event-id discovery (the linkage)

We already store, on every schedule row, a `source_url` containing the
**SuperMotocross event id** (`smx_id`), e.g. High Point R21 →
`results.supermotocross.com/results/?p=view_event&id=508725` → `smx_id = 508725`.

The Live Race Media id is derivable from that page. Asset URLs on the event page
use the path `event_files/{lrm_id}/{smx_id}/...`, e.g.:

```
https://assets.liveracemedia.com/event_files/7478/508725/2026_MX_R4_High_Point_Sector_Map_2D.jpg
```

So:

```
GET results.supermotocross.com/results/?p=view_event&id={smx_id}
  -> regex  event_files/(\d+)/   ->  lrm_id     (confirmed: 508725 -> 7478)
```

Path structure on the bucket: `event_files/{lrm_id}/{smx_id}/`.

---

## 3. Durable results (recommended source for Step 5)

`results.supermotocross.com` and `scoring.supermotocross.com` are the **same
backend** (Live Time Scoring / liveracemedia). Server-rendered HTML — no JS
needed, fully parseable with BeautifulSoup. PDF export available for the
pdfplumber path the kickoff mentioned.

### Event page → list of races

```
GET results.supermotocross.com/results/?p=view_event&id={smx_id}
```
Contains links to every race of the event:
```
/results/?p=view_race_result&id={race_id}
/results/?p=view_race_result&id={race_id}&export=pdf
```
The sample event (508725) listed **19 race ids** (qualifying, motos, etc.).

### Per-race result page

```
GET results.supermotocross.com/results/?p=view_race_result&id={race_id}
```
The **first `<table>`** is the finishing order with this header:

```
POS | # | BIKE | RIDER | BEST LAP (LAP #) | GAP | DIFF | HOMETOWN | TEAM
```
Example row:
```
1 | 1 |  | LACHLAN TURNER | 2:18.683 (4) | | | Gardnerville, NV | Altus Blu Cru Yamaha
```
(The page also contains ~99 tables of per-rider lap/sector detail; the results
table is the first one. Parse by its header row, not by index, to be safe.)

### PDF export

```
GET results.supermotocross.com/results/?p=view_race_result&id={race_id}&export=pdf
  -> 200, content-type application/pdf
```
Usable with `pdfplumber` if HTML parsing ever breaks.

---

## 4. Recommendation for Step 5

**Primary: durable HTML results** (`view_event` → `view_race_result` tables).
- Pros: complete history (every race of every round), persistent, parseable HTML,
  rich rider context (name + bike # + hometown + team) that helps entity
  resolution. We already have each event's `smx_id` from the schedule scraper.
- Flow: for each event, scrape race ids → parse each results table → resolve rider
  names → upsert `sessions` + `results` → recompute `standings`.

**Secondary (optional, later): live JSON** for real-time during events.
- Pros: low-latency, structured, includes stable `DriverLID` and DNF/DSQ flags.
- Cons: current-race-only, must poll and persist continuously, enums need a live
  event to decode.

**PDF (`pdfplumber`)** is a viable fallback but only if the HTML structure
changes — HTML is easier and richer here.

**Entity resolution inputs available:** full name, bike number, team, hometown,
and (in live JSON) a stable `DriverLID`. The `DriverLID` is the most reliable key
if we ever combine both surfaces.

---

## 5. Caveats & etiquette

- **Unofficial / undocumented.** No stability or availability guarantees. Build
  defensively (handle missing fields, HTTP errors, structure changes).
- **No auth required**, but this is for **personal use**. Identify with a real
  User-Agent, rate-limit (~1 req/sec), and cache responses.
- **The S3 bucket is public-read but not listable** — you must already know the id.
- **Don't hammer the live JSON.** During an event, polling every few seconds is
  what the official app does; off-event there's nothing new to fetch.

---

## Appendix — endpoints quick reference

```
# Live JSON (real-time, current race only) — needs lrm_id
https://s3.amazonaws.com/assets.liveracemedia.com/event_files/{lrm_id}/race.json
https://s3.amazonaws.com/assets.liveracemedia.com/event_files/{lrm_id}/riders.json
https://s3.amazonaws.com/assets.liveracemedia.com/event_files/{lrm_id}/clock.json
https://s3.amazonaws.com/assets.liveracemedia.com/event_files/{lrm_id}/weather.json
https://s3.amazonaws.com/assets.liveracemedia.com/event_files/{lrm_id}/announcements.json

# Derive lrm_id from the smx_id we already store on events.source_url
https://results.supermotocross.com/results/?p=view_event&id={smx_id}   # regex event_files/(\d+)/

# Durable results (full history) — start from smx_id
https://results.supermotocross.com/results/?p=view_event&id={smx_id}              # lists race ids
https://results.supermotocross.com/results/?p=view_race_result&id={race_id}       # HTML table
https://results.supermotocross.com/results/?p=view_race_result&id={race_id}&export=pdf  # PDF

# Rider headshots (bonus, for a future frontend)
https://storage.googleapis.com/feld-smx-rider-headshots/
```
