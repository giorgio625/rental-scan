#!/usr/bin/env python3
"""One-time ledger repair, 2026-08-21. Recovers what earlier runs dropped.

Three separate losses, all repaired the same way -- a null in the ledger is
filled from a source that still has the value, and a non-null is never
touched. That is section 7a's sticky rule: a non-null observation wins, a
null one leaves the stored value alone.

1. `baths` / `sqft` were absent from EVERY ledger record. The section 7a
   sticky list has carried them all along, but the version of `dedupe.py` that
   wrote these records did not, and the field list was only widened in "Phase
   2: harden dedupe.py" -- which landed after the 2026-08-21 scan had already
   run. Recovered from `raw/classified-*.json`, which carry both the canonical
   `_key` and the full harvested record, so the join is exact rather than
   guessed from an address string.

2. Every Redfin and Zillow listing harvested on 2026-08-21 reached the ledger
   with `url: null`, and the dashboard rendered five `href="null"` links --
   dead links indistinguishable from live ones. A backfill harvest of the same
   Gmail window recovered a link for all five, so the emails did carry them;
   they were lost in extraction. Filled from
   `raw/2026-08-21-inbox-backfill.json`.

3. Two Zillow `routing/email/...zpid_target` tracking wrappers were sitting in
   `sources` unnormalized. Section 6a is explicit that these expire onto a
   generic page rather than erroring, so they read as working links forever.
   Rewritten to the canonical `homedetails/{ZPID}_zpid/` permalink.

Only (1) and (3) are general repairs; (2) is specific to one bad harvest.
`dedupe.py` at HEAD already writes `baths`/`sqft`, and `render_dashboard.py`
normalizes URLs at render time, so none of this should ever need running
again. It is kept for provenance.

    python migrations/2026-08-21-url-and-baths-backfill.py [--dry-run]
"""

import argparse
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from render_dashboard import normalize_url  # noqa: E402  section 6a, one copy

# Only fields the section 7a sticky list already promised. This migration
# recovers dropped data; it does not widen what the ledger stores.
RECOVER = ("baths", "sqft", "beds", "neighborhood_claimed", "available_date",
           "laundry", "parking_type", "outdoor_space", "unit_level")

BACKFILL_HARVEST = ROOT / "raw" / "2026-08-21-inbox-backfill.json"


def observations_from_classified():
    """{canonical key: {field: value}} from every archived classified run.

    Later runs override earlier ones for the same key: a listing re-harvested
    on day 5 with a stated bath count is a better observation than day 1's
    silence, and the files sort chronologically by name.
    """
    observed = {}
    for path in sorted(glob.glob(str(ROOT / "raw" / "classified-*.json"))):
        if "superseded" in path:
            continue                      # a partial run that a later one replaced
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        for bucket in ("new", "price_change", "relist"):
            for record in payload.get(bucket) or []:
                key = record.get("_key")
                if not key:
                    continue
                slot = observed.setdefault(key, {})
                for field in RECOVER:
                    if record.get(field) is not None:
                        slot[field] = record[field]
    return observed


def url_observations():
    """{(source, rent): url} from the 2026-08-21 backfill harvest."""
    if not BACKFILL_HARVEST.exists():
        return {}
    with BACKFILL_HARVEST.open(encoding="utf-8") as handle:
        records = json.load(handle)
    found = {}
    for record in records:
        url = normalize_url(record.get("url"))
        if not url:
            continue
        found[(record.get("source"), record.get("rent_gross"))] = {
            "url": url,
            "address": record.get("address_raw") or "",
            "baths": record.get("baths"),
        }
    return found


def street_number(address):
    match = re.match(r"\s*(\d+)", address or "")
    return match.group(1) if match else None


def main():
    parser = argparse.ArgumentParser(description="Repair the 2026-08-21 ledger.")
    parser.add_argument("--ledger", default=str(ROOT / "ledger.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ledger_path = Path(args.ledger)
    with ledger_path.open(encoding="utf-8") as handle:
        ledger = json.load(handle)
    listings = ledger["listings"]

    observed = observations_from_classified()
    urls = url_observations()

    filled = {field: 0 for field in RECOVER}
    urls_filled, wrappers_fixed, unmatched = [], [], []

    for key, record in listings.items():
        # (1) sticky fields the old dedupe.py never wrote.
        for field, value in observed.get(key, {}).items():
            if record.get(field) is None:
                record[field] = value
                filled[field] += 1

        for source in record.get("sources") or []:
            # (3) section 6a wrappers already stored.
            current = source.get("url")
            if current:
                fixed = normalize_url(current)
                if fixed and fixed != current:
                    source["url"] = fixed
                    wrappers_fixed.append((key, current, fixed))
                continue

            # (2) the 2026-08-21 null-URL harvest.
            match = urls.get((source.get("source"), record.get("rent_gross")))
            if not match:
                unmatched.append((key, source.get("source")))
                continue
            # Rent and source alone could collide; require the street number
            # to agree before writing a link onto a listing.
            here = street_number(record.get("address_raw"))
            there = street_number(match["address"])
            if here and there and here != there:
                unmatched.append((key, source.get("source")))
                continue
            source["url"] = match["url"]
            urls_filled.append((key, match["url"]))
            if record.get("baths") is None and match["baths"] is not None:
                record["baths"] = match["baths"]
                filled["baths"] += 1

    print("sticky fields recovered from raw/classified-*.json:")
    for field in RECOVER:
        if filled[field]:
            print("  %-20s %d record(s)" % (field, filled[field]))
    print("\nURLs filled from the backfill harvest: %d" % len(urls_filled))
    for key, url in urls_filled:
        print("  %-38s %s" % (key, url))
    print("\nSection 6a wrappers normalized: %d" % len(wrappers_fixed))
    for key, before, after in wrappers_fixed:
        print("  %s\n    %s\n    -> %s" % (key, before, after))
    if unmatched:
        print("\nStill no link (nothing in the backfill harvest matched): %d"
              % len(unmatched))
        for key, source in unmatched:
            print("  %-38s %s" % (key, source))

    if args.dry_run:
        print("\n--dry-run: ledger not written")
        return

    tmp = Path(str(ledger_path) + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(ledger, handle, indent=2, sort_keys=True)
    tmp.replace(ledger_path)
    print("\nwrote %s" % ledger_path)


if __name__ == "__main__":
    main()
