# The USA Cycling / Sport80 public API

Notes on `usacycling.sport80.com`, written while building the scraper. Nothing
here is officially documented; it was all established by observation, and the
quirks section is the part worth reading twice.

There is no authentication. Every endpoint below is public.

## Getting JSON

The site is an [Inertia.js](https://inertiajs.com/) app. Page URLs return a full
HTML shell by default, but send `X-Inertia: true` and the same URL returns the
page's props as JSON — less than half the bytes and no parsing:

```http
GET /pub/results/events/30633/d/cx/2025-11-23
X-Inertia: true
X-Requested-With: XMLHttpRequest
```

Datatable endpoints are plain JSON and need no such header.

## Endpoints

### Event search

```
GET /pub/results/datatable?discipline=CX&state=KY&from_date=2017-01-01
GET /pub/results/datatable?data=1&discipline=CX&state=KY&from_date=2017-01-01&p=0
```

Without `data=1` you get the table configuration — columns, and the paging
parameters under `paginated` / `items_per_page`. With `data=1` you get rows.

| Parameter | |
|---|---|
| `discipline` | `RR CCR CRIT TT XC STXC CM DH SD DS MX CX PR SR IP SPR KEIR` |
| `state` | Two-letter code. 50 states; the platform does not list DC. |
| `from_date` / `to_date` | `YYYY-MM-DD` |
| `p` | **Page number, 0-indexed** |

Each row carries the event id, name, location, date (with `to_date` for
multi-day events), and `latest_race_date`.

### Event page

```
GET /pub/results/events/{event_id}/d/{discipline}/{race_date}    (X-Inertia: true)
```

`props.event_results_page` holds:

| Field | |
|---|---|
| `race_results_datatables` | `[{race_id, title, url}]` — the waves for **this day only** |
| `discipline_filters` | The event's **other** day/discipline panels |
| `event_name`, `event_location`, `event_date` | Event metadata |
| `gender_filters` | `?gender=F` / `N` / `M` variants of this page |

### Race results

```
GET /pub/results/races/{race_id}/datatable?data=1
```

Not paginated — one request returns the whole field. Columns: place, points,
athlete name, city/state, time, licence, bib, team.

### Athlete endpoints

```
GET /pub/athletes/{licence}/profile                      (X-Inertia: true)
GET /pub/athletes/{licence}/licence                      (X-Inertia: true)
GET /pub/athletes/{licence}/results/datatable?data=1&discipline_slug=cx&p=0
GET /pub/athletes/{licence}/d/{discipline}               (X-Inertia: true)
```

| Endpoint | What it gives |
|---|---|
| `profile` | `category_upgrades` (current/next category, points earned vs required, per discipline group) and `athlete_rankings` |
| `licence` | Racing age, birth year, sex, UCI ID, team, per-discipline categories, licence type and expiry |
| `results/datatable` | Full race history, paginated 20/page, date-descending, filterable by `discipline_slug` |
| `d/{discipline}` | Ranking audit — `contributing_events` with per-race **ranking** points |

### Public rankings

```
GET /pub/rankings/d/cx/datatable?data=1&state=OH&category=cx_category_3&gender=M&p=0
```

Licence, name, city, rank, points and **racing age**, in bulk — about 60
requests covers every ranked rider in three states. Filterable by state,
category (`cx_category_1` … `cx_category_5`) and gender (`M`/`F`/`N`).

Two caveats: only *ranked* riders appear, and rankings key on a rider's
**registered home state**, not where they race. Expect partial coverage.

### Endpoints that do not work

`/pub/athletes/search` and `/pub/athletes/results_search` return HTTP 500 on
every parameter combination tried. There is no rider enumeration — you need
licence numbers from somewhere else, and race results supply them.

## Cell value shapes

Datatable cells are either scalars or typed objects:

```json
{"type": "unset"}                                          → ""
{"type": "hyperlink", "url": "...", "value": "NAME"}       → "NAME"
{"text": "1", "type": "gold"}                              → "1"   (medal colour)
{"type": "date", "month": "Nov", "day": "23", "year": "2025"}
```

`place` carries `gold`/`silver`/`bronze`/`result` as its type, and its text may
be `DNF`, `DNS`, `DQ` or `DNP` rather than a number.

## The traps

### Search paging uses `p`, and wrong names fail silently

`page`, `offset`, `start`, `limit`, `per_page` and `items_per_page` are all
accepted and ignored — you get page one back with a 200. Only `p` works, and
it is 0-indexed. A `total` of 25 with 20 rows returned is the tell.

### One search result can hide several race days

The search links only an event's latest day. Other days live in
`discipline_filters` on the event page, each with its own race list.

2024 CX Nationals: **14 races** on the linked day, **47** across all four. A
scraper that follows only the search link loses two-thirds of the event without
any error.

Walk every `discipline_filters` entry whose discipline matches, and key your
"already visited" set off the panel the server **actually served** (the one
marked `active: true`) rather than the date you requested — an unknown date
silently falls back to the event's default panel, and trusting your own request
double-counts it.

### Ranking points and upgrade points are different systems

The `Points` column in race results is **ranking** points (hundreds-scale, lower
is better). Upgrade points are a separate placement-based system, and USA
Cycling publishes only the aggregate balance — never a per-race breakdown.

### Category 5 balances count races, not points

For Cat 5 riders, `category_upgrades.points_earned` and `points_required` count
**race finishes**. Their rule is "finish 5 races". Measured against 9 Cat 5
riders: mean error 0.22 against finishes, 4.89 against points.

Do not sum `points_earned` across categories.

### Points reset on upgrade, and upgrade dates are not published

USA Cycling zeroes a rider's balance when they upgrade. No endpoint exposes
when that happened, so a race history will contain races that no longer count.

A usable invariant: the 12-month window sits inside the 36-month one, so a
12-month subtotal can never exceed the official 36-month balance. If your
computed figure does, it is counting races USA Cycling no longer credits — cap
it at the official number.

### The profile endpoint is sometimes served wrong

Identical requests to `/pub/athletes/{id}/profile` can return different
`category_upgrades` content. One rider alternated roughly 50/50 between:

```
Category 1 -> Pro,        points_earned None, points_required None
Category 5 -> Category 4, points_earned 8,    points_required 5
```

All 200s, all `cf-cache-status: DYNAMIC`, distinct `cf-ray` on each — so this is
origin-side, not caching. Browser headers make no difference.

Two things make it detectable:

- The bad response **contradicts itself**: `athlete_rankings` in the very same
  payload still reported the correct category.
- `/pub/athletes/{id}/licence` was stable across repeated calls.

Adding **`?discipline_slug=cx` makes it worse** — the filtered profile returned
the bad variant on 10 of 10 calls, while the unfiltered one was correct on 9 of
10. The site's own URL includes that parameter, which is probably why the
website displays the wrong category for the affected rider. Do not add it.

The scraper cross-checks `category_upgrades` against the rankings block in the
same payload (free) plus the licence record, and retries before trusting it.

### Race categories are promoter free text

Across 610 distinct titles there is no reliable structure. The wave designator
appears on either side of the colon or mid-string:

```
15-18; 3; Master 3: Wave 1a      wave after the colon
1-1: JR 9-14 M                   wave before it
05 Wave 6 Novice Men 0-99 M      wave in the middle
```

Titles contain commas and pipes, so they break naive delimiters. Worse,
**combined waves put juniors and adults in the same field** — `15-18; 3; Master
3: Wave 1a` is juniors, Cat 3 and Master 3 starting together. Inferring a
rider's age from a wave title wrongly excluded 159 adults, including a
51-year-old Master. Use racing age from `licence` or the rankings sweep instead.

### Field size: starters vs the whole sheet

`total_riders` on the athlete results endpoint reproduces USA Cycling's own
balance exactly when fed into the points table — validated on 22 riders, 20
matching to the point. Use it as-is; do not subtract DNS.

(The race-results endpoint is a different matter: its sheets include DNS rows,
and excluding them matched better there.)

### Data quality

- Some `licence` values are 11-digit **UCI IDs**, not USA Cycling numbers. They
  return HTTP 500 on athlete endpoints.
- A handful of ordinary licence numbers also 500 on `/licence` — 8 of 681 in one
  run.
- Dates can be malformed. One 2012 event returned `race_category_date` of
  `Nov 30, -0001`. Validate before comparing, since `"Nov 30, -0001"` sorts
  *above* any ISO date as a string and will look like a recent result.

## Rate limiting

No published limit. The scraper defaults to 3 requests/second with retry and
backoff on 429 and 5xx, honouring `Retry-After`. Nothing here has ever been
rejected for rate at 8/second with 5 workers, but there is no reason to push it.
