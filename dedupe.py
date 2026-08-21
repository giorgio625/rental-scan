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
RENT_CEILING = 3800
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

# Locality tails. Chicago alert emails append ", Chicago, IL 60622" to roughly
# half of all address strings; the old single-regex parser required the string
# to END at the unit and returned {} for every one of them (48% of the 312
# distinct address_raw values ever harvested). Stripped before parsing rather
# than accommodated inside one expression, because the accommodating version
# is what silently swallowed units into street names.
CITY_TAILS = {"chicago"}
STATE_TAILS = {"il", "illinois"}
ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")

# A named unit — "Garden Unit", "Coach House" — is a unit, not a street.
NAMED_UNITS = {
    "garden", "basement", "coach", "penthouse", "rear", "front",
    "lower", "upper", "english", "gf",
}

# Leading house number. A range address ("2061-2071 N. Southport") keys off
# its low number, which is both the convention the harvesters already follow
# and the only choice that lets a range and a single-number record for the
# same building collapse.
NUMBER_RE = re.compile(r"^(\d+)(?:\s*-\s*\d+)?")


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


def normalize_street_number(value):
    """
    '649-51' -> '649'. A range address keys off its low number.

    Applied on BOTH the structured and the parsed path. Harvesters are
    inconsistent about ranges — the same building arrives as '649-51 N.
    Wolcott' from one source and '649 N Wolcott' from another — so
    normalizing in only one place puts them on two keys.
    """
    if value is None:
        return ""
    match = NUMBER_RE.match(str(value).strip())
    return match.group(1) if match else str(value).strip()


def normalize_street_name(value):
    """'Damen Ave' -> 'damen'. 'Saint Paul Blvd' -> 'saint-paul'."""
    if not value:
        return ""
    tokens = _strip_punct(str(value)).lower().split()
    # Drop a trailing suffix only. 'Court Place' should keep 'court'.
    while tokens and tokens[-1] in STREET_SUFFIXES:
        tokens.pop()
    # A leading directional sometimes rides along in street_name — but only
    # strip it when something follows; "North Avenue" IS the street.
    if len(tokens) > 1 and tokens[0] in DIRECTIONALS:
        tokens.pop(0)
    # A TRAILING directional is part of the name, not a prefix to drop:
    # "Lincoln Park West" is a street. Collapse it to the one-letter form so
    # sources spelling it "Lincoln Park W" land on the same key. Three
    # harvested records for 2140 N Lincoln Park W produced three different
    # street names before this.
    if len(tokens) > 1 and tokens[-1] in DIRECTIONALS:
        tokens[-1] = DIRECTIONALS[tokens[-1]]
    return "-".join(tokens)


def normalize_unit(value):
    """'Unit 2R' -> '2R'. '#3-N' -> '3N'. None -> None."""
    if value is None:
        return None
    token = str(value).strip().lower()
    if not token:
        return None
    # Drop prefix words wherever they sit: "Unit 2R" and "Garden Unit" are
    # both units, and joining the word in gives GARDENUNIT, a key that
    # matches nothing else the same building ever produces.
    kept = [t for t in token.split()
            if _strip_punct(t).strip() not in UNIT_PREFIXES]
    token = "".join(kept) if kept else ""
    token = re.sub(r"[^\w]", "", token)
    return token.upper() or None


def _is_state_or_zip(segment):
    """True for a ', IL' / ', IL 60622' / ', 60622' tail piece."""
    tokens = _strip_punct(segment).lower().split()
    if not tokens:
        return True
    if all(ZIP_RE.match(t) for t in tokens):
        return True
    if tokens[0] in STATE_TAILS and all(ZIP_RE.match(t) for t in tokens[1:]):
        return True
    return False


def _looks_like_city(segment):
    """
    A bare place name — no digits, no unit vocabulary.

    Only ever consulted after a state or zip has already been stripped, so
    ", Unit G" cannot be mistaken for a city: nothing pops it unless a state
    segment followed it. The unit-vocabulary guard is belt-and-braces for
    ", Unit G, IL", which no source has produced but which would otherwise
    lose the unit silently.
    """
    tokens = _strip_punct(segment).lower().split()
    if not tokens or any(any(ch.isdigit() for ch in t) for t in tokens):
        return False
    if any(t in UNIT_PREFIXES or t in NAMED_UNITS for t in tokens):
        return False
    return True


def _strip_locality(raw):
    """
    Drop trailing city/state/zip segments. Returns the surviving segments.

    The city is identified by position rather than by name: whatever bare
    place name sits immediately before a state or zip is the city. An earlier
    version matched a hardcoded {"chicago"} instead, and read the city of
    "299 Pembridge Ln #D2, Schaumburg, IL 60193" as the unit — the silent-
    wrong-parse failure this function exists to avoid.
    """
    segments = [s.strip() for s in str(raw).split(",")]
    segments = [s for s in segments if s]

    stripped_state = False
    while len(segments) > 1 and _is_state_or_zip(segments[-1]):
        segments.pop()
        stripped_state = True
    if stripped_state and len(segments) > 1 and _looks_like_city(segments[-1]):
        segments.pop()

    # A no-comma tail: "1234 North Ashland Avenue Chicago IL 60622". Only the
    # known city names are safe to strip here — without a comma there is no
    # boundary telling a city from the last word of a street name.
    if len(segments) == 1:
        tokens = segments[0].split()
        while len(tokens) > 2 and (
            _is_state_or_zip(tokens[-1])
            or _strip_punct(tokens[-1]).strip().lower() in CITY_TAILS
        ):
            tokens.pop()
        segments = [" ".join(tokens)]
    return segments


def _split_unit(tokens):
    """
    Pull a unit off a token list. Returns (unit, remaining_tokens).

    Three shapes appear in the real data, in this precedence:
      1. an explicit prefix   — "Unit 3W", "APT 2F", "#615", "Ste 400"
      2. a named unit         — "Garden Unit", "Coach House"
      3. a bare trailing token directly after a street suffix —
         "Ashland Avenue 502", "Honore Street G", "Wood Street 1N"

    Shape 3 is why this is a scan and not a regex: the same trailing token is
    a unit after "Avenue" and part of the street name after "N". "1451 N
    Ashland Avenue S" is unit S, not a second directional.
    """
    if not tokens:
        return None, tokens

    lowered = [_strip_punct(t).strip().lower() for t in tokens]

    # 1. explicit prefix, anywhere after the house number
    for i in range(1, len(tokens)):
        bare = lowered[i]
        if bare in UNIT_PREFIXES and i + 1 < len(tokens):
            return " ".join(tokens[i + 1:]), tokens[:i]
        if tokens[i].startswith("#") and len(tokens[i]) > 1:
            return " ".join(tokens[i:]), tokens[:i]

    # 2. named unit — "Garden Unit" reads backwards from shape 1
    for i in range(1, len(tokens)):
        if lowered[i] in NAMED_UNITS:
            return " ".join(tokens[i:]), tokens[:i]

    # 3. bare trailing token immediately after a street suffix
    if len(tokens) >= 3 and lowered[-2] in STREET_SUFFIXES:
        tail = lowered[-1]
        if tail not in STREET_SUFFIXES and re.match(r"^[a-z]?\d+[a-z]?$|^[a-z]$", tail):
            return tokens[-1], tokens[:-1]

    return None, tokens


def parse_address_raw(raw):
    """
    Fallback when a harvester returns address_raw but no structured parts.

    Roughly one raw record in six arrives without structured address fields,
    so this path is load-bearing rather than defensive. Returns {} when there
    is no house number to anchor on — dedupe.py routes those to
    `unresolvable` for a human, which is the honest outcome. It must never
    return a *plausible but wrong* parse: a unit swallowed into the street
    name mints a second key for a building already in the ledger, and nothing
    downstream can see that it happened.
    """
    if not raw:
        return {}

    segments = _strip_locality(raw)
    if not segments:
        return {}

    head = segments[0]
    number_match = NUMBER_RE.match(head.strip())
    if not number_match:
        return {}
    number = number_match.group(1)

    rest = head.strip()[number_match.end():].strip()
    tokens = rest.split()

    # A unit may live in a later segment ("... Ave, Unit 2") or ride along in
    # the head with no separator at all ("... Ave Unit 3W").
    unit = None
    trailing = [s for s in segments[1:] if s]
    if trailing:
        candidate = " ".join(trailing)
        cand_tokens = candidate.split()
        # ["Unit", "2"] has no house number, so _split_unit's index-1 scan
        # would miss a leading prefix. Handle the whole-segment case here.
        bare = _strip_punct(cand_tokens[0]).strip().lower() if cand_tokens else ""
        if bare in UNIT_PREFIXES and len(cand_tokens) > 1:
            unit = " ".join(cand_tokens[1:])
        elif candidate.startswith("#"):
            unit = candidate
        elif bare in NAMED_UNITS or len(cand_tokens) == 1:
            unit = candidate
        else:
            unit, _ = _split_unit(["_"] + cand_tokens)

    if unit is None:
        unit, tokens = _split_unit(["_"] + tokens)
        tokens = tokens[1:]

    directional = ""
    if len(tokens) > 1:
        candidate = normalize_directional(tokens[0])
        if candidate:
            directional = candidate
            tokens = tokens[1:]

    street = " ".join(tokens).strip()
    if not street:
        return {}

    return {
        "street_number": number,
        "street_directional": directional,
        "street_name": street,
        "unit": unit,
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

    # A structured street_name is not automatically clean. On 2026-08-21 a
    # harvester wrote street_name "Fullerton Ave APT 3" with unit null, and
    # because both structured fields were present the parser above was never
    # consulted — the unit went straight into the key as
    # 916-w-fullerton-ave-apt-3-2br-3000, a fallback key for a listing whose
    # unit was right there in the string. Recover it rather than trusting the
    # field, and only when the unit is otherwise missing: a harvester that
    # filled both fields is believed.
    if parts["street_name"] and not parts["unit"]:
        recovered, remaining = _split_unit(["_"] + str(parts["street_name"]).split())
        if recovered and len(remaining) > 1:
            parts["unit"] = recovered
            parts["street_name"] = " ".join(remaining[1:])
    return parts


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def building_key(listing):
    """Everything but the unit. Two records sharing this are the same building."""
    parts = _resolve_parts(listing)
    number = normalize_street_number(parts.get("street_number"))
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

    return f"{base}-{beds}br-{_fallback_bucket(rent)}", "fallback"


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


# Section 2 geography lives here rather than in enrich_select.py because
# `running_medians` needs it and enrich_select already imports this module.
# One definition, imported by everything that tests a coordinate.

# --------------------------------------------------------------------------
# Comps
# --------------------------------------------------------------------------

def running_medians(ledger):
    """
    Median gross rent by bed count across live, in-bounds ledger entries.

    Scoped to section 2 geography. The ledger holds every record the
    harvesters ever returned, filtering being downstream's job, so 46 of the
    live records sit in the South Loop, Hyde Park, and Humboldt Park. Letting
    them set the comp moved the 2-bedroom median $170 and the 1-bedroom $102
    — and that median is the denominator of section 4's value score and the
    `suspect_pricing` trigger, so out-of-area rents were quietly deciding
    whether a Wicker Park listing looked like a deal.

    A record with no cached coordinates still counts. `zone_of` returns
    "unknown" for those, and the geocoder runs after this, so every listing
    on its first pass is uncoordinated — excluding them would drop each new
    record from the comps it is being judged against. Only records that are
    provably out of bounds are dropped.
    """
    by_beds = {}
    for record in ledger["listings"].values():
        if record.get("status") == "dead":
            continue
        if zone_of(record.get("lat"), record.get("lng")) is None:
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

def _fallback_bucket(rent):
    return int(round(float(rent) / RENT_ROUNDING) * RENT_ROUNDING)


def _stabilize_fallback_keys(assigned, ledger):
    """
    Re-point a fallback key at the live ledger entry one bucket away.

    A fallback key embeds the rent, so a unit with no published unit number
    changes identity the moment its rent crosses a $50 boundary: $2,974 keys
    to ...-2950 and $2,975 to ...-3000. The old key then goes absent, dies
    seven days later, and the same apartment reappears as `new` with a fresh
    first_seen and an empty price_history — the price change, the days on
    market, and any human verdict all lost, with nothing anywhere reporting
    that it happened. Four of the 32 live fallback keys sit within $10 of a
    boundary right now.

    Claims are strictly one-to-one. Two unit-less listings in the same
    building whose rents straddle a boundary must NOT both collapse onto one
    ledger entry, so each ledger key is claimed at most once, by the listing
    whose rent is closest to the stored rent, ties broken on the key string
    so the result never depends on input order.
    """
    live = {key: record
            for key, record in ledger.get("listings", {}).items()
            if record.get("status") != "dead"}
    if not live:
        return

    taken = {entry["key"] for entry in assigned if entry["key"] in live}

    proposals = []
    for entry in assigned:
        if entry["key_type"] != "fallback" or entry["key"] in live:
            continue
        listing = entry["listing"]
        base = building_key(listing)
        rent = listing.get("rent_gross")
        if not base or rent is None:
            continue
        rent = float(rent)
        bucket = _fallback_bucket(rent)
        for offset in (-RENT_ROUNDING, RENT_ROUNDING):
            candidate = f"{base}-{listing.get('beds')}br-{bucket + offset}"
            record = live.get(candidate)
            if record is None:
                continue
            prior = record.get("rent_gross")
            if prior is None:
                continue
            distance = abs(float(prior) - rent)
            # A bucket-boundary crossing moves the rent by less than one
            # bucket. Anything further apart is a different apartment.
            if distance > RENT_ROUNDING:
                continue
            proposals.append((distance, candidate, entry))

    proposals.sort(key=lambda p: (p[0], p[1]))
    claimed_entries = set()
    for _, candidate, entry in proposals:
        if candidate in taken or id(entry["listing"]) in claimed_entries:
            continue
        entry["key"] = candidate
        entry["key_stabilized"] = True
        taken.add(candidate)
        claimed_entries.add(id(entry["listing"]))


def collapse_by_key(listings, ledger=None):
    """
    One unit listed on four portals becomes one record with four source links.
    Field-level merge keeps the most complete version: a non-null value from
    any source beats a null from another.
    """
    assigned = []
    unresolvable_early = []
    for listing in listings:
        key, key_type = canonical_key(listing)
        if key is None:
            unresolvable_early.append(listing)
            continue
        assigned.append({"listing": listing, "key": key, "key_type": key_type})

    if ledger:
        _stabilize_fallback_keys(assigned, ledger)

    merged = {}
    for entry in assigned:
        listing, key, key_type = entry["listing"], entry["key"], entry["key_type"]
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
        if entry.get("key_stabilized"):
            merged[key]["_key_stabilized"] = True
    return merged, unresolvable_early


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

        ledger["listings"][key] = _ledger_record(
            key, listing, prior, rent, cost, history, first_seen, today
        )

    return buckets


# Rewritten from today's observation every run.
CURRENT_STATE_FIELDS = ("rent_gross", "all_in_monthly", "net_effective",
                        "suspect_pricing", "key_type")

# Descriptive fields. A non-null observation wins; a null one leaves the
# stored value alone. Harvesters emit null for "not stated", never for
# "known absent" (section 6: absence of evidence is not evidence of absence),
# so overwriting a known sqft with today's null would be reading a silent
# email as a correction. Kept here rather than in raw/ because the ledger is
# the file that has to be able to rebuild shortlist.json on its own.
STICKY_FIELDS = ("address_raw", "unit", "beds", "baths", "sqft", "url",
                 "neighborhood_claimed", "loft_type", "loft_signals",
                 "ceiling_height_ft", "layout", "outdoor_space",
                 "outdoor_space_sqft", "laundry", "parking_type",
                 "parking_cost", "heat_included", "available_date",
                 "lease_term_months", "unit_level", "concession")

# Never written here. `verdict` is the human's ruling and `lat`/`lng` are the
# geocoder's cache; both outlive any single harvest.
PRESERVED_FIELDS = ("verdict", "lat", "lng")


def _ledger_record(key, listing, prior, rent, cost, history, first_seen, today):
    prior = prior or {}
    record = {
        "key": key,
        "first_seen": first_seen,
        "last_seen": today.isoformat(),
        "status": "active",
        "price_history": history,
        "sources": listing.get("_sources", []),
        "rent_gross": rent,
        "all_in_monthly": cost,
        "net_effective": listing.get("net_effective"),
        "suspect_pricing": listing.get("suspect_pricing"),
        "key_type": listing.get("_key_type"),
    }
    for field in STICKY_FIELDS:
        value = listing.get(field)
        record[field] = prior.get(field) if value is None else value
    for field in PRESERVED_FIELDS:
        record[field] = prior.get(field)
    return record


def reap_absent(ledger, seen_keys, today):
    """Mark anything absent DAYS_ABSENT_UNTIL_DEAD consecutive days as dead.

    Note this measures CALENDAR days since last_seen, not runs. On an
    every-other-day cadence a listing still dies ~7-8 days after it was last
    seen; the cadence only controls which run notices.
    """
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


def live_keys(ledger):
    """
    Every ledger key that is still on the market — the full cumulative set the
    reporter renders, not just this run's changes.

    `newly_dead` is a delta and only fires on the single run a listing dies.
    Anything downstream that removes listings by subtracting that delta strands
    a dead listing forever the first time a run is skipped or the reporter
    fails. This is the absolute set, so the shortlist reconciles to the ledger
    every run instead of accumulating drift.
    """
    return sorted(
        key for key, record in ledger["listings"].items()
        if record.get("status") != "dead"
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run(listings, ledger, today):
    merged, unresolvable = collapse_by_key(listings, ledger)
    duplicates = group_possible_duplicates(listings)
    buckets = classify(merged, ledger, today)
    newly_dead = reap_absent(ledger, set(merged.keys()), today)
    live = live_keys(ledger)

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
            "live_total": len(live),
            "possible_duplicates": len(duplicates),
            "unresolvable_addresses": len(unresolvable),
        },
        "new": buckets["new"],
        "price_change": buckets["price_change"],
        "relist": buckets["relist"],
        "possible_duplicates": duplicates,
        "unresolvable": unresolvable,
        "newly_dead": newly_dead,
        # The cumulative live set. reporter keeps shortlist.json entries whose
        # key appears here and drops every other one — an intersection against
        # current state, never a subtraction of newly_dead.
        "live_keys": live,
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
