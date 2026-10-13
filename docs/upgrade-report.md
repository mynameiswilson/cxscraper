# How the upgrade report is built

What `--athletes-csv` produces, where each number comes from, and which parts
are USA Cycling's and which are calculated here.

## Two tests, two clocks

USA Cycling's cyclocross upgrade chart defines two separate tests, and the
report shows both. Transcribed from
[USAC's chart](https://assets.usacycling.org/prod/documents/050726_CX.png):

| | Eligible (voluntary, 36 months) | Mandatory (12 months) |
|---|---|---|
| Cat 5 → 4 | finish 5 races | 10 points |
| Cat 4 → 3 | 10 points | 15 points **or** 2 wins in 20+ fields |
| Cat 3 → 2 | 15 points | 20 points **or** 2 wins in 20+ fields |
| Cat 2 → 1 | 20 points | 25 points **or** 2 wins in 20+ fields |

Cat 1 is the top of the ladder — no points-based upgrade above it — so Cat 1
riders are omitted. Sport80 offers them a "Pro" next step with no threshold.

Note the two-wins trigger does **not** apply to Cat 5, and that mandatory
thresholds sit on a 12-month clock while voluntary ones use 36.

### Upgrade points table

Points by finishing place and field size, in
[`usac_cx_upgrade_points.py`](../usac_cx_upgrade_points.py):

| Place | 5–10 | 11–20 | 21–35 | 36–60 | 61–80 | 81+ |
|---|---|---|---|---|---|---|
| 1 | 3 | 4 | 5 | 8 | 10 | 11 |
| 2 | 2 | 3 | 4 | 6 | 8 | 10 |
| 3 | 1 | 2 | 3 | 5 | 7 | 9 |
| 4 | – | 1 | 2 | 4 | 6 | 8 |
| 5 | – | – | 1 | 3 | 5 | 7 |
| 6 | – | – | – | 2 | 4 | 6 |
| 7 | – | – | – | 1 | 3 | 5 |

Continues to 11th in the largest bracket. Fields under 5 starters pay nothing.

**Mandatory upgrades are not enforced automatically.** USA Cycling's policy has
no auto-application language and includes a waiver provision, so riders can and
do sit far past a threshold. Treat `MAND` as "should be asked", not "is overdue
by rule".

## Who ends up in the report

Filters run in this order. Everything before the lookup saves requests.

1. **Riders in your results CSV** — the scraped file decides the population.
2. **Active within the window** — `--athletes-window-months`, default 36. Judged
   on the rider's most recent race *in your data*.
3. **Not a junior** — racing age 18 or under, checked before any points data is
   fetched.
4. **Has a published threshold** — drops Cat 1.
5. **At least N upgrade points** — `--athletes-min-points`, default 1.
6. **Status worth listing** — `MAND`, `Elig` or `Almost`.
   `--athletes-include-no` keeps the rest.

Each exclusion is counted in the run output.

### Junior exclusion uses age, not wave titles

Promoters run combined waves — `15-18; 3; Master 3: Wave 1a` is juniors, Cat 3
and Master 3 in one field. Inferring age from the wave title excluded **159
adults**, including a 51-year-old Master.

Racing age comes from, in order:

1. The bulk rankings sweep (~60 requests, covers every ranked rider in the
   scraped states, no per-rider cost).
2. `/pub/athletes/{id}/licence` for anyone the sweep missed — one request, and
   only `racing_age` and the CX category are read from it.
3. If age cannot be established, the wave-title rule, conservatively: wrongly
   excluding an adult leaves a gap, wrongly including a minor breaks the rule.

Juniors are dropped *before* their profile or results are requested, so no
points data is collected for them at all.

## The columns

| Column | Source |
|---|---|
| `Racer Name` | Your results CSV, as a `=HYPERLINK()` to their USA Cycling profile |
| `USAC ID` | Licence number — the join key back to the results CSV |
| `Status` | Calculated (below) |
| `Points: Mandatory` | `earned/required` against the 12-month test |
| `Points: Eligible` | `earned/required` against the 36-month test |
| `Races (36 mo)` | Count from the rider's USA Cycling history |
| `Race Log` | Point-scoring races, newest first |

`earned` on the Eligible test is USA Cycling's own published balance. A `*`
marks Category 5, where those figures are race starts rather than points.

### Status

| | |
|---|---|
| `MAND` | Meets the mandatory test. Evaluated first — it outranks everything |
| `Elig` | At or over the voluntary requirement |
| `Almost` | From 70% of it |
| `No` | Below 70% |

The 70% boundary is compared as `earned * 10 >= required * 7` rather than
against `0.7 * required`, so it lands exactly: 7 of 10 is `Almost`, 6.9 is not.

## What is USA Cycling's and what is calculated

**Theirs, used as published:** current and next category, points earned,
points required, race history, racing age.

**Calculated here:**

- **The 12-month mandatory subtotal.** USA Cycling publishes no 12-month figure
  and no mandatory flag, so it is summed from the race history using the points
  table above.
- **Per-race points in the Race Log.** They publish a total but never a
  breakdown, so each line is derived from place and field size.

The derived figures were validated against the official balance: on 22 sampled
riders the race log summed to `points_earned` **exactly for 20**, the two
exceptions being upgrade resets.

### The upgrade-reset problem

USA Cycling zeroes a rider's points when they upgrade and publishes no upgrade
dates, so a race history contains races that no longer count. One Cat 4 rider's
log summed to 15 points in 12 months while USA Cycling credited 8.

Two mechanisms handle this:

**The cap.** A 12-month subtotal cannot exceed the 36-month official balance,
because the shorter window sits inside the longer one. When the computed figure
does, it is counting races USA Cycling no longer credits, so it is capped at the
official number. That rider's Mandatory reads `8/15`, matching USA Cycling —
not the `15/15` the raw log implied. Not applied to Cat 5, whose balance counts
starts.

**The marker.** A reset zeroes everything *before* a date, so the surviving
races are a contiguous most-recent run. When the newest-first running total hits
the official balance **exactly** and older races remain, that boundary is where
the reset fell, and the log shows it:

```
2025/11/23 ..., 1 Points | 2025/10/25 ..., 2 Points | 2025/10/18 ..., 5 Points |
[Cat 4 Upgrade?] | 2025/09/28 ..., 2 Points | 2025/09/21 ..., 5 Points
```

Above the marker: 8 points, exactly what USA Cycling credits. Below: 7 that no
longer count.

Only exact boundaries are marked. Several scattered subsets of a rider's races
can sum to the same balance — contiguity plus an exact landing is the whole of
the evidence. If the running total jumps past the balance, nothing is placed,
because the gap could equally be an uncredited result. The `?` is doing real
work: this is inference, not published data.

## Two windows, independently set

- `--athletes-window-months` (default 36) decides **who gets looked up**.
- The **Race Log always covers 36 months**, USA Cycling's points window,
  regardless of the above. `--athletes-log-months` overrides it.

These were a single setting once, and it was a bug. With a 12-month selection
window the log was also truncated to 12 months, so a rider whose points came
from 2023–24 races showed a 12-month balance with an empty log that could not
account for it.

## Cost

Roughly, for a three-state 12-month report:

| | |
|---|---|
| Rankings sweep | ~60 requests, once |
| Licence lookup | 1 per rider the sweep missed |
| Profile | 1 per non-junior rider, plus retries on category mismatch |
| Race history | 1–3 per non-junior rider (paging stops at the window edge) |

About 1,500 requests and ten minutes for ~680 riders at `--rate 8 --workers 5`.

Narrowing `--athletes-window-months` is the only lever that genuinely reduces
lookups. Status filters trim the report but not the run, since a rider's status
is unknown until their points come back.

## Known limitations

- **A rider who earns an upgrade and then stops racing drops off.** The activity
  window is judged on their racing, so someone sitting at exactly their
  requirement disappears once they go quiet — precisely when a nudge would help.
- **Coverage is your scraped states only.** A rider's points come from racing
  anywhere; the report finds them only if they raced in your states within the
  window.
- **A handful of licences 500 on `/licence`** (8 of 681 in one run), and those
  riders are dropped silently rather than published with partial data.
- **The junior fallback is conservative.** Where racing age cannot be
  established, the wave-title rule may exclude an adult.
