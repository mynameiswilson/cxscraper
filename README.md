# cxscraper

Scrapes USA Cycling race results from the Sport80 platform and builds a
category-upgrade report for a set of states.

Two things live here:

1. **A results scraper** — walks USA Cycling's event search, every race day of
   every event, and every wave, and writes one CSV row per rider per race.
2. **An upgrade report** — takes that CSV, looks up each rider's official
   standing, and produces a human-readable report of who is eligible for (or
   required to take) a category upgrade.

Built for the Ohio Valley Cyclocross series (KY / IN / OH), but the scope is
just command-line arguments — any state and any of USA Cycling's disciplines
will work.

## Requirements

Python 3.9+ and two packages:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

`requests` and `tqdm` are the whole dependency list.

## Quick start

Scrape a season of results:

```bash
./.venv/bin/python cxscrape_sport80.py --states KY OH IN --from-date 2017-01-01 -o cx_results_sport80.csv
```

Build the upgrade report from that file:

```bash
./.venv/bin/python cxscrape_sport80.py --athletes-from cx_results_sport80.csv --athletes-window-months 12 --athletes-csv upgrades.csv --workers 5 --rate 8
```

Check the scope and cost of any run without making requests:

```bash
./.venv/bin/python cxscrape_sport80.py --states KY --from-date 2017-01-01 --dry-run
```

## Updating an existing scrape

Re-scraping years of results to pick up last weekend's races is wasteful.
`--update` reads an existing results CSV, starts from its latest race date, and
appends only what is new:

```bash
./.venv/bin/python cxscrape_sport80.py --update cx_results_sport80.csv --workers 4 --rate 8
```

States are taken from the file, so there is nothing else to pass. Races already
present are skipped by id, so the run is safe to repeat — a second run finds
nothing and costs a handful of requests.

It does **not** start exactly at the latest date. Organisers post results at
their own pace, so a race can be scraped before its results exist, and an event
can appear with a date weeks in the past. `--update` therefore rewinds
`--update-lookback` days (default 30) and re-checks that window, relying on the
id skip-list to avoid duplicating anything. Widen it if your region is
particularly slow to post.

Scale of the saving, updating a 114k-row file two weeks stale:

```
Dry run: 12 events, 164 races, 24 requests made
```

versus tens of thousands of requests to rebuild from 2017.

## The results CSV

One row per rider per race. The column names match the pre-Sport80 scraper so
existing spreadsheets keep working:

```
year, race_name, race_location, race_date_raw, race_date_start, race_date_end,
race_category, race_category_class, race_category_date, race_category_gender,
Place, Points, racer_name, racer_city_state, Time, racer_usac_id, Bib,
racer_team, race_wave_id, race_info_id, permit_id, race_state, results_link
```

Notes on a few of them:

| Column | |
|---|---|
| `Points` | **Ranking** points, not upgrade points. Different system entirely. |
| `race_category` | The promoter's verbatim wave title. |
| `race_category_class` | Juniors / Elite / Experienced / Novice / Singlespeed / Other, bucketed from the title. |
| `race_category_gender` | Men / Women / All / Unknown, from the title. Mostly `Unknown` unless `--gender-fallback` is used. |
| `race_info_id` | `{event}/{discipline}/{date}` — identifies one race day of one event. |
| `permit_id` | The Sport80 event id. The platform publishes no permit number. |

`--extended` appends Sport80-native fields (`result_id`, `race_field_size`,
athlete profile id and URL, `scraped_at`, and others).
`--events-csv` writes a separate one-row-per-race file with field sizes.

## The upgrade report

A formatted report rather than a flat table — masthead, one section per
category, and footnotes:

```
THE IN, KY, OH CX UPGRADE REPORT: October 8, 2026
This report is NOT an OFFICIAL USAC publication, ...

Racer Name, USAC ID, Status, Points: Mandatory, Points: Eligible, Races (36 mo), Race Log
Cat 5 -> Cat 4
  <riders, most urgent first>

Cat 4 -> Cat 3
  ...

FOOTNOTES
  ...
```

`Racer Name` is a `=HYPERLINK()` formula, so the name is clickable straight
through to the rider's USA Cycling profile in Excel, Sheets or LibreOffice. Use
`--athletes-plain-names` for a bare name plus a separate URL column if you are
reading the file with pandas.

**Status** is one of:

| | |
|---|---|
| `MAND` | Meets USAC's mandatory-upgrade test |
| `Elig` | At or over the voluntary requirement |
| `Almost` | From 70% of the voluntary requirement |
| `No` | Below 70% — omitted by default, `--athletes-include-no` keeps them |

Rows sort by category (Cat 5 first), then status, then points.

[docs/upgrade-report.md](docs/upgrade-report.md) covers the upgrade rules, the
two time windows, and how the derived figures are calculated and checked.

## Useful flags

| Flag | |
|---|---|
| `--states` / `--all-states` | Which states to search |
| `--from-date` / `--to-date` | Event date range |
| `--event-ids` | Scrape specific events, skipping the search |
| `--update` | Update an existing results CSV — start from its latest date, append what's new |
| `--update-lookback` | Days to rewind before that date, for late-posted results (default 30) |
| `--dry-run` | Count events and races, make no result requests |
| `--resume` | Skip work already present in the output file |
| `--gender-fallback` | Fill `Unknown` genders from the platform (2 extra requests per race day) |
| `--athletes-window-months` | How recently a rider must have raced to be looked up (default 36) |
| `--athletes-min-points` | Minimum upgrade points to list a rider (default 1) |
| `--join-athletes` | Write a copy of the results CSV with each rider's category and points joined on |
| `--workers` / `--rate` | Concurrency and requests per second |

`--help` lists everything.

## Project layout

```
cxscrape_sport80.py        the scraper and report builder
usac_cx_upgrade_points.py  USAC's cyclocross upgrade-points table
docs/sport80-api.md        API reference and the traps in it
docs/upgrade-report.md     how the upgrade report is calculated
```

USA Cycling has moved all results onto Sport80, and `legacy.usacycling.org`
now returns 404. Anything written against that host no longer runs.

## Things that will bite you

Each of these cost real debugging time and is documented in full in
[docs/sport80-api.md](docs/sport80-api.md):

- **Event search is paginated** at 20 per page, and the parameter is `p`
  (0-indexed). `page`, `offset` and `limit` are silently ignored — you get page
  one back and no error.
- **One search hit can hide several race days.** Multi-day events keep each day
  in a separate panel. 2024 CX Nationals shows 14 races on the day the search
  links to, and 47 across all four days.
- **`Points` in race results are ranking points**, not upgrade points.
- **For Category 5 riders, USA Cycling's `points_earned` counts race finishes,
  not points.** Their upgrade rule is "finish 5 races".
- **Upgrade points reset to zero when a rider upgrades**, and USA Cycling does
  not publish upgrade dates.
- **The athlete profile endpoint is occasionally served wrong.** One rider
  alternated between "Category 1" and "Category 5, 8 of 5 points" on identical
  requests. The scraper cross-checks before trusting it.

## Rate limiting and etiquette

Defaults are 3 requests/second and a single worker. The client rate-limits
across all threads, retries 429s and 5xx with backoff, and honours
`Retry-After`. A full three-state results scrape is tens of thousands of
requests, so prefer `--resume` over re-running from scratch.

## Data and privacy

Output contains rider names, USA Cycling licence numbers, home cities and team
names — all of it already public on USA Cycling's site, but aggregated here into
one file.

Two deliberate choices in the code:

- The report **excludes juniors by racing age** (18 and under), decided before
  any profile request, so no points data is collected for minors.
- Only the **profile** endpoint is used for upgrade standing. The `licence`
  endpoint, which carries birth year, sex and UCI ID, is read **only** for
  racing age and category, and nothing from it is written to any output file.

`.gitignore` excludes the generated CSVs for this reason. Think before
committing them or publishing the report anywhere public.

## Licence and credit

Report format and OVCX context by Ben Wilson, Louisville Cyclocross Collective —
<results@ovcx.com>.

Not affiliated with or endorsed by USA Cycling. The reports this produces are
unofficial; USA Cycling's own rider profiles are authoritative.
