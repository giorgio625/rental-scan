#!/usr/bin/env python3
"""
Regression tests for dedupe.py.

Run:  python -m unittest discover -s tests -v

`unittest` rather than pytest deliberately. dedupe.py and enrich_select.py
both advertise "stdlib only, no dependencies", and the scan runs unattended
in a sandbox with no outbound network — a suite that needs `pip install`
first is a suite that does not run on the machine that matters.

PLAYBOOK.md phase 3 described these checks as a manual console read-through
performed once, at build time. That is exactly the kind of verification that
stops happening: the fixture expectations below (6 ingested / 5 unique / 1
duplicate, $3,651.67 all-in, N-vs-S separation) come straight from that
table, and until now nothing re-checked them after a code change.
"""

import itertools
import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dedupe  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def empty_ledger():
    return {"listings": {}, "meta": {}}


def listing(**overrides):
    """A minimal well-formed listing; override what a test cares about."""
    base = {
        "source": "zillow",
        "url": "https://example.test/1",
        "street_number": "1920",
        "street_directional": "N",
        "street_name": "Milwaukee Ave",
        "unit": None,
        "beds": 1,
        "rent_gross": 2800,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Address normalization
# --------------------------------------------------------------------------

class TestNormalization(unittest.TestCase):

    def test_directional_forms_collapse(self):
        for raw in ("N", "n", "N.", "North", "north"):
            self.assertEqual(dedupe.normalize_directional(raw), "n", raw)
        self.assertEqual(dedupe.normalize_directional(None), "")
        self.assertEqual(dedupe.normalize_directional("X"), "")

    def test_street_suffix_stripped_only_at_the_end(self):
        self.assertEqual(dedupe.normalize_street_name("Damen Ave"), "damen")
        # 'Place' is the suffix; 'St James' is the name. Stripping every
        # suffix-looking token would leave 'james'.
        self.assertEqual(dedupe.normalize_street_name("St James Place"),
                         "st-james")

    def test_trailing_directional_collapses_to_one_letter(self):
        # Three harvesters produced three spellings for one building.
        self.assertEqual(dedupe.normalize_street_name("Lincoln Park West"),
                         "lincoln-park-w")
        self.assertEqual(dedupe.normalize_street_name("Lincoln Park W"),
                         "lincoln-park-w")

    def test_north_avenue_keeps_its_name(self):
        # 'North' here is the street, not a directional to strip.
        self.assertEqual(dedupe.normalize_street_name("North Ave"), "north")

    def test_unit_prefix_dropped_wherever_it_sits(self):
        self.assertEqual(dedupe.normalize_unit("Unit 2R"), "2R")
        self.assertEqual(dedupe.normalize_unit("#3-N"), "3N")
        self.assertEqual(dedupe.normalize_unit("apt 211"), "211")
        # Trailing, not leading — "Garden Unit" must not become GARDENUNIT.
        self.assertEqual(dedupe.normalize_unit("Garden Unit"), "GARDEN")
        self.assertIsNone(dedupe.normalize_unit(None))
        self.assertIsNone(dedupe.normalize_unit(""))

    def test_range_address_keys_off_its_low_number(self):
        self.assertEqual(dedupe.normalize_street_number("649-51"), "649")
        self.assertEqual(dedupe.normalize_street_number("649"), "649")


# --------------------------------------------------------------------------
# parse_address_raw — the fallback path, used by ~1 raw record in 6
# --------------------------------------------------------------------------

class TestParseAddressRaw(unittest.TestCase):
    """
    Every string below was harvested from a real alert email. Before this
    was rewritten, 48% of the 312 distinct address_raw values on record
    parsed to {} — anything carrying a ', Chicago, IL 60622' tail.
    """

    CASES = [
        ("1407 N Milwaukee Avenue, Chicago, IL 60622",
         "1407", "n", "milwaukee", None),
        ("1547 N. Damen Ave Unit 3W", "1547", "n", "damen", "3W"),
        ("2048 W Evergreen Ave #1", "2048", "w", "evergreen", "1"),
        ("1920 N Milwaukee Ave, Unit 306", "1920", "n", "milwaukee", "306"),
        ("2510 N Wayne Ave Apt 211, Chicago, IL", "2510", "n", "wayne", "211"),
        ("1600 W Division St, Chicago, IL 60622", "1600", "w", "division", None),
        ("850 W Cortland St", "850", "w", "cortland", None),
        ("1 N State St, Chicago, Illinois 60602", "1", "n", "state", None),
        ("1234 North Ashland Avenue Chicago IL 60622",
         "1234", "n", "ashland", None),
        ("2750 N Kenmore Ave, Garden Unit", "2750", "n", "kenmore", "GARDEN"),
        ("1140 N Wells St #615, Chicago, IL 60610", "1140", "n", "wells", "615"),
        ("916 W Fullerton Ave APT 3, Chicago, IL", "916", "w", "fullerton", "3"),
        ("452 West St James Place", "452", "w", "st-james", None),
        ("1600 W North Avenue, Chicago, IL", "1600", "w", "north", None),
    ]

    # A bare token after a street SUFFIX is a unit; after a directional it is
    # not. "1451 N Ashland Avenue S" is unit S, not a second directional.
    BARE_UNIT_CASES = [
        ("1235 N Ashland Avenue 502, Chicago, IL 60622",
         "1235", "n", "ashland", "502"),
        ("1451 N Ashland Avenue S, Chicago, IL 60622",
         "1451", "n", "ashland", "S"),
        ("1514 N Wood Street 1N, Chicago, IL 60622", "1514", "n", "wood", "1N"),
        ("1557 N Honore Street G, Chicago, IL 60622", "1557", "n", "honore", "G"),
        ("1557 N Honore St Unit G, Chicago, IL", "1557", "n", "honore", "G"),
    ]

    # The city is found by position, not by name. Hardcoding {"chicago"}
    # read "Schaumburg" as the unit.
    NON_CHICAGO_CASES = [
        ("299 Pembridge Ln #D2, Schaumburg, IL 60193",
         "299", "", "pembridge", "D2"),
        ("8041 W Bluemound Rd #412, Milwaukee, IL 53213",
         "8041", "w", "bluemound", "412"),
    ]

    RANGE_CASES = [
        ("2061-2071 N. Southport Ave.", "2061", "n", "southport", None),
        ("1413-15 E. 57th, Chicago, IL 60637", "1413", "e", "57th", None),
        ("649-51 N. Wolcott, Unit #1", "649", "n", "wolcott", "1"),
    ]

    def _check(self, cases):
        for raw, number, direction, street, unit in cases:
            with self.subTest(raw=raw):
                parts = dedupe.parse_address_raw(raw)
                self.assertEqual(parts.get("street_number"), number)
                self.assertEqual(parts.get("street_directional"), direction)
                self.assertEqual(
                    dedupe.normalize_street_name(parts.get("street_name")),
                    street)
                self.assertEqual(
                    dedupe.normalize_unit(parts.get("unit")), unit)

    def test_real_addresses(self):
        self._check(self.CASES)

    def test_bare_trailing_unit(self):
        self._check(self.BARE_UNIT_CASES)

    def test_city_detected_by_position_not_by_name(self):
        self._check(self.NON_CHICAGO_CASES)

    def test_address_ranges(self):
        self._check(self.RANGE_CASES)

    def test_unparseable_returns_empty_not_a_guess(self):
        """
        No house number means no address. These route to `unresolvable` for
        a human, which is the honest outcome — a plausible-looking wrong
        parse mints a second key for a building already in the ledger and
        nothing downstream can see that it happened.
        """
        for raw in ("Wicker Park", "Logan Square",
                    "AMLI Lofts, Chicago, IL 60605",
                    "N Broadway #3833-320, Chicago, IL", "", None):
            with self.subTest(raw=raw):
                self.assertEqual(dedupe.parse_address_raw(raw), {})

    def test_unit_never_swallowed_by_a_comma_tail(self):
        # ", Unit G, IL" — the city-strip must not eat the unit segment.
        parts = dedupe.parse_address_raw("1557 N Honore St, Unit G, IL")
        self.assertEqual(dedupe.normalize_unit(parts.get("unit")), "G")


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

class TestKeys(unittest.TestCase):

    def test_directional_is_load_bearing(self):
        """1600 N Damen and 1600 S Damen are four miles apart."""
        north = listing(street_number="1600", street_directional="N",
                        street_name="Damen Ave", unit="3W")
        south = listing(street_number="1600", street_directional="S",
                        street_name="Damen Ave", unit="3W")
        self.assertNotEqual(dedupe.canonical_key(north)[0],
                            dedupe.canonical_key(south)[0])

    def test_unit_recovered_from_a_structured_street_name(self):
        """
        A harvester wrote street_name "Fullerton Ave APT 3" with unit null.
        Both structured fields were present, so parse_address_raw was never
        consulted and the unit went into the key.
        """
        key, key_type = dedupe.canonical_key(listing(
            street_number="916", street_directional="w",
            street_name="Fullerton Ave APT 3", unit=None,
            beds=2, rent_gross=3000))
        self.assertEqual(key, "916-w-fullerton-3")
        self.assertEqual(key_type, "primary")

    def test_a_supplied_unit_is_believed(self):
        """Recovery only fires when the unit is otherwise missing."""
        key, _ = dedupe.canonical_key(listing(
            street_number="916", street_directional="w",
            street_name="Fullerton Ave", unit="5"))
        self.assertEqual(key, "916-w-fullerton-5")

    def test_range_and_single_number_land_on_one_key(self):
        structured = listing(street_number="649-51", street_directional="N",
                             street_name="Wolcott", unit="1")
        raw_only = {"source": "domu",
                    "address_raw": "649-51 N. Wolcott, Unit #1"}
        single = listing(street_number="649", street_directional="N",
                         street_name="Wolcott", unit="#1")
        keys = {dedupe.canonical_key(x)[0]
                for x in (structured, raw_only, single)}
        self.assertEqual(len(keys), 1, keys)

    def test_missing_address_is_unresolvable_not_invented(self):
        key, key_type = dedupe.canonical_key({"source": "craigslist",
                                              "address_raw": None})
        self.assertIsNone(key)
        self.assertEqual(key_type, "unresolvable")


# --------------------------------------------------------------------------
# Fallback key stability
# --------------------------------------------------------------------------

class TestFallbackKeyStability(unittest.TestCase):
    """
    A fallback key embeds the rent bucketed to $50, so without stabilization
    a $1 rent change re-identifies the apartment: the old key goes absent,
    dies seven days later, and the same unit returns as `new` with an empty
    price_history. Four of the 32 live fallback keys sit within $10 of a
    bucket boundary.
    """

    def _seed(self, rent, day=date(2026, 8, 20)):
        ledger = empty_ledger()
        dedupe.run([listing(rent_gross=rent)], ledger, day)
        return ledger

    def test_crossing_a_bucket_boundary_is_a_price_change(self):
        ledger = self._seed(2974)
        self.assertEqual(sorted(ledger["listings"]),
                         ["1920-n-milwaukee-1br-2950"])

        result = dedupe.run([listing(rent_gross=2975)], ledger,
                            date(2026, 8, 21))

        self.assertEqual(result["counts"]["new"], 0)
        self.assertEqual(result["counts"]["price_change"], 1)
        self.assertEqual(sorted(ledger["listings"]),
                         ["1920-n-milwaukee-1br-2950"])
        changed = result["price_change"][0]
        self.assertEqual(changed["rent_delta"], 1.0)
        self.assertEqual(changed["first_seen"], "2026-08-20")

    def test_two_unitless_listings_straddling_a_boundary_stay_separate(self):
        """
        The dangerous direction. Collapsing two real apartments into one
        ledger entry loses one of them silently, which is strictly worse
        than the phantom-relist bug this fix exists to remove.
        """
        ledger = self._seed(2974)
        result = dedupe.run(
            [listing(rent_gross=2975, source="a"),
             listing(rent_gross=3040, source="b")],
            ledger, date(2026, 8, 21))
        self.assertEqual(result["counts"]["unique_units"], 2)
        self.assertEqual(sorted(ledger["listings"]),
                         ["1920-n-milwaukee-1br-2950",
                          "1920-n-milwaukee-1br-3050"])

    def test_a_jump_beyond_one_bucket_is_a_different_apartment(self):
        ledger = self._seed(2500)
        result = dedupe.run([listing(rent_gross=2600)], ledger,
                            date(2026, 8, 21))
        self.assertEqual(result["counts"]["new"], 1)
        self.assertEqual(result["counts"]["price_change"], 0)

    def test_claims_do_not_depend_on_input_order(self):
        outcomes = set()
        pair = [listing(rent_gross=2975, source="a"),
                listing(rent_gross=3040, source="b")]
        for permutation in itertools.permutations(pair):
            ledger = self._seed(2974)
            dedupe.run(list(permutation), ledger, date(2026, 8, 21))
            outcomes.add(tuple(sorted(ledger["listings"])))
        self.assertEqual(len(outcomes), 1, outcomes)

    def test_a_dead_entry_is_not_claimed(self):
        """Probing only re-points at LIVE entries; a dead one relists."""
        ledger = self._seed(2974)
        record = ledger["listings"]["1920-n-milwaukee-1br-2950"]
        record["status"] = "dead"
        record["dead_since"] = "2026-08-21"
        result = dedupe.run([listing(rent_gross=2975)], ledger,
                            date(2026, 8, 25))
        self.assertEqual(result["counts"]["new"], 1)


# --------------------------------------------------------------------------
# Comps
# --------------------------------------------------------------------------

class TestComps(unittest.TestCase):

    def _ledger_with(self, records):
        return {"listings": {"k%d" % i: r for i, r in enumerate(records)},
                "meta": {}}

    def test_out_of_bounds_records_do_not_set_the_comp(self):
        """
        The ledger holds every record the harvesters returned — filtering is
        downstream's job — so South Loop and Hyde Park rents sit in it. That
        median is the denominator of section 4's value score.
        """
        wicker = [{"status": "active", "beds": 2, "rent_gross": 3500,
                   "lat": 41.9089, "lng": -87.6773} for _ in range(5)]
        south_loop = [{"status": "active", "beds": 2, "rent_gross": 2000,
                       "lat": 41.8636, "lng": -87.6269} for _ in range(5)]
        medians = dedupe.running_medians(self._ledger_with(wicker + south_loop))
        self.assertEqual(medians[2], 3500)

    def test_uncoordinated_records_still_count(self):
        """
        The geocoder runs AFTER dedupe, so every listing is uncoordinated on
        its first pass. Excluding those would drop each new record from the
        comps it is being judged against.
        """
        records = [{"status": "active", "beds": 2, "rent_gross": 3500,
                    "lat": None, "lng": None} for _ in range(5)]
        self.assertEqual(
            dedupe.running_medians(self._ledger_with(records))[2], 3500)

    def test_dead_records_do_not_count(self):
        records = [{"status": "dead", "beds": 2, "rent_gross": 3500,
                    "lat": None, "lng": None} for _ in range(5)]
        self.assertNotIn(2, dedupe.running_medians(self._ledger_with(records)))

    def test_thin_bed_counts_produce_no_median(self):
        records = [{"status": "active", "beds": 2, "rent_gross": 3500,
                    "lat": None, "lng": None}
                   for _ in range(dedupe.MIN_COMPS_FOR_MEDIAN - 1)]
        self.assertNotIn(2, dedupe.running_medians(self._ledger_with(records)))


class TestZones(unittest.TestCase):

    def test_known_coordinates_land_in_the_right_zone(self):
        cases = [
            (41.9089, -87.6773, "wicker_park"),
            (41.9214, -87.6773, "bucktown"),
            (41.8990, -87.6773, "east_village"),
            (41.9250, -87.6540, "lincoln_park"),
        ]
        for lat, lng, zone in cases:
            with self.subTest(zone=zone):
                self.assertEqual(dedupe.zone_of(lat, lng), zone)

    def test_out_of_area_is_none_and_missing_is_unknown(self):
        self.assertIsNone(dedupe.zone_of(41.7913, -87.5909))    # Hyde Park
        self.assertIsNone(dedupe.zone_of(41.8636, -87.6269))    # South Loop
        self.assertEqual(dedupe.zone_of(None, None), "unknown")

    def test_boundary_addresses_are_in_bounds(self):
        """
        A building fronting the boundary street geocodes a hair outside the
        computed line. criteria.md treats those as in bounds.
        """
        self.assertIsNotNone(dedupe.zone_of(41.9030, -87.6677))


# --------------------------------------------------------------------------
# Ledger record shape
# --------------------------------------------------------------------------

class TestLedgerRecord(unittest.TestCase):

    RICH = dict(unit="3W", beds=2, sqft=1100, laundry="in_unit",
                heat_included=False, loft_signals=["exposed_brick"],
                ceiling_height_ft=13, rent_gross=3200,
                street_number="1547", street_name="Damen Ave",
                street_directional="N")

    def _seeded(self):
        ledger = empty_ledger()
        dedupe.run([listing(**self.RICH)], ledger, date(2026, 8, 20))
        return ledger

    def test_descriptive_fields_survive_a_thinner_harvest(self):
        """
        Harvesters emit null for "not stated", never for "known absent", so
        a silent email is not a correction.
        """
        ledger = self._seeded()
        thin = listing(street_number="1547", street_name="Damen Ave",
                       street_directional="N", unit="3W", beds=2,
                       rent_gross=3100, source="domu")
        dedupe.run([thin], ledger, date(2026, 8, 21))

        record = ledger["listings"]["1547-n-damen-3W"]
        self.assertEqual(record["sqft"], 1100)
        self.assertEqual(record["laundry"], "in_unit")
        self.assertEqual(record["loft_signals"], ["exposed_brick"])
        self.assertEqual(record["ceiling_height_ft"], 13)
        self.assertIs(record["heat_included"], False)  # not None, not lost

    def test_current_state_fields_are_overwritten(self):
        ledger = self._seeded()
        dedupe.run([listing(**dict(self.RICH, rent_gross=3100))],
                   ledger, date(2026, 8, 21))
        record = ledger["listings"]["1547-n-damen-3W"]
        self.assertEqual(record["rent_gross"], 3100)
        self.assertEqual(record["first_seen"], "2026-08-20")
        self.assertEqual(len(record["price_history"]), 2)

    def test_human_and_geocoder_fields_are_never_touched(self):
        ledger = self._seeded()
        record = ledger["listings"]["1547-n-damen-3W"]
        record["verdict"] = "shortlisted"
        record["lat"], record["lng"] = 41.9089, -87.6773

        dedupe.run([listing(**dict(self.RICH, rent_gross=3100))],
                   ledger, date(2026, 8, 21))

        record = ledger["listings"]["1547-n-damen-3W"]
        self.assertEqual(record["verdict"], "shortlisted")
        self.assertEqual(record["lat"], 41.9089)
        self.assertEqual(record["lng"], -87.6773)

    def test_shortlist_fields_are_all_present(self):
        """
        reporter.md builds shortlist.json from these. If the ledger cannot
        supply them, the shortlist can only be carried forward, never rebuilt.
        """
        ledger = self._seeded()
        record = ledger["listings"]["1547-n-damen-3W"]
        for field in ("address_raw", "unit", "beds", "baths", "sqft", "url",
                      "rent_gross", "all_in_monthly", "loft_type",
                      "loft_signals", "layout", "outdoor_space",
                      "parking_type", "laundry", "available_date",
                      "lat", "lng", "sources", "first_seen"):
            self.assertIn(field, record, field)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

class TestLifecycle(unittest.TestCase):

    def test_absence_kills_only_after_the_full_window(self):
        window = dedupe.DAYS_ABSENT_UNTIL_DEAD
        ledger = empty_ledger()
        dedupe.run([listing()], ledger, date(2026, 8, 1))

        result = dedupe.run([], ledger, date(2026, 8, 1 + window - 1))
        self.assertEqual(result["counts"]["newly_dead"], 0)

        result = dedupe.run([], ledger, date(2026, 8, 1 + window))
        self.assertEqual(result["counts"]["newly_dead"], 1)

    def test_live_keys_is_absolute_not_a_delta(self):
        """
        reporter.md removes shortlist entries by intersecting with live_keys.
        Subtracting newly_dead instead strands a dead listing permanently the
        first time a run is skipped.
        """
        ledger = empty_ledger()
        dedupe.run([listing(),
                    listing(unit="2", url="https://example.test/2")],
                   ledger, date(2026, 8, 1))
        result = dedupe.run([], ledger, date(2026, 8, 20))
        self.assertEqual(result["live_keys"], [])
        self.assertEqual(result["counts"]["newly_dead"], 2)
        # A later run reports nothing newly dead; live_keys still says none.
        result = dedupe.run([], ledger, date(2026, 8, 21))
        self.assertEqual(result["counts"]["newly_dead"], 0)
        self.assertEqual(result["live_keys"], [])

    def test_a_dead_listing_returning_is_a_relist(self):
        ledger = empty_ledger()
        dedupe.run([listing()], ledger, date(2026, 8, 1))
        dedupe.run([], ledger, date(2026, 8, 20))
        result = dedupe.run([listing()], ledger, date(2026, 8, 25))
        self.assertEqual(result["counts"]["relist"], 1)
        self.assertEqual(result["relist"][0]["days_dead"], 5)


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------

class TestAllInRent(unittest.TestCase):

    def test_estimates_are_declared_not_hidden(self):
        amount, assumptions = dedupe.all_in_rent(listing(
            rent_gross=3200, parking_cost=200, heat_included=False,
            loft_type="hard", move_in_fee=500))
        self.assertAlmostEqual(amount, 3651.67, places=2)
        self.assertEqual(len(assumptions), 2)
        self.assertTrue(any("heat" in a for a in assumptions))
        self.assertTrue(any("move-in fee" in a for a in assumptions))

    def test_unknown_heat_is_flagged_not_priced_as_included(self):
        amount, assumptions = dedupe.all_in_rent(
            listing(rent_gross=3000, heat_included=None))
        self.assertEqual(amount, 3000)
        self.assertTrue(any("unknown" in a for a in assumptions))

    def test_no_rent_yields_no_number(self):
        amount, assumptions = dedupe.all_in_rent(listing(rent_gross=None))
        self.assertIsNone(amount)
        self.assertTrue(assumptions)


# --------------------------------------------------------------------------
# PLAYBOOK.md phase 3, as an actual test
# --------------------------------------------------------------------------

class TestPlaybookFixtures(unittest.TestCase):
    """The five checks PLAYBOOK.md phase 3 asked to be eyeballed once."""

    @classmethod
    def setUpClass(cls):
        cls.ledger = empty_ledger()
        with (FIXTURES / "day1.json").open(encoding="utf-8") as fh:
            cls.day1 = json.load(fh)
        cls.result = dedupe.run(cls.day1, cls.ledger, date(2026, 8, 13))

    def test_counts(self):
        counts = self.result["counts"]
        self.assertEqual(counts["ingested"], 6)
        self.assertEqual(counts["unique_units"], 5)
        self.assertEqual(counts["possible_duplicates"], 1)

    def test_zillow_and_redfin_collapse_to_one_record(self):
        record = self.ledger["listings"]["1547-n-damen-3W"]
        self.assertEqual(len(record["sources"]), 2)

    def test_field_merge_keeps_the_most_complete_version(self):
        record = self.ledger["listings"]["1547-n-damen-3W"]
        self.assertEqual(record["ceiling_height_ft"], 13)

    def test_north_and_south_damen_stay_separate(self):
        self.assertIn("1547-n-damen-3W", self.ledger["listings"])
        self.assertIn("1547-s-damen-3W", self.ledger["listings"])

    def test_all_in_math(self):
        record = self.ledger["listings"]["1547-n-damen-3W"]
        self.assertAlmostEqual(record["all_in_monthly"], 3651.67, places=2)

    def test_the_unitless_record_gets_a_fallback_key_not_a_guess(self):
        self.assertIn("1547-n-damen-2br-3200", self.ledger["listings"])

    def test_day_two_transitions(self):
        with (FIXTURES / "day2.json").open(encoding="utf-8") as fh:
            day2 = json.load(fh)
        ledger = json.loads(json.dumps(self.ledger))
        result = dedupe.run(day2, ledger, date(2026, 8, 14))
        self.assertGreaterEqual(result["counts"]["price_change"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
