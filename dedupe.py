#!/usr/bin/env python3
"""
dedupe.py — deterministic listing matching and state management
for the Wicker Park rental scan.

Implements sections 6 and 7 of criteria.md. Stdlib only, no dependencies.

The dedupe-analyst agent calls this instead of reasoning about set
differences itself. LLMs drift on set operations across hundreds of
records; this does not.

Usage
-----
    python3 dedupe.py --listings today.json --ledger ledger.json
    python3 dedupe.py --listings today.json --ledger ledger.json --dry-run
    cat today.json | python3 dedupe.py --ledger ledger.json

Input  : JSON array of listing objects matching the criteria.md schema
Output : JSON object on stdout with classified buckets
Side effect: ledger.json is updated in place unless --dry-run
"""

import argparse
import json
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration — mirror any change here back into criteria.md
# --------------------------------------------------------------------------

DAYS_ABSENT_UNTIL_DEAD = 7
RENT_CEILING = 3500
RENT_ROUNDING = 50           # fallback key buckets rent to nearest $50
DUPLICATE_RENT_WINDOW = 50   # same building + within $50 => possible duplicate
SUSPECT_PRICING_THRESHOLD = 0.80   # >20% below median for its bed count
MIN_COMPS_FOR_MEDIAN = 5

# Estimated tenant-paid heat, used only when heat_included is explicitly False.
HEAT_MONTHLY_STANDARD = 160
HEAT_MONTHLY_LOFT = 210      # high ceilings + single-pane factory glazing

AMORTIZE_MONTHS = 12         # one-time fees spread across the lease

# Chicago's grid runs off State & Madison. Directionals are load-bearing:
# 1600 N Damen and 1600 S Damen are four miles apart. Normalize the form,
# never strip the token.
DIRECTIONALS = {
    "n": "n", "north": "n",
    "s": "s", "south": "s",
    "e": "e", "east": "e",
    "w": "w", "west": "w",
}

STREET_SUFFIXES = {
    "st", "street", "ave", "av", "avenue", "blvd", "boulevard",
    "dr", "drive", "pl", "place", "ct", "court", "pkwy", "parkway",
    "ter", "terrace", "rd", "road", "ln", "lane", "way", "sq", "square",
}

UNIT_PREFIXES = {
    "unit", "units", "apt", "apartment", "ste", "suite", "no", "num", "#",
}

ADDRESS_RE = re.compile(
    r"^\s*(?P<number>\d+)\s+"
    r"(?:(?P<directional>[NSEW]\.?|North|South|East|West)\s+)?"
    r"(?P<street>[^,#]+?)"
    r"(?:\s*[,#]\s*(?:unit|apt|apartment|ste|suite|no\.?)?\s*(?P<unit>[\w\-]+))?"
    r"\s*$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def _strip_punct(text):
    return re.sub(r"[^\w\s]", " ", text)


def normalize_directional(value):
    """North / N. / n -> 'n'. Unknown or missing -> ''."""
    if not value:
        return ""
    token = _strip_punct(str(value)).strip().lower()
    return DIRECTIONALS.get(token, "")


def normalize_street_name(value):
    """'Damen Ave' -> 'damen'. 'Saint Paul Blvd' -> 'saint-paul'."""
    if not value:
        return ""
    tokens = _strip_punct(str(value)).lower().split()
    # Drop a trailing suffix only. 'Court Place' should keep 'court'.
    while tokens and tokens[-1] in STREET_SUFFIXES:
        tokens.pop()
    # A leading directional sometimes rides along in street_name.
    if tokens and tokens[0] in DIRECTIONALS:
        tokens.pop(0)
    return "-".join(tokens)


def normalize_unit(value):
    """'Unit 2R' -> '2R'. '#3-N' -> '3N'. None -> None."""
    if value is None:
        return None
    token = str(value).strip().lower()
    if not token:
        return None
    for prefix in sorted(UNIT_PREFIXES, key=len, reverse=True):
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    token = re.sub(r"[^\w]", "", token)
    return token.upper() or None


def parse_address_raw(raw):
    """Fallback when a harvester returns address_raw but no structured parts."""
    if not raw:
        return {}
    match = ADDRESS_RE.match(str(raw))
    if not match:
        return {}
    return {
        "street_number": match.group("number"),
        "street_directional": normalize_directional(match.group("directional")),
        "street_name": match.group("street"),
        "unit": match.group("unit"),
    }


def _resolve_parts(listing):
    """Prefer structured fields; fall back to parsing address_raw."""
    parts = {
        "street_number": listing.get("street_number"),
        "street_directional": listing.get("street_directional"),
        "street_name": listing.get("street_name"),
        "unit": listing.get("unit"),
    }
    if not parts["street_number"] or not parts["street_name"]:
        parsed = parse_address_raw(listing.get("address_raw"))
        for field, value in parsed.items():
            if not parts.get(field):
                parts[field] = value
    return parts


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def building_key(listing):
    """Everything but the unit. Two records sharing this are the same building."""
    parts = _resolve_parts(listing)
    number = str(parts.get("street_number") or "").strip()
    directional = normalize_directional(parts.get("street_directional"))
    street = normalize_street_name(parts.get("street_name"))
    if not number or not street:
        return None
    return "-".join(filter(None, [number, directional, street]))


def canonical_key(listing):
    """
    Primary key when a unit is known:
        {number}-{directional}-{street}-{UNIT}
    Fallback when it is not:
        {number}-{directional}-{street}-{beds}br-{rent bucketed to $50}
    Returns (key, key_type).
    """
    base = building_key(listing)
    if base is None:
        return None, "unresolvable"

    unit = normalize_unit(_resolve_parts(listing).get("unit"))
    if unit:
        return f"{base}-{unit}", "primary"

    beds = listing.get("beds")
    rent = listing.get("rent_gross")
    if beds is None or rent is None:
        return f"{base}-UNKNOWN", "weak"

    bucket = int(round(float(rent) / RENT_ROUNDING) * RENT_ROUNDING)
    return f"{base}-{beds}br-{bucket}", "fallback"


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------

def all_in_rent(listing):
    """
    Base rent + parking + tenant-paid heat + one-time fees amortized over 12.

    Returns (amount, assumptions) where assumptions lists every estimate that
    went into the number, so the reporter can show its work instead of
    presenting a guess as a fact.
    """
    assumptions = []
    rent = listing.get("rent_gross")
    if rent is None:
        return None, ["no base rent published"]

    total = float(rent)

    parking = listing.get("parking_cost")
    if parking:
        total += float(parking)

    heat_included = listing.get("heat_included")
    if heat_included is False:
        est = (HEAT_MONTHLY_LOFT if listing.get("loft_type") == "hard"
               else HEAT_MONTHLY_STANDARD)
        total += est
        assumptions.append(f"tenant-paid heat estimated at ${est}/mo")
    elif heat_included is None:
        assumptions.append("heat inclusion unknown — not priced in")

    for field, label in (("move_in_fee", "move-in fee"),
                         ("broker_fee", "broker fee")):
        value = listing.get(field)
        if value:
            total += float(value) / AMORTIZE_MONTHS
            assumptions.append(f"{label} ${value:,.0f} amortized over 12 mo")

    return round(total, 2), assumptions


def net_effective(listing):
    """Concession-adjusted monthly, when the listing publishes one."""
    net = listing.get("rent_net_effective")
    return float(net) if net is not None else None


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

def load_ledger(path):
    p = Path(path)
    if not p.exists():
        return {"listings": {}, "meta": {"created": date.today().isoformat()}}
    with p.open() as fh:
        data = json.load(fh)
    data.setdefault("listings", {})
    data.setdefault("meta", {})
    return data


def save_ledger(path, ledger):
    ledger["meta"]["last_run"] = date.today().isoformat()
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w") as fh:
        json.dump(ledger, fh, indent=2, sort_keys=True)
    tmp.replace(path)


def _parse_day(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# Comps
# --------------------------------------------------------------------------

def running_medians(ledger):
    """Median gross rent by bed count across all live ledger entries."""
    by_beds = {}
    for record in ledger["listings"].values():
        if record.get("status") == "dead":
            continue
        beds, rent = record.get("beds"), record.get("rent_gross")
        if beds is not None and rent:
            by_beds.setdefault(beds, []).append(float(rent))
    return {
        beds: statistics.median(rents)
        for beds, rents in by_beds.items()
        if len(rents) >= MIN_COMPS_FOR_MEDIAN
    }


def is_suspect_pricing(listing, medians):
    median = medians.get(listing.get("beds"))
    rent = listing.get("rent_gross")
    if median is None or not rent:
        return False
    return float(rent) < median * SUSPECT_PRICING_THRESHOLD


# --------------------------------------------------------------------------
# Duplicate detection within a single run
# --------------------------------------------------------------------------

def group_possible_duplicates(listings):
    """
    Same building, rents within $50, and at least one record missing its unit.

    Deliberately does NOT merge. It hands the ambiguity back for a human
    call, because silently collapsing two real units is worse than showing
    one pair twice.
    """
    by_building = {}
    for listing in listings:
        bkey = building_key(listing)
        if bkey:
            by_building.setdefault(bkey, []).append(listing)

    groups = []
    for bkey, members in by_building.items():
        if len(members) < 2:
            continue
        unresolved = []
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                unit_a = normalize_unit(_resolve_parts(a).get("unit"))
                unit_b = normalize_unit(_resolve_parts(b).get("unit"))
                if unit_a and unit_b:
                    continue  # both known: same unit merges by key; distinct units are different apartments
                rent_a, rent_b = a.get("rent_gross"), b.get("rent_gross")
                if rent_a is None or rent_b is None:
                    continue
                if abs(float(rent_a) - float(rent_b)) <= DUPLICATE_RENT_WINDOW:
                    unresolved.append((a, b))
        if unresolved:
            groups.append({
                "building": bkey,
                "reason": "same building, rents within "
                          f"${DUPLICATE_RENT_WINDOW}, unit missing on at least one record",
                "members": [
                    {
                        "source": m.get("source"),
                        "url": m.get("url"),
                        "address_raw": m.get("address_raw"),
                        "unit": m.get("unit"),
                        "beds": m.get("beds"),
                        "rent_gross": m.get("rent_gross"),
                    }
                    for m in members
                ],
            })
    return groups


# --------------------------------------------------------------------------
# Cross-source collapse
# --------------------------------------------------------------------------

def collapse_by_key(listings):
    """
    One unit listed on four portals becomes one record with four source links.
    Field-level merge keeps the most complete version: a non-null value from
    any source beats a null from another.
    """
    merged = {}
    for listing in listings:
        key, key_type = canonical_key(listing)
        if key is None:
            merged.setdefault("__unresolvable__", []).append(listing)
            continue
        if key not in merged:
            record = dict(listing)
            record["_key"] = key
            record["_key_type"] = key_type
            record["_sources"] = [{"source": listing.get("source"),
                                   "url": listing.get("url")}]
            merged[key] = record
        else:
            record = merged[key]
            for field, value in listing.items():
                if record.get(field) is None and value is not None:
                    record[field] = value
            record["_sources"].append({"source": listing.get("source"),
                                       "url": listing.get("url")})
    unresolvable = merged.pop("__unresolvable__", [])
    return merged, unresolvable


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def classify(merged, ledger, today):
    """Apply the §7 status transition table."""
    buckets = {"new": [], "price_change": [], "relist": [], "seen": []}
    medians = running_medians(ledger)

    for key, listing in merged.items():
        prior = ledger["listings"].get(key)
        rent = listing.get("rent_gross")
        cost, assumptions = all_in_rent(listing)

        listing["all_in_monthly"] = cost
        listing["cost_assumptions"] = assumptions
        listing["net_effective"] = net_effective(listing)
        listing["suspect_pricing"] = is_suspect_pricing(listing, medians)
        listing["over_ceiling_by"] = (
            round(cost - RENT_CEILING, 2) if cost and cost > RENT_CEILING else 0
        )

        if prior is None:
            status = "new"
            history = [{"date": today.isoformat(), "rent": rent}]
            first_seen = today.isoformat()
        else:
            history = prior.get("price_history", [])
            first_seen = prior.get("first_seen", today.isoformat())
            prior_rent = prior.get("rent_gross")

            if prior.get("status") == "dead":
                status = "relist"
                dead_since = _parse_day(prior.get("dead_since"))
                gap = (today - dead_since).days if dead_since else None
                listing["days_dead"] = gap
                if gap is not None and gap < 30:
                    listing["note"] = f"quick relist — only {gap} days off market"
                history.append({"date": today.isoformat(), "rent": rent})
            elif prior_rent is not None and rent is not None \
                    and float(prior_rent) != float(rent):
                status = "price_change"
                listing["prior_rent"] = prior_rent
                listing["rent_delta"] = round(float(rent) - float(prior_rent), 2)
                listing["days_on_market"] = (
                    (today - _parse_day(first_seen)).days
                    if _parse_day(first_seen) else None
                )
                history.append({"date": today.isoformat(), "rent": rent})
            else:
                status = "seen"

        listing["status"] = status
        listing["first_seen"] = first_seen
        buckets[status].append(listing)

        ledger["listings"][key] = {
            "key": key,
            "key_type": listing.get("_key_type"),
            "address_raw": listing.get("address_raw"),
            "unit": listing.get("unit"),
            "beds": listing.get("beds"),
            "rent_gross": rent,
            "all_in_monthly": cost,
            "loft_type": listing.get("loft_type"),
            "layout": listing.get("layout"),
            "first_seen": first_seen,
            "last_seen": today.isoformat(),
            "status": "active",
            "price_history": history,
            "sources": listing.get("_sources", []),
            "verdict": (ledger["listings"].get(key) or {}).get("verdict"),
            "lat": (ledger["listings"].get(key) or {}).get("lat"),
            "lng": (ledger["listings"].get(key) or {}).get("lng"),
        }

    return buckets


def reap_absent(ledger, seen_keys, today):
    """Mark anything absent DAYS_ABSENT_UNTIL_DEAD consecutive days as dead."""
    newly_dead = []
    for key, record in ledger["listings"].items():
        if key in seen_keys or record.get("status") == "dead":
            continue
        last_seen = _parse_day(record.get("last_seen"))
        if last_seen and (today - last_seen).days >= DAYS_ABSENT_UNTIL_DEAD:
            record["status"] = "dead"
            record["dead_since"] = today.isoformat()
            newly_dead.append(key)
    return newly_dead


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run(listings, ledger, today):
    merged, unresolvable = collapse_by_key(listings)
    duplicates = group_possible_duplicates(listings)
    buckets = classify(merged, ledger, today)
    newly_dead = reap_absent(ledger, set(merged.keys()), today)

    last_output = _parse_day(ledger["meta"].get("last_output_date"))
    silent_days = (today - last_output).days if last_output else 0
    has_output = bool(buckets["new"] or buckets["price_change"]
                      or buckets["relist"])
    if has_output:
        ledger["meta"]["last_output_date"] = today.isoformat()

    return {
        "run_date": today.isoformat(),
        "counts": {
            "ingested": len(listings),
            "unique_units": len(merged),
            "new": len(buckets["new"]),
            "price_change": len(buckets["price_change"]),
            "relist": len(buckets["relist"]),
            "seen_suppressed": len(buckets["seen"]),
            "newly_dead": len(newly_dead),
            "possible_duplicates": len(duplicates),
            "unresolvable_addresses": len(unresolvable),
        },
        "new": buckets["new"],
        "price_change": buckets["price_change"],
        "relist": buckets["relist"],
        "possible_duplicates": duplicates,
        "unresolvable": unresolvable,
        "newly_dead": newly_dead,
        # Canary: a silent scan and a broken scan look identical from outside.
        "heartbeat_due": silent_days >= 7 and not has_output,
        "sources_seen": sorted({
            s.get("source") for l in listings for s in [l] if s.get("source")
        }),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listings", help="JSON array of listings; omit for stdin")
    parser.add_argument("--ledger", default="ledger.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify without writing the ledger")
    args = parser.parse_args()

    if args.listings:
        with open(args.listings) as fh:
            listings = json.load(fh)
    else:
        listings = json.load(sys.stdin)

    if not isinstance(listings, list):
        sys.exit("error: input must be a JSON array of listing objects")

    ledger = load_ledger(args.ledger)
    result = run(listings, ledger, date.today())

    if not args.dry_run:
        save_ledger(args.ledger, ledger)

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
