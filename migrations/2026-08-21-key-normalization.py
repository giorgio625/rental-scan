#!/usr/bin/env python3
"""
One-time ledger migration: re-point keys that dedupe.py now normalizes.

Run:  python migrations/2026-08-21-key-normalization.py --dry-run
      python migrations/2026-08-21-key-normalization.py

Phase 2 changed how three things are normalized, so a handful of existing
canonical keys no longer match what the current code produces for the same
listing:

  * address ranges key off the low number  (649-51-... -> 649-...)
  * a trailing directional collapses       (...-lincoln-park-west-...
                                            -> ...-lincoln-park-w-...)
  * a unit jammed into a structured street_name is recovered
                                           (916-w-fullerton-ave-apt-3-2br-3000
                                            -> 916-w-fullerton-3)

Left alone, each stale key would go absent, die seven days later, and the
same apartment would return as `new` with a fresh first_seen, an empty
price_history, and no lat/lng or verdict. `ledger.json` is the one artifact
here that cannot be rebuilt, so this is committed rather than run ad hoc:
the file's history should be explicable six months from now.

The old->new mapping is not hardcoded. It is derived by replaying every
record in raw/ through the CURRENT dedupe.py and asking which ledger keys no
longer appear — which means this script needs no copy of the pre-Phase-2
code, and re-running it after the migration is a no-op.
"""

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dedupe  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def load_raw_records():
    records = []
    for path in sorted(glob.glob(str(ROOT / "raw" / "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload, list):
            records += [r for r in payload if isinstance(r, dict)]
    return records


def stored_building(old_key, record):
    """
    The building portion of an existing ledger key.

    canonical_key appends one of three suffixes to a building key, and the
    ledger record still carries the fields each was built from, so the
    suffix can be removed rather than guessed at.
    """
    unit = dedupe.normalize_unit(record.get("unit"))
    if unit:
        suffix = "-" + unit
    elif record.get("beds") is not None and record.get("rent_gross") is not None:
        suffix = "-%sbr-%d" % (record["beds"],
                               dedupe._fallback_bucket(record["rent_gross"]))
    else:
        suffix = "-UNKNOWN"
    return old_key[:-len(suffix)] if old_key.endswith(suffix) else None


def build_mapping(ledger, records):
    """
    Old ledger key -> key the current code produces for the same listing.

    Scoped narrowly, and deliberately so. The only keys migrated are those
    whose BUILDING portion the current code can no longer produce — which is
    exactly what Phase 2 changed. A key that is merely absent from today's
    harvest is left alone: harvesters vary their output run to run, and
    absence is not evidence that normalization moved.

    That distinction is what keeps this migration off two real pairs in the
    ledger that look superficially identical:

      541-w-oakdale-1br-1800 / 541-w-oakdale-0br-1800
          same address, same $1,795 rent, one harvester read it as a studio
          and another as a 1-bedroom
      2140-n-lincoln-park-w-807 / 2140-n-lincoln-park-807
          same address and unit; one harvester dropped the trailing
          directional from the street name

    Both are genuine `possible_duplicates`, and criteria.md is explicit that
    resolving those is the human's call. They are reported, never merged.
    """
    current_keys, current_buildings = set(), set()
    by_address = {}
    for record in records:
        key, _ = dedupe.canonical_key(record)
        if key is None:
            continue
        current_keys.add(key)
        current_buildings.add(dedupe.building_key(record))
        address = record.get("address_raw")
        if address:
            by_address.setdefault(address, set()).add(key)

    mapping, ambiguous = {}, []
    for old_key, stored in ledger["listings"].items():
        if old_key in current_keys:
            continue
        building = stored_building(old_key, stored)
        if building is None or building in current_buildings:
            # The building still normalizes the way it always did, so this
            # key did not go stale because of Phase 2.
            continue
        candidates = {k for k in by_address.get(stored.get("address_raw"), set())
                      if k != old_key}
        if len(candidates) == 1:
            mapping[old_key] = candidates.pop()
        elif candidates:
            ambiguous.append((old_key, sorted(candidates)))
    return mapping, ambiguous


def merge(into, other):
    """
    Fold `other` into `into`. The older sighting wins on history; a known
    value beats an unknown one everywhere else.
    """
    if other.get("first_seen") and (
        not into.get("first_seen") or other["first_seen"] < into["first_seen"]
    ):
        into["first_seen"] = other["first_seen"]
    if other.get("last_seen") and (
        not into.get("last_seen") or other["last_seen"] > into["last_seen"]
    ):
        into["last_seen"] = other["last_seen"]

    history = {(h.get("date"), h.get("rent")): h
               for h in (into.get("price_history") or [])}
    for entry in other.get("price_history") or []:
        history.setdefault((entry.get("date"), entry.get("rent")), entry)
    into["price_history"] = sorted(history.values(),
                                   key=lambda h: h.get("date") or "")

    sources = {(s.get("source"), s.get("url")): s
               for s in (into.get("sources") or [])}
    for source in other.get("sources") or []:
        sources.setdefault((source.get("source"), source.get("url")), source)
    into["sources"] = list(sources.values())

    # A live sighting outranks a dead one: the listing is on the market.
    if other.get("status") == "active":
        into["status"] = "active"
        into.pop("dead_since", None)

    for field, value in other.items():
        if field in ("first_seen", "last_seen", "price_history", "sources",
                     "status", "dead_since", "key"):
            continue
        if into.get(field) is None and value is not None:
            into[field] = value
    return into


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=str(ROOT / "ledger.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.ledger, encoding="utf-8") as fh:
        ledger = json.load(fh)

    before = len(ledger["listings"])
    mapping, ambiguous = build_mapping(ledger, load_raw_records())

    for old_key, candidates in ambiguous:
        print("SKIPPED (ambiguous, needs a human): %s -> one of %s"
              % (old_key, ", ".join(candidates)))

    if not mapping:
        print("nothing to migrate — every ledger key matches current dedupe.py")
        return

    renames, merges = [], []
    for old_key, new_key in sorted(mapping.items()):
        record = ledger["listings"].pop(old_key)
        record["key"] = new_key
        if new_key in ledger["listings"]:
            ledger["listings"][new_key] = merge(
                ledger["listings"][new_key], record)
            merges.append((old_key, new_key))
        else:
            ledger["listings"][new_key] = record
            renames.append((old_key, new_key))

    for old_key, new_key in renames:
        print("rename  %s\n     -> %s" % (old_key, new_key))
    for old_key, new_key in merges:
        print("merge   %s\n    into %s" % (old_key, new_key))
    print("\nkeys: %d -> %d (%d renamed, %d merged away)"
          % (before, len(ledger["listings"]), len(renames), len(merges)))

    if args.dry_run:
        print("\n--dry-run: ledger.json not written")
        return

    tmp = Path(args.ledger + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, sort_keys=True)
    tmp.replace(args.ledger)
    print("\nwrote %s" % args.ledger)


if __name__ == "__main__":
    main()
