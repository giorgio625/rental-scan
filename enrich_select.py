#!/usr/bin/env python3
"""
enrich_select.py — deterministic selection of listings worth fetching.

Implements section 4b of criteria.md. Stdlib only, no dependencies.

The `enricher` agent calls this instead of choosing candidates itself.
Picking N records out of a hundred by a multi-part rule is a set operation,
and the same reasoning that keeps `dedupe.py` deterministic applies here:
an LLM ranking a hundred listings drifts, and the drift is invisible because
any ordering looks plausible. The agent's judgment belongs on the page it
fetches, not on which pages to fetch.

Usage
-----
    python enrich_select.py --classified raw/classified-2026-08-20.json
    python enrich_select.py --classified ... --limit 20
    python enrich_select.py --classified ... --explain

Input  : today's classified JSON, ledger.json (for cached lat/lng),
         enrichment.json (the cache; absent on first run)
Output : JSON object on stdout — a ranked, budget-capped candidate list
Side effect: none. This script never writes anything.
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import dedupe  # RENT_CEILING, so the two files cannot drift apart

# --------------------------------------------------------------------------
# Configuration — mirror any change here back into criteria.md 4b
# --------------------------------------------------------------------------

DEFAULT_BUDGET = 20          # pages fetched per run; see 4b on sizing
REPORT_THRESHOLD = 50        # 4: report anything scoring 50+
REFETCH_FAILED_AFTER_DAYS = 14
BEDS_IN_SCOPE = (1, 2)       # 3: studio and 3BR+ are hard rejects

# 4a's five hard-loft criteria, as groups rather than as signal names.
# "Exposed brick OR exposed timber structure" is ONE criterion, so a listing
# with both still counts one toward the 2-of-5 bar. Counting signal strings
# instead would quietly inflate a listing over the threshold.
HARD_CRITERIA = (
    ("former_industrial",),
    ("exposed_brick", "timber_beams"),
    ("exposed_ductwork",),
    ("high_ceilings",),
    ("post_and_beam",),
)
HARD_LOFT_BAR = 2            # of 5


def hard_criteria_met(signals):
    """How many of 4a's five hard-loft criteria a signal list satisfies."""
    present = set(signals or ())
    return sum(1 for group in HARD_CRITERIA if present & set(group))

# Hosts the enricher will not fetch. Empty by design, decided 2026-08-20.
#
# This briefly held ("zillow.com", "redfin.com"), generalised from a line in
# the sibling condo project about its provider-adapter ingest layer. It cost
# 10 of 23 eligible candidates in one day, including all three Wicker Park
# listings -- among them 2048-w-evergreen-1, which already had exposed brick
# and 12 ft ceilings on record and needed exactly one more signal to clear
# 4a. Blocking the page that could supply it, in the target neighbourhood,
# is not caution; it is the search failing at its own purpose.
#
# The standing instruction is to let every listing through for personal
# review. `web-scout` already fetches listing pages routinely, so nothing
# here is a new kind of access. Keep the mechanism -- a host that starts
# refusing automated reads, or that should be left alone, goes in this
# tuple and the selector stops offering it.
EXCLUDED_HOSTS = ()

# --------------------------------------------------------------------------
# Chicago grid -> lat/lng
#
# The grid is anchored at State & Madison, 800 address units to the mile.
# Expressing the section 2 boundaries as address numbers rather than raw
# decimals keeps this readable against criteria.md: `lat_north(1800)` is
# Bloomingdale, and stays Bloomingdale when someone re-reads it in six months.
# --------------------------------------------------------------------------

_MADISON_LAT = 41.8819
_STATE_LNG = -87.6278
_LAT_PER_800 = 0.0145        # one mile of latitude
_LNG_PER_800 = 0.0195        # one mile of longitude at Chicago's latitude


def lat_north(address_number):
    return _MADISON_LAT + (address_number / 800.0) * _LAT_PER_800


def lng_west(address_number):
    return _STATE_LNG - (address_number / 800.0) * _LNG_PER_800


# Section 2 zones. `primary` is the target; everything else takes the -5.
ZONES = {
    "wicker_park": {
        "primary": True,
        "south": lat_north(1200),   # Division
        "north": lat_north(1800),   # Bloomingdale
        "east":  lng_west(1600),    # Ashland
        "west":  lng_west(2400),    # Western
    },
    "bucktown": {
        "primary": False,
        "south": lat_north(1800),   # Bloomingdale
        "north": lat_north(2400),   # Fullerton
        "east":  lng_west(1600),
        "west":  lng_west(2400),
    },
    "east_village": {
        "primary": False,
        "south": lat_north(800),    # Chicago Ave
        "north": lat_north(1200),   # Division
        "east":  lng_west(1600),
        "west":  lng_west(2400),
    },
    "lincoln_park": {
        "primary": False,
        "south": lat_north(1600),   # North Ave
        "north": lat_north(2800),   # Diversey
        "east":  -87.6100,          # the lake
        "west":  lng_west(1600),    # Clybourn corridor, boxed generously
    },
}

# The 606 / Bloomingdale Trail runs along 1800 N. Three blocks either way.
TRAIL_LAT = lat_north(1800)
TRAIL_BLOCKS_LAT = lat_north(300) - lat_north(0)

# Half a block of slack on every boundary. A geocode lands mid-building, and
# a building fronting the boundary street itself geocodes a hair outside the
# line -- Division St addresses sit ~0.0003 south of the computed 1200 N
# line. criteria.md already treats those as in bounds in practice: the
# reports repeatedly place addresses like 1420 N Western and 1449 N Ashland
# inside the boundary they sit on. Without this, the zone test would be
# stricter than the spec it implements, and would silently drop exactly the
# Wicker Park edge stock this search most wants.
BOUNDARY_TOLERANCE_LAT = lat_north(50) - lat_north(0)
BOUNDARY_TOLERANCE_LNG = lng_west(0) - lng_west(50)

ARTERIALS = ("milwaukee", "damen", "north", "division", "ashland", "western")


def zone_of(lat, lng):
    """Which section 2 zone a coordinate falls in, or None if out of bounds.

    This is a budget pre-filter, NOT the authoritative section 2 test. The
    reporter still applies section 2 in full. The point here is to avoid
    spending a fetch on something already out of bounds -- and to do it from
    the geocoder's cached coordinates rather than `neighborhood_claimed`,
    which listings get wrong constantly.
    """
    if lat is None or lng is None:
        return "unknown"
    tlat, tlng = BOUNDARY_TOLERANCE_LAT, BOUNDARY_TOLERANCE_LNG
    for name, box in ZONES.items():
        if (box["south"] - tlat <= lat <= box["north"] + tlat
                and box["west"] - tlng <= lng <= box["east"] + tlng):
            return name
    return None


# --------------------------------------------------------------------------
# Section 4 scoring ceiling
# --------------------------------------------------------------------------

_LAYOUT_POINTS = {"duplex_up": 18, "two_story": 15, "duplex_down": 8,
                  "single_floor": 0}
_PARKING_POINTS = {"included_garage": 14, "paid_garage": 9, "paid_lot": 9,
                   "street": 0}
_OUTDOOR_POINTS = {"private_roof": 12, "private_terrace": 10, "balcony": 7,
                   "shared": 0, "none": 0}
_LAUNDRY_POINTS = {"in_unit": 8, "in_building": 3, "none": 0}

_MAX = {"loft": 20, "layout": 18, "parking": 14, "outdoor": 12, "laundry": 8}


def _best(value, table, unknown_max):
    """Points if the field is known; the field's ceiling if it is not.

    An unknown field is upside, and that is the whole reason to fetch: a null
    `layout` might be a duplex worth 18. Scoring unknowns at their maximum is
    what makes this a ceiling rather than an estimate.
    """
    if value is None:
        return unknown_max
    return table.get(value, 0)


def score_ceiling(listing, enriched, lat, lng, zone):
    """The highest section 4 score this listing could still reach.

    Known fields contribute what they actually earn. Unknown fields
    contribute their maximum. Fields that no amount of fetching can resolve
    -- transit, value vs comps, condition -- contribute their maximum for
    everyone, so they shift every candidate equally and never affect order.
    """
    def field(name):
        if enriched and enriched.get(name) is not None:
            return enriched.get(name)
        return listing.get(name)

    total = 0
    breakdown = {}

    breakdown["loft"] = _MAX["loft"]
    total += _MAX["loft"]

    for name, key, table in (
        ("layout", "layout", _LAYOUT_POINTS),
        ("parking", "parking_type", _PARKING_POINTS),
        ("outdoor", "outdoor_space", _OUTDOOR_POINTS),
        ("laundry", "laundry", _LAUNDRY_POINTS),
    ):
        pts = _best(field(key), table, _MAX[name])
        breakdown[name] = pts
        total += pts

    # Not resolvable by fetching a page -- constant for every candidate.
    breakdown["transit"] = 10
    breakdown["value_vs_comps"] = 10
    breakdown["condition"] = 5
    total += 25

    if lat is None:
        breakdown["the_606"] = 3
    elif abs(lat - TRAIL_LAT) <= TRAIL_BLOCKS_LAT:
        breakdown["the_606"] = 3
    else:
        breakdown["the_606"] = 0
    total += breakdown["the_606"]

    # Penalties that are already known. Unknown penalties are not assumed,
    # which keeps this an honest ceiling.
    if field("unit_level") == "garden":
        breakdown["garden_penalty"] = -10
        total -= 10
    street = (listing.get("street_name") or "").lower()
    if field("unit_level") == "ground" and any(a in street for a in ARTERIALS):
        breakdown["arterial_penalty"] = -5
        total -= 5
    if zone != "unknown" and not ZONES.get(zone, {}).get("primary", False):
        breakdown["secondary_zone_penalty"] = -5
        total -= 5

    return total, breakdown


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------

def _parse_day(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def eligibility(listing, enriched, today):
    """Return None if the listing should be fetched, else the reason not to."""
    url = listing.get("url")
    if not url:
        return "no url"

    host = (urlparse(url).netloc or "").lower()
    if any(h in host for h in EXCLUDED_HOSTS):
        return "excluded host (" + host + ")"

    beds = listing.get("beds")
    if beds not in BEDS_IN_SCOPE:
        return "beds out of scope (" + str(beds) + ")"

    cost = listing.get("all_in_monthly")
    if cost is None:
        return "no all-in rent"
    if cost > dedupe.RENT_CEILING:
        return "over rent ceiling (" + format(cost, ",.0f") + ")"

    if enriched:
        status = enriched.get("fetch_status")
        if status == "ok":
            return "already enriched"
        fetched = _parse_day(enriched.get("fetched_at"))
        if fetched and (today - fetched).days < REFETCH_FAILED_AFTER_DAYS:
            age = (today - fetched).days
            return "fetch failed " + str(age) + "d ago, retry at " + \
                   str(REFETCH_FAILED_AFTER_DAYS) + "d"

    # Only skip when 4a is already SETTLED, never merely because some
    # evidence exists. A listing sitting one short of the bar is the single
    # most valuable thing this stage can fetch: one more signal decides it
    # either way. The earlier rule here skipped anything with any signal at
    # all, and so excluded 2048-w-evergreen-1 -- exposed brick, 12 ft
    # ceilings, then one signal short, in Wicker Park -- precisely backwards.
    met = hard_criteria_met(listing.get("loft_signals"))
    if met >= HARD_LOFT_BAR:
        return "4a already satisfied (" + str(met) + " of 5 hard criteria)"

    return None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def select(classified, ledger, enrichment, today, budget):
    entries = enrichment.get("entries", {})
    records = ledger.get("listings", {})

    pool = []
    for bucket in ("new", "price_change", "relist"):
        pool.extend(classified.get(bucket, []))

    candidates, skipped = [], []
    for listing in pool:
        key = listing.get("_key")
        if not key:
            continue
        enriched = entries.get(key)
        reason = eligibility(listing, enriched, today)
        if reason:
            skipped.append({"key": key, "reason": reason})
            continue

        record = records.get(key, {})
        lat, lng = record.get("lat"), record.get("lng")
        zone = zone_of(lat, lng)
        if zone is None:
            skipped.append({"key": key,
                            "reason": "out of bounds by coordinates"})
            continue

        ceiling, breakdown = score_ceiling(listing, enriched, lat, lng, zone)
        if ceiling < REPORT_THRESHOLD:
            skipped.append({
                "key": key,
                "reason": "cannot reach " + str(REPORT_THRESHOLD) +
                          " even at best case (" + str(ceiling) + ")",
            })
            continue

        candidates.append({
            "key": key,
            "url": listing.get("url"),
            "address_raw": listing.get("address_raw"),
            "unit": listing.get("unit"),
            "beds": listing.get("beds"),
            "rent_gross": listing.get("rent_gross"),
            "all_in_monthly": listing.get("all_in_monthly"),
            "zone": zone,
            "score_ceiling": ceiling,
            "score_ceiling_breakdown": breakdown,
            "unknown_fields": [
                f for f in ("layout", "parking_type", "outdoor_space",
                            "laundry", "ceiling_height_ft", "heat_included")
                if listing.get(f) is None
            ],
        })

    # Highest ceiling first; primary zone breaks ties; then cheaper first.
    candidates.sort(key=lambda c: (
        -c["score_ceiling"],
        not ZONES.get(c["zone"], {}).get("primary", False),
        c["all_in_monthly"] or 0,
    ))

    selected = candidates[:budget]
    return {
        "run_date": today.isoformat(),
        "budget": budget,
        "counts": {
            "pool": len(pool),
            "eligible": len(candidates),
            "selected": len(selected),
            "deferred_over_budget": max(0, len(candidates) - budget),
            "skipped": len(skipped),
        },
        "selected": selected,
        "deferred": candidates[budget:],
        "skipped": skipped,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classified", required=True)
    parser.add_argument("--ledger", default="ledger.json")
    parser.add_argument("--enrichment", default="enrichment.json")
    parser.add_argument("--limit", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--explain", action="store_true",
                        help="include the skip reasons in stdout")
    args = parser.parse_args()

    with open(args.classified) as fh:
        classified = json.load(fh)

    ledger_path = Path(args.ledger)
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}

    enrich_path = Path(args.enrichment)
    enrichment = json.loads(enrich_path.read_text()) if enrich_path.exists() else {}

    result = select(classified, ledger, enrichment, date.today(), args.limit)
    if not args.explain:
        result.pop("skipped", None)

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
