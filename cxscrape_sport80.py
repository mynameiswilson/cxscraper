#!/usr/bin/env python3
"""
USA Cycling cyclocross results scraper — Sport80 platform (current as of 2025).

Replaces the legacy legacy.usacycling.org scrapers (cxscrape.py, cxscrape2.py).
The new platform is a JSON API, so there is no HTML parsing here.

API shape
---------
Event search (paginated, 20/page, `p` is a 0-indexed page number):
    /pub/results/datatable?discipline=CX&state=KY&from_date=2017-01-01          -> table config
    /pub/results/datatable?data=1&discipline=CX&state=KY&from_date=...&p=0      -> rows
    params: discipline, state, from_date, to_date, p

Event page (Inertia.js; send `X-Inertia: true` to get JSON instead of HTML):
    /pub/results/events/{event_id}/d/{discipline}/{race_date}
    props.event_results_page.race_results_datatables -> [{race_id, title, url}]
    props.event_results_page.discipline_filters      -> the event's OTHER day/discipline panels

    IMPORTANT: a single search hit exposes only the event's *latest* day. Multi-day
    events (e.g. Nationals) keep each day in a separate panel, and each panel has its
    own race list. 2024 CX Nationals = 14 races on the search-linked day but 47 across
    all four. This scraper walks every matching panel in discipline_filters.

Race results (NOT paginated — one request returns the full field):
    /pub/results/races/{race_id}/datatable?data=1

Output
------
Columns match the legacy cxscrape2.py CSV (see FIELDNAMES). race_category_class and
race_category_gender are classification cascades transcribed from the data owner's
Google Sheets IFS() formulas; see _CLASS_RULES / _GENDER_RULES, where order matters.

--athletes-csv builds the upgrade report. Everything in it comes from USAC:

  * points_earned / points_required / categories — /pub/athletes/{id}/profile,
    category_upgrades. This is the authority; nothing here is derived.
  * points_audit_log, races_in_window — /pub/athletes/{id}/results/
    datatable, the rider's own CX history, newest race first.

The scraped race CSV is used only to decide WHO to look up (riders with a result in
the last 36 months, juniors excluded). Two caveats on the audit log:

  * USAC publishes no per-race upgrade points, only the aggregate balance, so the
    points on each log entry are derived from place + field size via the published
    table (usac_cx_upgrade_points.py). The log explains the balance; it does not
    replace it. Sums may differ from points_earned — points reset to zero on
    upgrade and the API exposes no upgrade dates.
  * Field size is the endpoint's `total_riders`, used as-is. Validated against the
    platform's own balance: on 22 sampled riders the log summed to points_earned
    exactly for 20, the other two being upgrade resets.

For Category 5 riders points_earned/points_required count RACES FINISHED, not
points (USAC's Cat 5 -> 4 criterion is "finish 5 races"). Verified against 9 Cat 5
riders: mean error 0.22 against finishes, 4.89 against points.

Usage
-----
    python cxscrape_sport80.py --states KY OH IN --from-date 2017-01-01
    python cxscrape_sport80.py --all-states --from-date 2015-01-01 -o national.csv
    python cxscrape_sport80.py --states KY --dry-run          # count races, fetch no results
    python cxscrape_sport80.py --states KY --resume           # skip races already in the CSV
    python cxscrape_sport80.py --states KY --gender-fallback  # fill Unknown genders
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby
from datetime import date, datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Sibling module — resolves via sys.path[0] (this script's directory) whatever the cwd.
try:
    from usac_cx_upgrade_points import points_for
except ImportError:  # pragma: no cover
    sys.exit("usac_cx_upgrade_points.py must sit next to this script "
             "(it holds the USAC cyclocross upgrade-points table).")

try:
    from tqdm import tqdm
except ImportError:  # tqdm is optional — degrade to a no-op wrapper
    def tqdm(it=None, **kw):
        return it if it is not None else _NullBar()

    class _NullBar:
        def update(self, n=1): pass
        def close(self): pass
        def set_postfix_str(self, s): pass


BASE = "https://usacycling.sport80.com"
SEARCH_URL = f"{BASE}/pub/results/datatable"
EVENT_URL = f"{BASE}/pub/results/events/{{event_id}}/d/{{discipline}}/{{race_date}}"
RACE_URL = f"{BASE}/pub/results/races/{{race_id}}/datatable"

SEARCH_PAGE_SIZE = 20  # server-side, not configurable

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# 50 states, as offered by the site's own filter. The platform does not list DC.
US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY",
]

# Discipline codes accepted by the search filter.
DISCIPLINES = [
    "RR", "CCR", "CRIT", "TT", "XC", "STXC", "CM", "DH", "SD", "DS", "MX",
    "CX", "PR", "SR", "IP", "SPR", "KEIR",
]

# Output schema, matching the legacy cxscrape2.py CSV (column names and order) so
# existing downstream analysis keeps working. Declared up front rather than inferred
# from the first row — the legacy scraper built its header from the first race's keys,
# which is why the old cyclocross_results.csv has an unnamed column in its header.
FIELDNAMES = [
    "year", "race_name", "race_location",
    "race_date_raw", "race_date_start", "race_date_end",
    "race_category", "race_category_class", "race_category_date",
    "race_category_gender",
    "Place", "Points", "racer_name", "racer_city_state", "Time",
    "racer_usac_id", "Bib", "racer_team",
    "race_wave_id", "race_info_id", "permit_id", "race_state", "results_link",
]

# Appended by --extended: Sport80-native identifiers and fields with no legacy column.
EXTENDED_FIELDNAMES = [
    "place_type", "splits", "racer_athlete_id", "racer_profile_url",
    "platform_gender", "event_id", "race_field_size", "result_id", "scraped_at",
]

# Full state names as the platform renders them in `location`, for race_state fallback.
STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Client:
    """Rate-limited, retrying JSON client for the Sport80 public endpoints."""

    def __init__(self, rate=2.0, retries=4, timeout=30, verbose=False):
        self.min_interval = 1.0 / rate if rate > 0 else 0.0
        self.timeout = timeout
        self.verbose = verbose
        self._lock = threading.Lock()
        self._next_ok = 0.0
        self.request_count = 0

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _throttle(self):
        if not self.min_interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_ok - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            # small jitter so concurrent workers don't march in lockstep
            self._next_ok = now + self.min_interval * random.uniform(0.9, 1.1)

    def get_json(self, url, params=None, inertia=False):
        self._throttle()
        headers = {}
        if inertia:
            # Inertia.js content negotiation: same URL, JSON instead of a full HTML shell.
            headers["X-Inertia"] = "true"
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Referrer"] = url
        if self.verbose:
            print(f"[GET] {url} {params or ''}", file=sys.stderr)
        resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        with self._lock:
            self.request_count += 1
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(
                f"Expected JSON from {resp.url} but got {resp.headers.get('content-type')}"
            )


# --------------------------------------------------------------------------
# Value flattening
# --------------------------------------------------------------------------

def flat(value):
    """Collapse a Sport80 cell value to a plain string.

    Cells are either scalars or typed dicts:
      {"type":"unset"}                                   -> ""
      {"type":"hyperlink","url":...,"value":"NAME"}      -> "NAME"
      {"text":"1","type":"gold"}                         -> "1"   (place; type is the medal)
      {"type":"date","month":"Nov","day":"23","year":..} -> "2025-11-23"
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        t = value.get("type")
        if t == "unset":
            return ""
        if t == "date":
            return iso_date(value)
        if "value" in value:
            return str(value["value"])
        if "text" in value:
            return str(value["text"])
        return ""
    return str(value)


def cell_type(value):
    """The `type` tag of a cell, if any — e.g. gold/silver/bronze/result for place."""
    return value.get("type", "") if isinstance(value, dict) else ""


def cell_url(value):
    """Absolute URL from a hyperlink cell."""
    if isinstance(value, dict) and value.get("url"):
        return BASE + value["url"]
    return ""


def iso_date(d):
    """{'month':'Nov','day':'23','year':'2025'} -> '2025-11-23'."""
    if not isinstance(d, dict):
        return ""
    try:
        return datetime.strptime(
            f"{d['year']}-{d['month']}-{d['day']}", "%Y-%b-%d"
        ).strftime("%Y-%m-%d")
    except (KeyError, ValueError, TypeError):
        return ""


def split_location(text):
    """'Louisville, Kentucky' -> ('Louisville', 'Kentucky'). Best effort."""
    if not text or "," not in text:
        return text or "", ""
    city, _, region = text.rpartition(",")
    return city.strip(), region.strip()


def state_code(region):
    """'Kentucky' -> 'KY'. Returns the input unchanged if already a code."""
    if not region:
        return ""
    r = region.strip()
    if len(r) == 2 and r.isalpha():
        return r.upper()
    return STATE_CODES.get(r.lower(), "")


# Classification rules supplied by the data owner, transcribed from the Google
# Sheets IFS() formulas that previously ran over the race_category column. IFS
# returns on first match, so ORDER IS SIGNIFICANT — do not reorder these lists.
# Both cascades read the verbatim race_category title and nothing else.
_CLASS_RULES = [
    ("Juniors", r"(?i)\b(10|11|12|13|14|15|16|17|18)\b|jr|junior"),
    ("Elite", r"(?i)(elite|pro|cat\s?1|cat\?2|1\/2\/3|men a|1\/2|U23|1234)"),
    ("Experienced", r"(?i)(cat\s?3\s?$|cat\s?4\s?$|master|mr|3\/4|3\-4|men b|"
                    r"men\sm|\+|men\s?3|men\s?4)"),
    ("Novice", r"(?i)(novice|cat\s?5|men 5|4\/5|4\-5|beginner|men c|citizen)"),
    ("Singlespeed", r"(?i)(single|ss|speed)"),
]
CLASS_DEFAULT = "Other"

_GENDER_RULES = [
    ("Men", r"(?i)\bmen\b"),
    ("Women", r"(?i)\bwomen\b"),
    ("All", r"(?i)\ball\b"),
]
GENDER_DEFAULT = "Unknown"

_CLASS_COMPILED = [(label, re.compile(p)) for label, p in _CLASS_RULES]
_GENDER_COMPILED = [(label, re.compile(p)) for label, p in _GENDER_RULES]


def _first_match(title, rules, default):
    """IFS() semantics: first rule whose pattern is found wins."""
    s = title or ""
    for label, rx in rules:
        if rx.search(s):
            return label
    return default


def category_class(title):
    """Bucket a category title: Juniors / Elite / Experienced / Novice /
    Singlespeed / Other. First matching rule wins."""
    return _first_match(title, _CLASS_COMPILED, CLASS_DEFAULT)


def category_gender(title):
    """Bucket a category title: Men / Women / All / Unknown.

    'Men' is tested before 'Women', which is safe: \\b prevents \\bmen\\b from
    matching inside "women" (the 'm' is preceded by a word character).
    """
    return _first_match(title, _GENDER_COMPILED, GENDER_DEFAULT)


# Maps the platform's own gender codes onto the rule vocabulary, for --gender-fallback.
PLATFORM_GENDER_LABELS = {"F": "Women", "N": "All", "M": "Men"}

# Athlete upgrade-points output. Deliberately carries no name, city, age, sex or UCI
# id: racer_usac_id is the join key back to the results CSV, and /profile (unlike
# /licence) exposes none of those fields, so nothing identifying is collected here.
DEFAULT_ATHLETE_WINDOW_MONTHS = 36

# USAC counts upgrade points over 36 months. That is policy, not a preference, so
# the audit log always covers it regardless of how far back riders are selected
# from — otherwise a shorter selection window silently truncates the log and it
# stops accounting for points_earned.
USAC_POINTS_WINDOW_MONTHS = 36

# Mandatory upgrades run on a shorter clock than voluntary ones, with a second
# trigger that no points comparison can see: category -> (points in 12 months,
# wins in fields of 20+ starters). From USAC's cyclocross chart.
MANDATORY_WINDOW_MONTHS = 12
MANDATORY_RULES = {
    "5": (10, None),
    "4": (15, 2),
    "3": (20, 2),
    "2": (25, 2),
}
MANDATORY_MIN_FIELD = 20


# (internal key, report header). Headers are what lands in the CSV; the internal
# keys are used for building, sorting and reading the file back on --resume.
ATHLETE_COLUMNS = [
    ("racer_name", "Racer Name"),
    ("racer_usac_id", "USAC ID"),
    ("upgrade_eligible", "Status"),
    ("points_mandatory", "Points: Mandatory"),
    ("points_eligible", "Points: Eligible"),
    ("races_in_window", "Races (36 mo)"),
    ("points_audit_log", "Race Log"),
]
ATHLETE_HEADER_TO_KEY = {header: key for key, header in ATHLETE_COLUMNS}
ATHLETE_HEADER_TO_KEY.update({"Discipline": "discipline_group",
                              "Profile URL": "racer_profile_url"})

# Category is no longer a column — it heads each block instead, e.g. "Cat 3 -> Cat 2".
_SECTION_RE = re.compile(r"^Cat\s+(\S+)\s*->\s*(?:Cat\s+)?(\S+)$")


def section_label(current, nxt):
    """Block heading for a category. 'Pro' has no number, so it is not "Cat Pro"."""
    head = f"Cat {current}" if current else "Uncategorised"
    tail = f"Cat {nxt}" if (nxt or "").isdigit() else (nxt or "")
    return f"{head} -> {tail}" if tail else head


def parse_section_label(text):
    """'Cat 3 -> Cat 2' -> ('3', '2'). Used to restore category on --resume."""
    m = _SECTION_RE.match((text or "").strip())
    return (m.group(1), m.group(2)) if m else (None, None)


# Voluntary upgrade requirements, from USAC's cyclocross chart (36-month window).
UPGRADE_REQUIREMENTS = [
    ("5", "4", "finish 5 races"),
    ("4", "3", "10 points in 36 months"),
    ("3", "2", "15 points in 36 months"),
    ("2", "1", "20 points in 36 months"),
]


UPGRADE_POLICY_URL = "https://usacycling.org/category-upgrades"
UPGRADE_HELP_URL = ("https://usacyclinghelp.zendesk.com/hc/en-us/articles/"
                    "15860039287067-How-do-race-category-upgrades-work")
REPORT_CONTACT = "results@ovcx.com"
REPORT_CREDIT = ("Created by Ben Wilson, Louisville Cyclocross Collective. "
                 f"Contact {REPORT_CONTACT} for questions or details.")


def column_header(key):
    """The report header for an internal column key.

    Notes reference columns by name, so looking the name up keeps the prose in
    step with a rename instead of quietly pointing at a column that no longer
    exists.
    """
    return dict(ATHLETE_COLUMNS).get(key, key)


def _state_prose(states):
    """['IN','KY','OH'] -> 'IN, KY and OH'."""
    if not states:
        return "the scraped results"
    if len(states) == 1:
        return states[0]
    return f"{', '.join(states[:-1])} and {states[-1]}"


def report_preamble(states, window_months, discipline="CX", report_date=None):
    """Masthead, placed above the column headers.

    The date, state list and window length are interpolated from the actual run
    rather than hardcoded, so the masthead cannot drift out of step with the data.
    """
    when = report_date or datetime.now(timezone.utc).date().strftime("%B %d, %Y")
    listed = ", ".join(states) if states else "the scraped results"
    return [
        f"THE {listed} {discipline} UPGRADE REPORT: {when}",
        f"This report is NOT an OFFICIAL USAC publication, and is limited to "
        f"{listed} {discipline} racers active in the last {window_months} months.",
        "USA Cycling does not automatically apply upgrades. They may be requested "
        'by the rider OR race directors / USAC officials (for "mandatory" '
        "upgrades)",
        "Read the FOOTNOTES for more details.",
        "",
    ]


def report_footer(states, window_months, log_months=USAC_POINTS_WINDOW_MONTHS):
    """Notes block placed below the data."""
    prose = _state_prose(states)
    reqs = "; ".join(f"Cat {c}->{n}: {req}" for c, n, req in UPGRADE_REQUIREMENTS)
    # Built from the rules the code actually applies, so the note cannot drift.
    mand_points = "; ".join(f"Cat {c}: {pts}"
                            for c, (pts, _wins) in sorted(MANDATORY_RULES.items(),
                                                          reverse=True))
    return [
        "",
        "FOOTNOTES",
        f'Read USA Cycling\'s "How do race category upgrades work?" '
        f"{UPGRADE_HELP_URL}",
        "Click on a rider's name to see USA Cycling's current and official "
        "UPGRADE status for that rider.",
        f"* Data is pulled from USA Cycling sanctioned and reported races for ONLY "
        f"{prose} in the last {window_months} months.",
        f'* Upgrade "Eligible": {reqs}.',
        f'* Upgrade "Mandatory": Two wins in fields of {MANDATORY_MIN_FIELD}+ '
        f"starters OR {MANDATORY_WINDOW_MONTHS}-month points ({mand_points})",
        f'* Status: "{STATUS_MANDATORY}" at Mandatory req. "{STATUS_ELIGIBLE}" at '
        f'or over "Eligible" req, "{STATUS_ALMOST}" from 70% of it, '
        f'"{STATUS_NOT_YET}" below 70%.',
        f'* Points columns read "earned/required" for each test. A '
        f'"{STARTS_MARKER}" marks Category 5, whose Eligible figures are race '
        f"starts rather than points.",
        f"* {column_header('races_in_window')} total races over the last "
        f"{log_months} mo.",
        f"* {column_header('points_audit_log')} lists only points-awarding "
        f"finishes may include races that have already been accounted for in a "
        f"recent upgrade. [Cat X Upgrade?] in a "
        f"{column_header('points_audit_log')} marks where we've inferred an "
        f"upgrade appears to have reset a rider's points. Everything newer adds "
        f"up to the total USA Cycling currently credits them, and everything "
        f"older does not. USAC does not publish upgrade dates.",
        "* Juniors and Category 1 riders are excluded: junior categories are "
        "omitted, and Cat 1 is the top of the USAC cyclocross ladder with no "
        "points-based upgrade above it.",
        "",
        REPORT_CREDIT,
    ]


def read_athlete_rows(path):
    """Read a report back into internal-key rows, for --resume.

    The file is report-shaped — display headers, per-category section lines, blank
    separators and footnotes — so this locates the column header row, then keeps
    only rows carrying a licence, restoring each row's category from the section
    heading above it.
    """
    if not os.path.exists(path):
        return []
    rows = []
    keys = None
    current = nxt = ""
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.reader(f):
            if not raw or not any(c.strip() for c in raw):
                continue
            if raw[0].strip() == ATHLETE_COLUMNS[0][1]:      # "Racer Name"
                keys = [ATHLETE_HEADER_TO_KEY.get(h.strip(), h.strip()) for h in raw]
                continue
            single = raw[0].strip() and not any(c.strip() for c in raw[1:])
            if single:
                cur, nx = parse_section_label(raw[0])
                if cur:
                    current, nxt = cur, nx
                continue                                      # section or footnote
            if keys is None or "racer_usac_id" not in keys:
                continue
            idx = keys.index("racer_usac_id")
            if idx >= len(raw) or not raw[idx].strip().isdigit():
                continue
            row = dict(zip(keys, raw))
            row.setdefault("current_category", current)
            row.setdefault("next_category", nxt)
            rows.append(row)
    return rows


# Status vocabulary. MAND outranks ELIG: required to upgrade, not merely permitted.
STATUS_MANDATORY = "MAND"
STATUS_ELIGIBLE = "Elig"
STATUS_ALMOST = "Almost"
STATUS_NOT_YET = "No"

ALMOST_NUMERATOR, ALMOST_DENOMINATOR = 7, 10  # "Almost" from 70% of the requirement


def eligibility(earned, required):
    """'Yes' at or over the requirement, 'Almost' from 70%, otherwise 'No'.

    Compared as earned*10 >= required*7 rather than against 0.7*required, so the
    boundary is exact — 7 of 10 is Almost, not a float's-breadth short of it.
    """
    if not isinstance(earned, (int, float)) or not isinstance(required, (int, float)):
        return ""
    if required <= 0:
        return ""
    if earned >= required:
        return STATUS_ELIGIBLE
    if earned * ALMOST_DENOMINATOR >= required * ALMOST_NUMERATOR:
        return STATUS_ALMOST
    return STATUS_NOT_YET


def mandatory_tally(rides, since_12mo, cap=None):
    """(points, big-field wins) inside the mandatory window — the two triggers.

    `cap` is USAC's official 36-month balance. Any honest 12-month subtotal must
    fall at or under it, because the 12-month window sits inside the 36-month one.
    Exceeding it means the log is counting races USAC no longer credits — points
    reset to zero on upgrade, and the log has no way to see that a rider upgraded
    mid-season. Capping restores agreement with the official figure: a Cat 4 whose
    log summed to 15 while USAC credited 8 had earned exactly 7 of those points as
    a Cat 5 before upgrading.

    Not applied to Category 5, whose official balance counts starts, not points.
    """
    points = wins = 0
    for r in rides:
        if r["date"] and r["date"] < since_12mo:
            continue
        points += r["points"]
        if r["place"] == 1 and (r["starters"] or 0) >= MANDATORY_MIN_FIELD:
            wins += 1
    if isinstance(cap, (int, float)):
        points = min(points, int(cap))
    return points, wins


def wins_rule_met(category, wins):
    """Whether the two-wins trigger is satisfied. Cat 5 has no wins rule."""
    rule = MANDATORY_RULES.get(category or "")
    need_wins = rule[1] if rule else None
    return bool(need_wins) and wins >= need_wins


def must_upgrade(category, tally):
    """Whether USAC's MANDATORY upgrade test is met, per the cyclocross chart.

    Two triggers over the last 12 months: a points total, or two wins in fields of
    20+ starters. Derived here — USAC publishes neither a 12-month subtotal nor a
    mandatory flag.

    Judged on its own, ahead of and independent of the voluntary test, so a rider
    at or over the mandatory bar reads MAND whatever their voluntary standing.
    Post-upgrade over-counting is handled upstream by capping the tally against
    USAC's official balance (see mandatory_tally), not by gating here.
    """
    rule = MANDATORY_RULES.get(category or "")
    if not rule:
        return ""
    need_points, _need_wins = rule
    points, wins = tally
    if points >= need_points or wins_rule_met(category, wins):
        return STATUS_MANDATORY
    return ""


def category_number(name):
    """'Category 5' -> '5'. Anything without a number (e.g. 'Pro') passes through."""
    m = re.search(r"\d+", name or "")
    return m.group() if m else (name or "")

# Per-rider CX result history, straight from USAC. Rows come back date-descending
# and carry place, field size and the age band; they do NOT carry upgrade points
# (USAC publishes only the aggregate balance), so per-race points in the audit log
# are derived from place + field size via the published table.
ATHLETE_PROFILE_PAGE = f"{BASE}/pub/athletes/{{licence}}/profile"
ATHLETE_RESULTS_URL = f"{BASE}/pub/athletes/{{licence}}/results/datatable"
ATHLETE_RESULTS_PAGE_SIZE = 20
ATHLETE_RESULTS_MAX_PAGES = 12  # safety cap; ~240 results per rider

_HYPERLINK_LABEL_RE = re.compile(r'^=HYPERLINK\(".*?","(.*)"\)$', re.S)


def hyperlink_label(value):
    """The display text of a =HYPERLINK() cell, or the value itself if it is plain.

    Needed because racer_name may hold a formula, and sorting on the raw string
    would order by URL (i.e. by licence number) instead of by name.
    """
    m = _HYPERLINK_LABEL_RE.match(value or "")
    return m.group(1).replace('""', '"') if m else (value or "")


# Report order within a category: most urgent status first.
STATUS_ORDER = {STATUS_MANDATORY: 0, STATUS_ELIGIBLE: 1,
                STATUS_ALMOST: 2, STATUS_NOT_YET: 3}

# Statuses the report lists. Riders below 70% of their requirement are omitted:
# the report exists to prompt upgrades, and they have nothing to act on yet.
REPORTED_STATUSES = (STATUS_MANDATORY, STATUS_ELIGIBLE, STATUS_ALMOST)


def athlete_sort_key(row):
    """Lowest category first (Cat 5 -> Cat 1), then status, then points, then name.

    Accepts a bare number ('5'), the platform's long form ('Category 5'), or 'Pro'.
    Pro sits above Cat 1 on USAC's ladder, so it follows the numbered categories;
    anything unrecognised sorts last.
    """
    cat = (row.get("current_category") or "").strip()
    m = re.search(r"\d+", cat)
    if m:
        order = -int(m.group())   # 5 before 4 before ... before 1
    else:
        order = 1 if cat else 2   # 'Pro' after Cat 1; blanks last
    # Points are packed into display cells ("16 / 10"), so sort on the values
    # stashed at build time, falling back to each cell's leading number for rows
    # re-read from disk during --resume. Mandatory outranks eligible: being
    # required to upgrade is more urgent than being permitted to.
    def points(stash_key, cell_key):
        v = row.get(stash_key)
        if v is None:
            v = leading_number(row.get(cell_key))
        try:
            return float(v)
        except (TypeError, ValueError):
            return -1

    status = STATUS_ORDER.get((row.get("upgrade_eligible") or "").strip(),
                              len(STATUS_ORDER))
    mand = points("_sort_mandatory", "points_mandatory")
    elig = points("_sort_points", "points_eligible")
    name = row.get("_sort_name") or hyperlink_label(row.get("racer_name"))
    return (order, status, -mand, -elig, name.upper())


def _clean_event_name(name):
    """'Rockburn Cross | CX | 50-99' -> ('Rockburn Cross', '50-99')."""
    parts = [p.strip() for p in (name or "").split("|")]
    event = parts[0] if parts else ""
    band = parts[2] if len(parts) > 2 else ""
    return event, band


def _place_number(place):
    """'19/41' -> 19; 'DNS/19' -> None."""
    head = str(place or "").split("/")[0].strip()
    return int(head) if head.isdigit() else None


def athlete_cx_results(client, licence, discipline_slug="cx", since=None,
                       max_pages=ATHLETE_RESULTS_MAX_PAGES):
    """One rider's results for a discipline, newest first, from USAC.

    /pub/athletes/{id}/results/datatable?data=1&discipline_slug=cx is paginated
    20/page and already ordered date-descending, so paging stops as soon as a row
    predates `since` — usually after one or two pages.

    Field size here is `total_riders`, which counts everyone on the sheet including
    DNS. USAC's bracket table keys on STARTERS, so a race with DNS entries can land
    one bracket high; this endpoint exposes no DNS breakdown to correct for.
    """
    out = []
    url = ATHLETE_RESULTS_URL.format(licence=licence)
    for page in range(max_pages):
        payload = client.get_json(url, params={
            "data": 1, "discipline_slug": discipline_slug, "p": page})
        rows = payload.get("data") or []
        if not rows:
            break
        stop = False
        for row in rows:
            rdate = (row.get("race_date") or "").strip()
            if since and rdate and rdate < since:
                stop = True
                continue
            event, _band = _clean_event_name(row.get("event_name"))
            total = row.get("total_riders") or 0
            place = _place_number(row.get("place"))
            out.append({
                "date": rdate,
                "race": event,
                "place": place if place is not None else flat(row.get("place")),
                "starters": total,
                "points": points_for(place, total) if place else 0,
            })
        total_rows = payload.get("total")
        if stop or len(rows) < ATHLETE_RESULTS_PAGE_SIZE:
            break
        if total_rows is not None and (page + 1) * ATHLETE_RESULTS_PAGE_SIZE >= total_rows:
            break
    return out


def athlete_upgrades(client, licence, discipline_slug=None):
    """Upgrade-point standing for one rider, from /pub/athletes/{id}/profile.

    Returns (category_upgrades, ranking_category):

      category_upgrades — one entry per discipline group, each with current/next
        category and points earned vs required.
      ranking_category — the same rider's category according to athlete_rankings
        in the SAME payload, for `discipline_slug`.

    The second value exists because category_upgrades is sometimes served wrong.
    One rider came back as "Category 5, 8 of 5 points" while the rankings block in
    that very response still read "Category 1" — a self-contradicting payload, and
    the licence record agreed with the rankings. Reading both lets a bad response
    be spotted for free, without a second request.

    `discipline_slug` is deliberately NOT sent as a query parameter: filtering the
    profile to one discipline made the bad variant reproduce every single time,
    while the unfiltered profile returned the correct category on 9 calls in 10.
    """
    payload = client.get_json(f"{BASE}/pub/athletes/{licence}/profile", inertia=True)
    page = payload.get("props", {}).get("athlete_profile_page")
    if not page:
        raise RuntimeError(f"no athlete_profile_page for licence {licence}")
    ranking_category = None
    if discipline_slug:
        for entry in page.get("athlete_rankings") or []:
            if (entry.get("discipline_slug") or "").lower() == discipline_slug.lower():
                ranking_category = entry.get("discipline_category")
                break
    return (page.get("category_upgrades") or []), ranking_category


def panel_genders(client, event_id, discipline_slug, race_date):
    """Map race_id -> 'F' | 'N' | 'M' using the platform's own gender filters.

    The event page exposes ?gender=F/N/M. F and N return precise subsets; M is
    unreliable (on some events it returns every male-eligible race, i.e. all of
    them), so M is used only as the fallback for races in neither F nor N. Verified
    against events with explicit M/F categories: derived values match the titles.
    """
    url = EVENT_URL.format(event_id=event_id, discipline=discipline_slug,
                           race_date=race_date)
    out = {}
    for g in ("F", "N"):
        try:
            payload = client.get_json(url, params={"gender": g}, inertia=True)
        except (requests.RequestException, RuntimeError) as e:
            print(f"[WARN] gender={g} lookup failed for event {event_id} "
                  f"{race_date}: {e}", file=sys.stderr)
            continue
        page = payload.get("props", {}).get("event_results_page") or {}
        for race in page.get("race_results_datatables") or []:
            rid = race.get("race_id")
            if rid is not None:
                out.setdefault(rid, g)
    return out


# --------------------------------------------------------------------------
# Scrape stages
# --------------------------------------------------------------------------

def search_events(client, state=None, discipline="CX", from_date=None, to_date=None):
    """Yield event summary dicts from the paginated search datatable."""
    params = {"data": 1}
    if discipline:
        params["discipline"] = discipline
    if state:
        params["state"] = state
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date

    page = 0
    seen = 0
    total = None
    while True:
        params["p"] = page
        payload = client.get_json(SEARCH_URL, params=params)
        rows = payload.get("data") or []
        if total is None:
            total = payload.get("total", 0)
        if not rows:
            break
        for row in rows:
            yield row
        seen += len(rows)
        if total is not None and seen >= total:
            break
        if len(rows) < SEARCH_PAGE_SIZE:
            break
        page += 1
        if page > 500:  # runaway guard
            print(f"[WARN] search pagination exceeded 500 pages for state={state}",
                  file=sys.stderr)
            break


def event_panels(client, event_id, discipline_slug, race_date, want_discipline):
    """Return (event_meta, [panel, ...]) for every day of `want_discipline` in an event.

    The search gives us one entry point; `discipline_filters` on that page lists the
    event's other day/discipline panels, each with its own race list. Panels for other
    disciplines (e.g. 'other') are skipped.
    """
    url = EVENT_URL.format(event_id=event_id, discipline=discipline_slug, race_date=race_date)
    payload = client.get_json(url, inertia=True)
    page = payload.get("props", {}).get("event_results_page")
    if not page:
        raise RuntimeError(f"No event_results_page in response for event {event_id}")

    want = (want_discipline or "").lower()
    ev_date = page.get("event_date") or {}
    start = iso_date(ev_date)
    end = iso_date(ev_date.get("to_date")) if ev_date.get("to_date") else ""
    meta = {
        "event_id": event_id,
        "event_name": page.get("event_name", ""),
        "event_location": page.get("event_location", ""),
        "event_date_start": start,
        "event_date_end": end,
        "event_date_raw": raw_date_range(ev_date),
    }

    # Panels to visit: the one we already loaded, plus any sibling CX day we haven't.
    panels = []
    visited = set()

    def add_panel(page_obj, panel_url):
        races = page_obj.get("race_results_datatables") or []
        panels.append({
            "url": BASE + panel_url if panel_url.startswith("/") else panel_url,
            "race_date": normalize_panel_date(page_obj),
            "races": races,
        })

    add_panel(page, f"/pub/results/events/{event_id}/d/{discipline_slug}/{race_date}")
    # Key off the panel the server actually served, not the one we asked for: an
    # unknown date (e.g. the --event-ids probe date) silently falls back to the
    # event's default panel, and trusting the requested date would fetch that same
    # panel again from discipline_filters and duplicate every one of its races.
    visited.add(active_panel_key(page, discipline_slug, race_date))

    for filt in page.get("discipline_filters") or []:
        qp = filt.get("query_param") or {}
        f_disc = (qp.get("discipline") or "").lower()
        f_date = qp.get("race_date") or ""
        if want and f_disc != want:
            continue
        if (f_disc, f_date) in visited:
            continue
        visited.add((f_disc, f_date))
        sub = client.get_json(BASE + filt["url"], inertia=True)
        sub_page = sub.get("props", {}).get("event_results_page")
        if sub_page:
            add_panel(sub_page, filt["url"])

    return meta, panels


def active_panel_key(page_obj, requested_slug, requested_date):
    """(discipline, date) of the panel this response actually represents.

    discipline_filters marks the served panel with active=True; fall back to the
    panel's own rendered date, then to what we requested.
    """
    for filt in page_obj.get("discipline_filters") or []:
        if filt.get("active"):
            qp = filt.get("query_param") or {}
            return ((qp.get("discipline") or "").lower(), qp.get("race_date") or "")
    return (requested_slug.lower(), normalize_panel_date(page_obj) or requested_date)


def raw_date_range(ev_date):
    """Human-readable event date, matching the legacy race_date_raw column.

    'Nov 23, 2025', or 'Dec 12, 2024 - Dec 15, 2024' for a multi-day event.
    """
    def one(d):
        if not isinstance(d, dict):
            return ""
        m, day, y = d.get("month"), d.get("day"), d.get("year")
        return f"{m} {day}, {y}" if m and day and y else ""

    start = one(ev_date)
    end = one(ev_date.get("to_date")) if isinstance(ev_date, dict) else ""
    return f"{start} - {end}" if end else start


def normalize_panel_date(page_obj):
    """'Nov 23, 2025' -> '2025-11-23'."""
    raw = page_obj.get("discipline_date") or ""
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def race_results(client, race_id):
    """Return the full result list for one race. This endpoint is never paginated."""
    payload = client.get_json(RACE_URL.format(race_id=race_id), params={"data": 1})
    rows = payload.get("data") or []
    total = payload.get("total")
    if total is not None and total != len(rows):
        print(f"[WARN] race {race_id}: total={total} but received {len(rows)} rows",
              file=sys.stderr)
    return rows


def build_row(result, race_ctx, event_ctx, field_size, scraped_at):
    """Flatten one API result row into the legacy CSV schema.

    Identifier mapping from the legacy permit/info/race hierarchy:
      race_wave_id  <- Sport80 race id (one wave/category), same role as before
      race_info_id  <- the day+discipline panel that owns the race list; Sport80
                       gives it no numeric id, so it is keyed "{event}/{disc}/{date}"
      permit_id     <- Sport80 event id. The new platform exposes no permit or
                       sanction number anywhere in its payloads, and the event id is
                       the equivalent top-level handle.
    """
    place = result.get("place")
    race_date = race_ctx["race_date"]
    row = {
        "year": (race_date or event_ctx["event_date_start"] or "")[:4],
        "race_name": event_ctx["event_name"],
        "race_location": event_ctx["event_location"],
        "race_date_raw": event_ctx["event_date_raw"],
        "race_date_start": event_ctx["event_date_start"],
        "race_date_end": event_ctx["event_date_end"],
        "race_category": race_ctx["race_category"],
        "race_category_class": race_ctx["race_category_class"],
        "race_category_date": race_date,
        "race_category_gender": race_ctx["race_category_gender"],
        "Place": flat(place),
        "Points": flat(result.get("points")),
        "racer_name": flat(result.get("athlete_name")),
        "racer_city_state": flat(result.get("athlete_location")),
        "Time": flat(result.get("time")),
        "racer_usac_id": flat(result.get("licence")),
        "Bib": flat(result.get("bib")),
        "racer_team": flat(result.get("team")),
        "race_wave_id": race_ctx["race_id"],
        "race_info_id": race_ctx["race_info_id"],
        "permit_id": event_ctx["event_id"],
        "race_state": event_ctx["race_state"],
        "results_link": event_ctx["event_url"],
    }
    row.update({
        "place_type": cell_type(place),
        "splits": flat(result.get("splits")),
        "racer_athlete_id": flat(result.get("athlete_id")),
        "racer_profile_url": cell_url(result.get("athlete_name")),
        "platform_gender": race_ctx.get("platform_gender", ""),
        "event_id": event_ctx["event_id"],
        "race_field_size": field_size,
        "result_id": flat(result.get("id")),
        "scraped_at": scraped_at,
    })
    return row


# --------------------------------------------------------------------------
# Resume support
# --------------------------------------------------------------------------

JUNIOR_CLASS = "Juniors"

# USAC juniors are U19, so racing age 18 and under.
JUNIOR_MAX_RACING_AGE = 18

# The profile's category_upgrades block is occasionally served stale, so it is
# confirmed against the licence record (or rankings bucket) before use.
PROFILE_CATEGORY_RETRIES = 3


def category_matches(upgrades, group, true_category):
    """Whether the profile's entry for `group` names the rider's real category."""
    for u in upgrades or []:
        if (u.get("discipline_group_name") or "").lower() == (group or "").lower():
            return (u.get("current_category_name") or "") == true_category
    return False

# Public rankings listing: licence + racing age in bulk, filterable by state,
# category and gender, 20 per page. One sweep (~60 requests for three states)
# resolves the ages of every ranked rider, sparing a per-rider licence lookup.
RANKINGS_URL = f"{BASE}/pub/rankings/d/{{discipline}}/datatable"
RANKINGS_CATEGORIES = ("5", "4", "3", "2", "1")
RANKINGS_GENDERS = ("M", "F", "N")
RANKINGS_PAGE_SIZE = 20


def rankings_ages(client, states, discipline_slug="cx"):
    """licence -> racing age, swept from the public rankings for `states`.

    Only ranked riders appear, and rankings key on a rider's registered home
    state, so coverage is partial — callers fall back to a per-rider lookup.
    """
    ages = {}
    url = RANKINGS_URL.format(discipline=discipline_slug)
    for state in states:
        for cat in RANKINGS_CATEGORIES:
            for gender in RANKINGS_GENDERS:
                page = 0
                while True:
                    try:
                        payload = client.get_json(url, params={
                            "data": 1, "state": state,
                            "category": f"{discipline_slug}_category_{cat}",
                            "gender": gender, "p": page})
                    except (requests.RequestException, RuntimeError) as e:
                        print(f"[WARN] rankings sweep {state} cat{cat} {gender}: "
                              f"{e}", file=sys.stderr)
                        break
                    rows = payload.get("data") or []
                    for row in rows:
                        lic = row.get("license")
                        if isinstance(lic, dict):
                            lic = lic.get("value")
                        if lic is None:
                            continue
                        age = row.get("racing_age")
                        # The query filtered by category, so membership settles it.
                        ages[str(lic)] = (age if isinstance(age, int) else None,
                                          f"Category {cat}")
                    total = payload.get("total") or 0
                    if not rows or (page + 1) * RANKINGS_PAGE_SIZE >= total:
                        break
                    page += 1
    return ages


def athlete_licence_info(client, licence):
    """(racing age, cyclocross category) from /pub/athletes/{id}/licence.

    Junior status has to come from the rider, not from the races they entered:
    promoters run combined waves like "15-18; 3; Master 3: Wave 1a", so a
    category title cannot tell a 17-year-old from the 51-year-old Master lining
    up beside them.

    Only racing_age is read. That endpoint also carries birth year, sex and UCI
    id; none of it is kept, and it is requested BEFORE the profile so a junior
    costs one request and no points data is collected for them at all.
    """
    payload = client.get_json(f"{BASE}/pub/athletes/{licence}/licence",
                              inertia=True)
    page = payload.get("props", {}).get("athlete_licence_page") or {}
    age = page.get("racing_age")
    category = None
    for entry in page.get("categories") or []:
        if (entry.get("text") or "").lower() == "cyclocross":
            category = entry.get("value")
            break
    return (age if isinstance(age, int) else None), category

# USAC counts upgrade points over a rolling 36-month window, and the platform
# reports a balance current as of the moment it is read — so a rider whose last
# result predates the window has a balance our data cannot speak to.


def months_before(months, today=None):
    """ISO date `months` before today, clamped to a valid day-of-month."""
    d = today or datetime.now(timezone.utc).date()
    total = d.year * 12 + (d.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    # Step back to the last valid day when the target month is shorter.
    day = d.day
    while day > 1:
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            day -= 1
    return date(year, month, 1).isoformat()


def new_rider():
    return {"classes": set(), "last": "", "races": 0, "name": ""}


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def iso_or_blank(*candidates):
    """First candidate that is really YYYY-MM-DD, else "".

    The window filter compares dates as strings, so a malformed value has to be
    rejected rather than passed through: the platform occasionally serves a junk
    race date (e.g. "Nov 30, -0001"), and 'N' > '2' would make such a row look
    newer than any real cutoff.
    """
    for c in candidates:
        c = (c or "").strip()
        if _ISO_DATE_RE.match(c):
            return c
    return ""


def months_between(iso_date, today=None):
    """Whole months from `iso_date` to today, for describing a window in a note."""
    if not iso_date:
        return None
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    n = today or datetime.now(timezone.utc).date()
    return (n.year - d.year) * 12 + (n.month - d.month)


def note_rider(riders, licence, category_class, race_date, name=""):
    """Accumulate licence -> {classes raced, most recent race date, count, name}."""
    if not licence:
        return None
    entry = riders.setdefault(licence, new_rider())
    entry["classes"].add(category_class or "")
    entry["races"] += 1
    if race_date and race_date > entry["last"]:
        entry["last"] = race_date
        if name:
            entry["name"] = name  # prefer the spelling on the most recent result
    elif name and not entry["name"]:
        entry["name"] = name
    return entry


def hyperlink_cell(url, text):
    """A spreadsheet HYPERLINK formula, so the rider's name itself is clickable.

    Excel, Google Sheets and LibreOffice all evaluate =HYPERLINK() on CSV import.
    Quotes inside the label are doubled for the formula's string literal; the csv
    writer then applies its own quoting on top, which the spreadsheet undoes.
    """
    label = (text or "").replace('"', '""')
    return f'=HYPERLINK("{url}","{label}")'


STARTS_BASED_CATEGORY = "Category 5"


def starts_note(earned):
    """Race-start count for the starts-based criterion.

    USAC's Cat 5 -> 4 rule is "Finish 5 Races", not a points total, and the
    platform reports finishes rather than points for these riders. Points Earned
    now always holds points, so this note is the only place the start count
    appears — it is what actually governs a Cat 5 upgrade.
    """
    n = int(earned) if isinstance(earned, (int, float)) else 0
    return f"{n} start{'' if n == 1 else 's'}"


STARTS_MARKER = "*"


def _num(value):
    """Format a points figure without a trailing .0."""
    if value is None or value == "":
        return "?"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f.is_integer() else str(f)


def packed_points(earned, required, starts_based=False):
    """'earned/required' for one test, e.g. '6/5*' or '10/10'.

    The marker flags a Category 5 row, where the figures are race starts rather
    than points — USAC's Cat 5 rule is "finish 5 races".
    """
    mark = STARTS_MARKER if starts_based else ""
    return f"{_num(earned)}/{_num(required)}{mark}"


def leading_number(packed):
    """First number out of a packed cell, for sorting rows read back from disk."""
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", packed or "")
    return float(m.group(1)) if m else None


def wins_note(wins):
    """Big-field win count, shown when the two-wins mandatory trigger is met."""
    return f"{wins} win{'' if wins == 1 else 's'}"


def _audit_safe(text):
    """Keep an event name from looking like an entry break.

    Only '|' is substituted — it separates entries, so one inside a name would
    read as a new race. Commas are left alone: the log is a visual reference, not
    something to be split on.
    """
    return (text or "").replace("|", "/").strip()


def upgrade_break(scoring_newest_first, balance):
    """Where an upgrade appears to have reset the rider's points, or None.

    USAC publishes no upgrade dates. What it does publish is a current balance,
    and a reset zeroes everything BEFORE a date — so the races still counting are
    the most recent contiguous run. If the newest-first running total hits the
    official balance exactly and older scoring races remain beyond it, that
    boundary is where the reset must have fallen.

    Only an exact hit is marked. Several scattered subsets of a rider's races can
    sum to the same balance, so the contiguity plus an exact landing is the whole
    of the evidence; a running total that jumps straight past the balance places
    nothing, because the discrepancy could as easily be an uncredited result or a
    field-size disagreement.
    """
    if not isinstance(balance, (int, float)):
        return None
    total = sum(r["points"] for r in scoring_newest_first)
    if total <= balance:
        return None
    run = 0
    for i, ride in enumerate(scoring_newest_first):
        run += ride["points"]
        if run == balance:
            return i + 1 if i + 1 < len(scoring_newest_first) else None
        if run > balance:
            return None
    return None


def format_audit_log(scoring, break_at=None, break_label=""):
    """Render the scoring rides as one readable string, newest race first.

        2025/12/08 Race Name, 1/17, 3 Points | 2025/11/23 Other Race, 4/22, 2 Points

    Per entry: date, event name, place/field size, points. The race category is
    deliberately omitted — USAC's per-rider results endpoint supplies only an age
    band, and for most races supplies none, so it rendered as an empty field.
    """
    parts = []
    for s in sorted(scoring, key=lambda x: (x["date"], x["race"]), reverse=True):
        d = (s["date"] or "").replace("-", "/")
        parts.append(f"{d} {_audit_safe(s['race'])}, "
                     f"{s['place']}/{s['starters']}, {s['points']} Points")
    if break_at is not None and break_label and 0 < break_at < len(parts):
        parts.insert(break_at, break_label)
    return " | ".join(parts)


def riders_from_csv(path):
    """Read a results CSV.

    Returns (riders, states) where riders maps licence -> {classes, last, name} and
    states is the sorted set of race_state values, used for the report's scope note.
    """
    riders = {}
    states = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        for col in ("racer_usac_id", "race_category_class"):
            if col not in cols:
                raise SystemExit(f"{path} has no {col} column — need a results CSV "
                                 f"written by this scraper.")
        if "race_category_date" not in cols and "race_date_start" not in cols:
            raise SystemExit(f"{path} has no race date column — need a results CSV "
                             f"written by this scraper.")
        for row in reader:
            note_rider(riders, (row.get("racer_usac_id") or "").strip(),
                       (row.get("race_category_class") or "").strip(),
                       iso_or_blank(row.get("race_category_date"),
                                    row.get("race_date_start")),
                       name=(row.get("racer_name") or "").strip())
            st = (row.get("race_state") or "").strip()
            if st:
                states.add(st)
    return riders, sorted(states)


def write_athlete_upgrades(client, riders, path, discipline, scraped_at,
                           workers=1, resume=False, since=None, dry_run=False,
                           min_points=1, keep_no_threshold=False,
                           disc_slug="cx", plain_names=False,
                           scope_states=None, window_months=None,
                           log_since=None, statuses=REPORTED_STATUSES):
    """Fetch and write upgrade-point standings, skipping juniors entirely.

    `riders` maps licence -> {"classes": set, "last": ISO date, "name": str}.
    Three filters run before any request is made, so excluded riders' profiles are
    never fetched at all:

      juniors      — anyone who appears in a Juniors category
      `since`      — anyone whose most recent result predates the points window
      `min_points` — anyone who never placed highly enough to score (default 1, so
                     riders with no upgrade points are neither polled nor listed)

    Note that min_points is judged on points computed from THIS dataset. A rider who
    scored only in races outside the scraped states is filtered out even though the
    platform would show them with a balance.
    """
    # Juniors are no longer pre-filtered from the race data: combined waves make a
    # category title an unreliable proxy for a rider's age. They are settled by
    # racing age during the lookup instead (see fetch_rider).
    stale = set()
    if since:
        stale = {lic for lic, r in riders.items()
                 if not r["last"] or r["last"] < since}

    targets = sorted(set(riders) - stale, key=lambda x: (len(x), x))

    mandatory_cutoff = months_before(MANDATORY_WINDOW_MONTHS)
    existing = read_athlete_rows(path) if resume else []
    done = {(r.get("racer_usac_id") or "").strip()
            for r in existing if (r.get("racer_usac_id") or "").strip()}
    if done:
        targets = [t for t in targets if t not in done]

    print(f"\nAthlete upgrade points: {len(riders)} riders seen"
          + (f", {len(stale)} with no result since {since}" if since else "")
          + (f", {len(done)} already done" if done else "")
          + f" -> {len(targets)} to check (juniors dropped by racing age below)")
    if not targets:
        return 0, 0, set()
    if dry_run:
        print("Dry run: no athlete profiles requested.")
        return 0, 0, set()

    # Bulk ages first: one sweep covers every ranked rider in these states and
    # spares that many per-rider licence lookups.
    ages = dict(rankings_ages(client, scope_states or [], disc_slug))
    print(f"Racing ages known for {len(ages)} riders from the rankings sweep "
          f"({client.request_count} requests so far); the rest are looked up "
          f"individually.")

    want = None if discipline.lower() == "all" else discipline.lower()
    disc_group = discipline if want else "Cyclocross"
    # discipline_group is dropped from the report, but with 'all' the rows would be
    # ambiguous without it, so put it back in that case only.
    cols = list(ATHLETE_COLUMNS)
    if want is None:
        cols.insert(2, ("discipline_group", "Discipline"))
    # With a plain-text name the link has nowhere to live, so expose it as a column.
    if plain_names:
        cols.append(("racer_profile_url", "Profile URL"))

    failed = []
    out_rows = []
    skipped_no_threshold = 0
    skipped_no_points = 0
    skipped_status = 0
    juniors = set()
    juniors_by_proxy = set()
    mismatched = []

    JUNIOR = object()   # sentinel: rider resolved as a junior, nothing fetched

    def safe(lic):
        """Age gate first, then upgrade standing and USAC result history."""
        try:
            age, true_category = ages.get(lic, (None, None))
            if age is None and true_category is None:
                age, true_category = athlete_licence_info(client, lic)
                ages[lic] = (age, true_category)
            if age is not None:
                if age <= JUNIOR_MAX_RACING_AGE:
                    juniors.add(lic)
                    return JUNIOR
            elif JUNIOR_CLASS in riders.get(lic, new_rider())["classes"]:
                # Age unknown: fall back to the category proxy. Conservative —
                # wrongly excluding an adult leaves a gap, wrongly including a
                # minor breaks the requirement.
                juniors.add(lic)
                juniors_by_proxy.add(lic)
                return JUNIOR

            # The profile's category_upgrades block is occasionally served stale:
            # one rider returned Category 5 (with points) and Category 1 (without)
            # on alternating calls to the same URL. The licence record and the
            # rankings bucket are both stable, so use whichever we already have to
            # confirm the block describes the rider's actual category, retrying a
            # few times before giving up on them.
            upgrades, ranked = athlete_upgrades(client, lic, disc_slug)
            # Prefer the licence/rankings-sweep category; otherwise fall back to
            # the rankings block carried in the profile payload itself, which is
            # free and was correct even inside a bad response.
            authority = true_category or ranked
            for _ in range(PROFILE_CATEGORY_RETRIES):
                if not authority or category_matches(upgrades, disc_group,
                                                     authority):
                    break
                upgrades, ranked = athlete_upgrades(client, lic, disc_slug)
                authority = true_category or ranked
            else:
                if authority and not category_matches(upgrades, disc_group,
                                                      authority):
                    mismatched.append((lic, authority))
                    return None

            # Log range is USAC's points window, NOT the rider-selection window.
            rides = athlete_cx_results(client, lic, discipline_slug=disc_slug,
                                       since=log_since or since)
            return lic, upgrades, rides
        except (requests.RequestException, RuntimeError, ValueError) as e:
            failed.append((lic, str(e)))
            return None

    bar = tqdm(total=len(targets), desc="athletes", unit="rider")
    if workers > 1:
        pool = ThreadPoolExecutor(max_workers=workers)
        stream = pool.map(safe, targets)
    else:
        pool = None
        stream = (safe(t) for t in targets)

    for item in stream:
        if item is None or item is JUNIOR:
            bar.update(1)
            continue
        lic, upgrades, rides = item
        # Audit log and race counts come from the rider's USAC history, restricted
        # to the points window. Our own scraped results only pick who to look up.
        rider = riders.get(lic, new_rider())
        scoring_rides = [r for r in rides if r["points"] > 0]
        # Same order the log renders in, so an upgrade break can be located by index.
        scoring_newest = sorted(scoring_rides,
                                key=lambda x: (x["date"], x["race"]), reverse=True)
        for up in upgrades:
            group = up.get("discipline_group_name") or ""
            if want and group.lower() != want:
                continue
            earned = up.get("points_earned")
            required = up.get("points_required")
            # No published threshold means no points-based upgrade to track — this
            # is every Category 1 rider, whose Sport80 "next category" is Pro.
            if required is None and not keep_no_threshold:
                skipped_no_threshold += 1
                continue
            # points_earned is the authority: drop riders USAC credits with nothing.
            if min_points > 0 and (not isinstance(earned, (int, float))
                                   or earned < min_points):
                skipped_no_points += 1
                continue
            # Eligibility is judged on the platform's own figures, which for
            # Category 5 are race finishes against a 5-race rule.
            current = up.get("current_category_name") or ""
            cat = category_number(current)
            starts_based = current == STARTS_BASED_CATEGORY
            # Cap the 12-month tally at USAC's official balance, except for
            # Category 5 where that balance counts starts rather than points.
            tally = mandatory_tally(rides, mandatory_cutoff,
                                    cap=None if starts_based else earned)
            # Mandatory is evaluated first and wins: being required to upgrade
            # outranks merely being permitted to.
            eligible = must_upgrade(cat, tally) or eligibility(earned, required)

            # Two tests, two windows, one packed cell each: the 36-month voluntary
            # test and the 12-month mandatory one. Category 5's voluntary figures
            # are race starts, flagged with the marker.
            mand_required = (MANDATORY_RULES.get(cat) or (None, None))[0]
            points_eligible = packed_points(earned, required, starts_based)
            points_mandatory = packed_points(tally[0], mand_required)

            if eligible not in statuses:
                skipped_status += 1
                continue

            # Mark where a reset appears to have fallen, if it can be placed.
            break_at = None if starts_based else upgrade_break(scoring_newest, earned)
            audit = format_audit_log(scoring_newest, break_at,
                                     f"[Cat {cat} Upgrade?]")

            notes = []
            if starts_based:
                notes.append(starts_note(earned))
            if wins_rule_met(cat, tally[1]):
                notes.append(wins_note(tally[1]))
            row_audit = " | ".join(notes + ([audit] if audit else []))
            profile = ATHLETE_PROFILE_PAGE.format(licence=lic)
            out_rows.append({
                "racer_name": (rider["name"] if plain_names
                               else hyperlink_cell(profile, rider["name"])),
                "racer_profile_url": profile,
                "_sort_name": rider["name"],  # dropped on write (extrasaction)
                "racer_usac_id": lic,
                "discipline_group": group,
                "current_category": cat,
                "next_category": category_number(up.get("next_category_name")),
                "points_eligible": points_eligible,
                "points_mandatory": points_mandatory,
                "_sort_points": earned,        # dropped on write
                "_sort_mandatory": tally[0],   # dropped on write
                "upgrade_eligible": eligible,
                "races_in_window": len(rides),
                "points_audit_log": row_audit,
            })
        bar.update(1)
    bar.close()
    if pool:
        pool.shutdown()

    # Sorting is global, so a resumed run rewrites the whole file rather than
    # appending out of order. Rows already on disk are merged back in; a freshly
    # fetched row wins over a stored one for the same rider.
    seen = {(r.get("racer_usac_id"), r.get("discipline_group", "")) for r in out_rows}
    for row in existing:
        key = (row.get("racer_usac_id"), row.get("discipline_group", ""))
        if key not in seen:
            seen.add(key)
            out_rows.append(row)

    out_rows.sort(key=athlete_sort_key)
    log_months = months_between(log_since) or USAC_POINTS_WINDOW_MONTHS
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for line in report_preamble(scope_states or [], window_months):
            writer.writerow([line] if line else [])
        writer.writerow([header for _key, header in cols])
        for _cat, block in groupby(out_rows, key=lambda r: r.get("current_category")):
            block = list(block)
            writer.writerow([section_label(block[0].get("current_category"),
                                           block[0].get("next_category"))])
            for row in block:
                writer.writerow([row.get(key, "") for key, _header in cols])
            writer.writerow([])          # separates this block from the next heading
        for line in report_footer(scope_states or [], window_months,
                                  log_months=log_months):
            writer.writerow([line] if line else [])

    if failed:
        print(f"[WARN] {len(failed)} athlete lookups failed", file=sys.stderr)
        for lic, err in failed[:10]:
            print(f"  licence {lic}: {err}", file=sys.stderr)
    if mismatched:
        print(f"[WARN] {len(mismatched)} riders skipped: the profile kept "
              f"reporting a category that disagrees with their licence record",
              file=sys.stderr)
        for lic, cat in mismatched[:10]:
            print(f"  licence {lic}: licence says {cat}", file=sys.stderr)
    if juniors:
        print(f"{len(juniors)} riders omitted as juniors by racing age"
              + (f" ({len(juniors_by_proxy)} of them by category, age unknown)"
                 if juniors_by_proxy else ""))
    if skipped_no_threshold:
        print(f"{skipped_no_threshold} riders omitted with no upgrade threshold "
              f"(Category 1 — top of the USAC ladder)")
    if skipped_no_points:
        print(f"{skipped_no_points} riders omitted with fewer than {min_points} "
              f"upgrade points per USAC")
    if skipped_status:
        print(f"{skipped_status} riders omitted as "
              f"{'/'.join(s for s in STATUS_ORDER if s not in statuses)}")
    print(f"{len(out_rows)} upgrade rows -> {path}")
    return len(out_rows), len(failed), juniors


def join_athlete_columns(results_path, athletes_path, out_path):
    """Append each rider's category and upgrade standing to the results CSV.

    Pure local join on racer_usac_id — no network. The category is already in the
    athlete profile we fetch for upgrade points, so this costs nothing extra.
    """
    cat, earned, req = {}, {}, {}
    for row in read_athlete_rows(athletes_path):
        lic = (row.get("racer_usac_id") or "").strip()
        if lic:
            cat[lic] = row.get("current_category") or ""
            earned[lic] = row.get("points_earned") or ""
            req[lic] = row.get("points_required") or ""

    added = ["racer_category", "racer_points_earned", "racer_points_required"]
    matched = 0
    with open(results_path, newline="", encoding="utf-8") as fin, \
            open(out_path, "w", newline="", encoding="utf-8") as fout:
        reader = csv.DictReader(fin)
        fields = [c for c in (reader.fieldnames or []) if c not in added] + added
        writer = csv.DictWriter(fout, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            lic = (row.get("racer_usac_id") or "").strip()
            if lic in cat:
                matched += 1
            row["racer_category"] = cat.get(lic, "")
            row["racer_points_earned"] = earned.get(lic, "")
            row["racer_points_required"] = req.get(lic, "")
            writer.writerow(row)
    print(f"Joined athlete category onto {matched} result rows -> {out_path}")
    return matched


def scan_results_csv(path):
    """(race_wave_ids, latest race date, states) from an existing results CSV.

    One pass, because this file runs to tens of megabytes. The date is the latest
    valid ISO race date in the file; malformed ones are ignored rather than
    compared, since a junk value like "Nov 30, -0001" sorts above any real date.
    """
    ids, states, latest = set(), set(), ""
    if not os.path.exists(path):
        raise SystemExit(f"{path} does not exist")
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        if "race_wave_id" not in cols:
            raise SystemExit(f"{path} has no race_wave_id column — needs a results "
                             f"CSV written by this scraper.")
        for row in reader:
            rid = (row.get("race_wave_id") or "").strip()
            if rid:
                ids.add(rid)
            st = (row.get("race_state") or "").strip()
            if st:
                states.add(st)
            when = iso_or_blank(row.get("race_category_date"),
                                row.get("race_date_start"))
            if when > latest:
                latest = when
    return ids, latest, sorted(states)


def existing_race_ids(path):
    """race_wave_ids already present in an output CSV, for --resume."""
    done = set()
    if not os.path.exists(path):
        return done
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "race_wave_id" not in reader.fieldnames:
                print(f"[WARN] {path} has no race_wave_id column; cannot resume "
                      f"from it", file=sys.stderr)
                return done
            for row in reader:
                rid = (row.get("race_wave_id") or "").strip()
                if rid:
                    done.add(rid)
    except OSError as e:
        print(f"[WARN] could not read {path} for resume: {e}", file=sys.stderr)
    return done


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Scrape USA Cycling (Sport80) race results to CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage\n-----")[-1],
    )
    scope = ap.add_argument_group("scope")
    scope.add_argument("--states", nargs="+", metavar="ST",
                       help="Two-letter state codes to search (e.g. KY OH IN).")
    scope.add_argument("--all-states", action="store_true",
                       help="Search all 50 states.")
    scope.add_argument("--discipline", default="CX", choices=DISCIPLINES,
                       help="Discipline code (default: CX).")
    scope.add_argument("--from-date", default="2017-01-01", metavar="YYYY-MM-DD",
                       help="Earliest event date (default: 2017-01-01).")
    scope.add_argument("--to-date", default=None, metavar="YYYY-MM-DD",
                       help="Latest event date (default: no limit).")
    scope.add_argument("--event-ids", nargs="+", type=int, metavar="ID",
                       help="Scrape only these event ids (skips the search stage). "
                            "Requires the event's discipline/date to be discoverable; "
                            "uses --from-date's year as a probe date if needed.")

    out = ap.add_argument_group("output")
    out.add_argument("-o", "--output", default="cx_results_sport80.csv",
                     help="Output CSV path (default: cx_results_sport80.csv).")
    out.add_argument("--resume", action="store_true",
                     help="Append to the output CSV, skipping race_ids already in it.")
    out.add_argument("--events-csv", default=None,
                     help="Also write one row per race to this CSV (field sizes, no riders).")
    out.add_argument("--update", metavar="RESULTS_CSV",
                     help="Update an existing results CSV instead of scraping "
                          "from scratch: start from its latest race date (less "
                          "--update-lookback), skip races already in it, and "
                          "append. States are taken from the file unless given.")
    out.add_argument("--update-lookback", type=int, default=30, metavar="DAYS",
                     help="How far before the file's latest race date an update "
                          "restarts (default: 30). Organisers post results at "
                          "their own pace, so a race already scraped may have "
                          "gained results since; this re-checks that window.")
    out.add_argument("--dry-run", action="store_true",
                     help="Enumerate events and races but fetch no results.")
    out.add_argument("--extended", action="store_true",
                     help="Append Sport80-native columns with no legacy equivalent "
                          "(place_type, splits, athlete id/profile, event_id, "
                          "race_field_size, result_id, scraped_at).")
    ath = ap.add_argument_group("athlete upgrade points")
    ath.add_argument("--athletes-csv", default=None, metavar="PATH",
                     help="Also fetch each rider's upgrade-point standing to this "
                          "CSV (one /pub/athletes/{id}/profile request per distinct "
                          "rider). Riders who appear in any Juniors category are "
                          "excluded and never requested.")
    ath.add_argument("--athletes-from", default=None, metavar="RESULTS_CSV",
                     help="Read rider licences from an existing results CSV instead "
                          "of scraping races. Requires --athletes-csv; skips the "
                          "search/event/results stages entirely.")
    ath.add_argument("--athletes-discipline", default="Cyclocross", metavar="GROUP",
                     help="Only emit this discipline group's upgrade row "
                          "(default: Cyclocross). Use 'all' for every group.")
    ath.add_argument("--athletes-window-months", type=int,
                     default=DEFAULT_ATHLETE_WINDOW_MONTHS, metavar="N",
                     help="Only look up riders with a result in the last N months "
                          "(default: 36, matching USAC's rolling upgrade-points "
                          "window). Riders whose last result is older have a "
                          "balance our data cannot account for. 0 disables the "
                          "filter and looks up every rider in the dataset.")
    ath.add_argument("--athletes-since", default=None, metavar="YYYY-MM-DD",
                     help="Explicit cutoff date instead of --athletes-window-months.")
    ath.add_argument("--athletes-log-months", type=int,
                     default=USAC_POINTS_WINDOW_MONTHS, metavar="N",
                     help="Months of race history the Race Log covers (default: 36, "
                          "USAC's upgrade-points window). Independent of "
                          "--athletes-window-months, which only chooses who to look "
                          "up: shortening the log would stop it accounting for "
                          "Points Earned.")
    ath.add_argument("--athletes-min-points", type=int, default=1, metavar="N",
                     help="Only list riders USAC credits with at least N upgrade "
                          "points (default: 1, i.e. drop riders with no points). "
                          "Judged on the platform's own points_earned, so it "
                          "filters the report rather than the lookups. 0 keeps "
                          "everyone.")
    ath.add_argument("--athletes-include-no", action="store_true",
                     help=f"Also list riders at status "
                          f"\"{STATUS_NOT_YET}\" (below 70%% of their "
                          f"requirement). By default the report carries only "
                          f"{', '.join(REPORTED_STATUSES)}.")
    ath.add_argument("--athletes-plain-names", action="store_true",
                     help="Write racer_name as plain text with the profile link in "
                          "its own racer_profile_url column. By default racer_name "
                          "is a =HYPERLINK() formula so the name is clickable in "
                          "Excel/Sheets — use this for pandas or other tooling that "
                          "wants the bare name.")
    ath.add_argument("--athletes-keep-no-threshold", action="store_true",
                     help="Keep riders the platform publishes no upgrade threshold "
                          "for. That is every Category 1 rider (USAC's top "
                          "cyclocross category; Sport80 shows 'Pro' as next with no "
                          "points figures), so they are omitted by default.")
    ath.add_argument("--join-athletes", default=None, metavar="OUT_CSV",
                     help="Write a copy of the results CSV with racer_category, "
                          "racer_points_earned and racer_points_required joined on "
                          "racer_usac_id. Local join, no extra requests — the "
                          "category comes free with the upgrade-points lookup.")

    out.add_argument("--gender-fallback", action="store_true",
                     help="Where the gender rule yields Unknown, fill it from the "
                          "platform's own gender filter (F->Women, N->All, M->Men). "
                          "Costs 2 extra requests per race day. Sport80 category "
                          "titles mostly use a trailing 'M'/'F' rather than the "
                          "words 'Men'/'Women', so the rule alone leaves ~81%% of "
                          "rows Unknown.")

    net = ap.add_argument_group("network")
    net.add_argument("--rate", type=float, default=3.0,
                     help="Max requests per second (default: 3).")
    net.add_argument("--workers", type=int, default=1,
                     help="Concurrent race-result fetches (default: 1). "
                          "The rate limit still applies across all workers.")
    net.add_argument("--retries", type=int, default=4,
                     help="Retries per request on 429/5xx (default: 4).")
    net.add_argument("--timeout", type=int, default=30, help="Per-request timeout seconds.")
    net.add_argument("-v", "--verbose", action="store_true", help="Log every request.")

    args = ap.parse_args()

    if args.athletes_from and not args.athletes_csv:
        ap.error("--athletes-from requires --athletes-csv")

    # --update rewrites the scope from the file it is updating, so it has to be
    # resolved before the scope check below.
    update_known = set()
    if args.update:
        if args.event_ids:
            ap.error("--update and --event-ids are mutually exclusive")
        update_known, latest, file_states = scan_results_csv(args.update)
        if not latest:
            ap.error(f"{args.update} has no usable race date to update from")
        start = (datetime.strptime(latest, "%Y-%m-%d").date()
                 - timedelta(days=max(0, args.update_lookback)))
        args.output = args.update
        args.resume = True
        args.from_date = start.isoformat()
        if not (args.states or args.all_states):
            if not file_states:
                ap.error(f"{args.update} has no race_state values; pass --states")
            args.states = file_states
        print(f"Updating {args.update}: {len(update_known)} races already present, "
              f"latest {latest}. Searching from {args.from_date} "
              f"({args.update_lookback}-day lookback) for "
              f"{', '.join(args.states or US_STATES)}.")

    if args.all_states:
        states = US_STATES
    elif args.states:
        states = [s.strip().upper() for s in args.states]
    elif args.event_ids:
        states = [None]
    elif args.athletes_from:
        states = []
    else:
        ap.error("specify --states, --all-states, --event-ids, --update, or "
                 "--athletes-from")

    for d in (args.from_date, args.to_date):
        if d:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                ap.error(f"dates must be YYYY-MM-DD, got {d!r}")

    client = Client(rate=args.rate, retries=args.retries,
                    timeout=args.timeout, verbose=args.verbose)
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    disc_slug = args.discipline.lower()

    # Cutoff for athlete lookups. Relative to now, not to the races being scraped:
    # the platform reports a balance current as of read time.
    if args.athletes_since:
        athlete_cutoff = args.athletes_since
    elif args.athletes_window_months > 0:
        athlete_cutoff = months_before(args.athletes_window_months)
    else:
        athlete_cutoff = None
    # Rider selection window (user's choice) vs points-log window (USAC policy).
    log_cutoff = months_before(args.athletes_log_months)
    if args.athletes_csv and athlete_cutoff:
        print(f"Athlete lookups limited to riders with a result since "
              f"{athlete_cutoff}; race log covers {args.athletes_log_months} "
              f"months back to {log_cutoff}.")

    # Standalone athlete mode: licences come from an existing results CSV, so the
    # search/event/results stages are skipped entirely.
    if args.athletes_from:
        riders, csv_states = riders_from_csv(args.athletes_from)
        write_athlete_upgrades(client, riders, args.athletes_csv,
                               args.athletes_discipline, scraped_at,
                               workers=args.workers, resume=args.resume,
                               since=athlete_cutoff, dry_run=args.dry_run,
                               min_points=args.athletes_min_points,
                               keep_no_threshold=args.athletes_keep_no_threshold,
                               disc_slug=disc_slug,
                               plain_names=args.athletes_plain_names,
                               scope_states=csv_states,
                               window_months=args.athletes_window_months,
                               log_since=log_cutoff,
                               statuses=(tuple(STATUS_ORDER)
                                         if args.athletes_include_no
                                         else REPORTED_STATUSES))
        if args.join_athletes and not args.dry_run:
            join_athlete_columns(args.athletes_from, args.athletes_csv,
                                 args.join_athletes)
        print(f"{client.request_count} HTTP requests.")
        return 0

    # --update already scanned the file; don't read tens of megabytes twice.
    done_races = update_known or (existing_race_ids(args.output)
                                  if args.resume else set())
    if args.resume and done_races and not args.update:
        print(f"Resuming: {len(done_races)} race_ids already in {args.output}")

    # ---- Stage 1: enumerate events -------------------------------------
    # (event_id, discipline_slug, race_date, search_state)
    entry_points = []
    seen_events = set()

    if args.event_ids:
        probe_date = args.from_date or "2020-01-01"
        for eid in args.event_ids:
            entry_points.append((eid, disc_slug, probe_date, None))
    else:
        for state in states:
            n = 0
            try:
                for ev in search_events(client, state=state, discipline=args.discipline,
                                        from_date=args.from_date, to_date=args.to_date):
                    eid = ev.get("id")
                    link = (ev.get("event_name") or {}).get("url") or ""
                    slug = ev.get("latest_discipline_slug") or disc_slug
                    rdate = ev.get("latest_race_date") or ""
                    if not rdate and link:
                        rdate = link.rstrip("/").rsplit("/", 1)[-1]
                    if eid is None or not rdate:
                        print(f"[WARN] skipping unparseable search row: {ev}", file=sys.stderr)
                        continue
                    if eid in seen_events:
                        continue  # same event can surface under multiple states
                    seen_events.add(eid)
                    entry_points.append((eid, slug, rdate, state))
                    n += 1
            except requests.RequestException as e:
                print(f"[ERROR] search failed for state={state}: {e}", file=sys.stderr)
                continue
            print(f"{state}: {n} events")

    print(f"\n{len(entry_points)} events to expand")

    # ---- Stage 2: expand events into races -----------------------------
    tasks = []  # (race_ctx, event_ctx)
    seen_races = set()  # a race_id belongs to exactly one panel; a repeat is a dupe
    bar = tqdm(total=len(entry_points), desc="events", unit="ev")
    for eid, slug, rdate, state in entry_points:
        try:
            meta, panels = event_panels(client, eid, slug, rdate, disc_slug)
        except (requests.RequestException, RuntimeError) as e:
            print(f"[ERROR] event {eid}: {e}", file=sys.stderr)
            bar.update(1)
            continue
        # race_state: the state we searched, falling back to the event's own
        # location (so --event-ids runs still populate the column).
        ev_state = state or state_code(split_location(meta["event_location"])[1])
        event_ctx_base = dict(meta, race_state=ev_state, discipline=args.discipline)

        for panel in panels:
            pending = [r for r in panel["races"]
                       if r.get("race_id") is not None
                       and str(r["race_id"]) not in done_races
                       and r["race_id"] not in seen_races]
            if not pending:
                continue
            # The rules are pure text, so the platform lookup is only needed to fill
            # Unknowns or to report platform_gender under --extended.
            genders = ({} if not (args.gender_fallback or args.extended) else
                       panel_genders(client, eid, disc_slug, panel["race_date"]))
            info_id = f"{eid}/{disc_slug}/{panel['race_date']}"
            for race in pending:
                rid = race["race_id"]
                seen_races.add(rid)
                title = race.get("title", "")
                platform = PLATFORM_GENDER_LABELS.get(genders.get(rid, "M"), "")
                gender = category_gender(title)
                if args.gender_fallback and gender == GENDER_DEFAULT and platform:
                    gender = platform
                tasks.append((
                    {
                        "race_id": rid,
                        "race_category": title,
                        "race_category_class": category_class(title),
                        "race_category_gender": gender,
                        "platform_gender": platform,
                        "race_info_id": info_id,
                        "race_url": BASE + race.get("url", ""),
                        "race_date": panel["race_date"],
                    },
                    dict(event_ctx_base, event_url=panel["url"]),
                ))
        bar.update(1)
    bar.close()

    print(f"{len(tasks)} races to fetch"
          + (f" ({len(done_races)} skipped by --resume)" if done_races else ""))

    if args.dry_run:
        by_event = {}
        for rc, ec in tasks:
            by_event.setdefault(ec["event_id"], [ec["event_name"], 0])[1] += 1
        for eid, (name, cnt) in by_event.items():
            print(f"  {eid:>6}  {cnt:>3} races  {name}")
        print(f"\nDry run: {len(by_event)} events, {len(tasks)} races, "
              f"{client.request_count} requests made.")
        return 0

    # ---- Stage 3: fetch results ----------------------------------------
    mode = "a" if (args.resume and os.path.exists(args.output)) else "w"
    total_rows = 0
    failures = []
    riders = {}  # licence -> set of category classes raced, for --athletes-csv

    race_writer = None
    race_file = None
    if args.events_csv:
        rmode = "a" if (args.resume and os.path.exists(args.events_csv)) else "w"
        race_file = open(args.events_csv, rmode, newline="", encoding="utf-8")
        race_writer = csv.DictWriter(race_file, fieldnames=[
            "event_id", "event_name", "event_location", "event_date_start",
            "race_date", "race_id", "race_category",
            "race_field_size", "race_finishers", "race_dnf_dns", "race_url",
        ])
        if rmode == "w":
            race_writer.writeheader()

    fieldnames = FIELDNAMES + (EXTENDED_FIELDNAMES if args.extended else [])
    with open(args.output, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()

        bar = tqdm(total=len(tasks), desc="races", unit="race")

        def fetch(task):
            race_ctx, event_ctx = task
            return task, race_results(client, race_ctx["race_id"])

        if args.workers > 1:
            pool = ThreadPoolExecutor(max_workers=args.workers)
            stream = pool.map(_safe(fetch, failures), tasks)
        else:
            pool = None
            stream = (_safe(fetch, failures)(t) for t in tasks)

        for item in stream:
            if item is None:
                bar.update(1)
                continue
            (race_ctx, event_ctx), results = item
            size = len(results)
            cls = race_ctx["race_category_class"]
            rdate = race_ctx["race_date"] or event_ctx["event_date_start"]
            for r in results:
                writer.writerow(build_row(r, race_ctx, event_ctx, size, scraped_at))
                note_rider(riders, flat(r.get("licence")), cls, rdate,
                           name=flat(r.get("athlete_name")))
            total_rows += size
            if race_writer:
                # race_field_size counts everyone on the sheet; DNF/DNS appear in the
                # place column as text, so split them out for field-size analysis.
                finishers = sum(1 for r in results if flat(r.get("place")).isdigit())
                race_writer.writerow({
                    "event_id": event_ctx["event_id"],
                    "event_name": event_ctx["event_name"],
                    "event_location": event_ctx["event_location"],
                    "event_date_start": event_ctx["event_date_start"],
                    "race_date": race_ctx["race_date"],
                    "race_id": race_ctx["race_id"],
                    "race_category": race_ctx["race_category"],
                    "race_field_size": size,
                    "race_finishers": finishers,
                    "race_dnf_dns": size - finishers,
                    "race_url": race_ctx["race_url"],
                })
            bar.update(1)
            bar.set_postfix_str(f"{total_rows} rows")
            f.flush()

        bar.close()
        if pool:
            pool.shutdown()

    if race_file:
        race_file.close()

    if args.athletes_csv:
        write_athlete_upgrades(client, riders, args.athletes_csv,
                               args.athletes_discipline, scraped_at,
                               workers=args.workers, resume=args.resume,
                               since=athlete_cutoff,
                               min_points=args.athletes_min_points,
                               keep_no_threshold=args.athletes_keep_no_threshold,
                               disc_slug=disc_slug,
                               plain_names=args.athletes_plain_names,
                               scope_states=[s for s in states if s],
                               window_months=args.athletes_window_months,
                               log_since=log_cutoff,
                               statuses=(tuple(STATUS_ORDER)
                                         if args.athletes_include_no
                                         else REPORTED_STATUSES))
        if args.join_athletes:
            join_athlete_columns(args.output, args.athletes_csv, args.join_athletes)

    print(f"\nDone. {total_rows} result rows from {len(tasks) - len(failures)} races "
          f"-> {args.output}")
    print(f"{client.request_count} HTTP requests.")
    if failures:
        print(f"{len(failures)} races failed:", file=sys.stderr)
        for rid, err in failures[:20]:
            print(f"  race {rid}: {err}", file=sys.stderr)
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more", file=sys.stderr)
        return 1
    return 0


def _safe(fn, failures):
    """Wrap a fetch so one bad race doesn't kill the run; records the failure."""
    def wrapped(task):
        try:
            return fn(task)
        except (requests.RequestException, RuntimeError, ValueError) as e:
            failures.append((task[0]["race_id"], str(e)))
            print(f"[ERROR] race {task[0]['race_id']}: {e}", file=sys.stderr)
            return None
    return wrapped


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Partial output retained; rerun with --resume to continue.",
              file=sys.stderr)
        sys.exit(130)
