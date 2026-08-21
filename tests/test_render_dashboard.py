#!/usr/bin/env python3
"""
Regression tests for render_dashboard.py.

Run:  python -m unittest discover -s tests -v

Same reasoning as test_dedupe.py: stdlib `unittest`, no network, no fixtures
that need downloading. This file exists because the dashboards have now
failed twice in ways nothing downstream could catch --

  2026-08-20  both pages rendered with zero listings while the markdown
              report correctly carried a near miss, and the run reported
              success.
  2026-08-21  five source links shipped as href="null": dead links that look
              live until tapped, which is the one failure mode section 6a
              calls out by name.

Both were mechanical, so the mechanics moved into a script and the checks
that would have caught them live here.
"""

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import render_dashboard as rd  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "mockup.html"


def ledger(**records):
    return {"listings": records, "meta": {}}


def record(**overrides):
    base = {
        "address_raw": "1620 N Damen Ave", "unit": None, "beds": 2,
        "baths": 2, "sqft": 1400, "rent_gross": 3150, "all_in_monthly": 3150,
        "first_seen": "2026-08-20", "last_seen": "2026-08-20",
        "status": "active", "lat": 41.9105, "lng": -87.6772,
        "sources": [{"source": "domu", "url": "https://www.domu.com/x"}],
    }
    base.update(overrides)
    return base


def card(**overrides):
    base = {"key": "k1", "tier": "near", "score": 0}
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Section 6a -- URL normalization
# --------------------------------------------------------------------------

class NormalizeUrl(unittest.TestCase):

    def test_null_and_junk_become_none(self):
        # The 2026-08-21 bug in one line: a null URL must never come out of
        # here as something a template will put in an href.
        for value in (None, "", "   ", 12345, "not a url", "ftp://x/y"):
            self.assertIsNone(rd.normalize_url(value), repr(value))

    def test_both_zillow_wrapper_spellings_rewrite_to_the_permalink(self):
        # Section 6a: "routed" and "routing" both appear in real alert mail.
        permalink = "https://www.zillow.com/homedetails/464607265_zpid/"
        for spelling in ("routed", "routing"):
            url = ("https://www.zillow.com/%s/email/property-notifications"
                   "/zpid_target/464607265_zpid" % spelling)
            self.assertEqual(rd.normalize_url(url), permalink)

    def test_click_wrapper_is_unwrapped_then_normalized(self):
        # Nested: a click.mail wrapper carrying a routed wrapper. Unwrapping
        # once and stopping would leave an expiring link in place.
        inner = ("https%3A%2F%2Fwww.zillow.com%2Frouting%2Femail%2F"
                 "property-notifications%2Fzpid_target%2F999_zpid")
        url = "https://click.mail.zillow.com/f/a/tok/AAAAARA~/tok2?target=" + inner
        self.assertEqual(rd.normalize_url(url),
                         "https://www.zillow.com/homedetails/999_zpid/")

    def test_empty_click_wrapper_is_not_a_link(self):
        self.assertIsNone(
            rd.normalize_url("https://click.mail.zillow.com/f/a/tok/AAAAARA~/t"))

    def test_http_is_upgraded_and_tracking_params_stripped(self):
        url = ("http://www.domu.com/chicago/1140-n-wells"
               "?utm_source=alert&rgid=88&signature=abc&unit=615")
        self.assertEqual(rd.normalize_url(url),
                         "https://www.domu.com/chicago/1140-n-wells?unit=615")

    def test_building_level_url_passes_through_unchanged(self):
        # Section 6a is explicit: do NOT synthesize a unit-level URL. A
        # floorplan page is the real link the source published.
        url = "https://www.rentcafe.com/apartments/il/chicago/x/default.aspx"
        self.assertEqual(rd.normalize_url(url), url)


class SourceEntries(unittest.TestCase):

    def test_unlinkable_source_is_kept_with_a_null_url(self):
        # Dropping it would hide that the listing was seen at all; the
        # template renders this as text that says there is no link.
        entries = rd.source_entries(record(sources=[{"source": "redfin", "url": None}]))
        self.assertEqual(entries, [{"label": "Redfin", "url": None}])

    def test_unknown_source_is_labelled_not_dropped(self):
        entries = rd.source_entries(record(
            sources=[{"source": "newsite", "url": "https://a.example/1"}]))
        self.assertEqual(entries[0]["label"], "Newsite")

    def test_duplicate_source_rows_collapse(self):
        entries = rd.source_entries(record(sources=[
            {"source": "domu", "url": "http://www.domu.com/x"},
            {"source": "domu", "url": "https://www.domu.com/x"},
        ]))
        self.assertEqual(len(entries), 1)


# --------------------------------------------------------------------------
# Card assembly
# --------------------------------------------------------------------------

class BuildCards(unittest.TestCase):

    def test_beds_and_baths_are_joined_from_the_ledger(self):
        cards, missing = rd.build_cards(
            {"cards": [card()]}, ledger(k1=record()))
        self.assertEqual((cards[0]["beds"], cards[0]["baths"]), (2, 2))
        self.assertEqual(missing, [])

    def test_reporter_value_beats_the_ledger(self):
        # Section 4b: where enrichment and the harvest disagree, the page wins.
        cards, _ = rd.build_cards(
            {"cards": [card(beds=3)]}, ledger(k1=record(beds=2)))
        self.assertEqual(cards[0]["beds"], 3)

    def test_zero_beds_is_a_studio_not_a_missing_value(self):
        # 0 is falsy; a truthiness test here would silently turn every studio
        # into "beds not listed".
        cards, _ = rd.build_cards(
            {"cards": [card(beds=0)]}, ledger(k1=record(beds=2)))
        self.assertEqual(cards[0]["beds"], 0)

    def test_unstated_bath_count_stays_none(self):
        # Section 6: null means "not stated", never "known absent". Nothing
        # here may substitute a plausible number.
        cards, _ = rd.build_cards(
            {"cards": [card()]}, ledger(k1=record(baths=None)))
        self.assertIsNone(cards[0]["baths"])

    def test_card_without_a_key_is_a_hard_failure(self):
        with self.assertRaises(SystemExit):
            rd.build_cards({"cards": [{"tier": "near"}]}, ledger())

    def test_unknown_tier_is_a_hard_failure(self):
        with self.assertRaises(SystemExit):
            rd.build_cards({"cards": [card(tier="maybe")]}, ledger(k1=record()))

    def test_key_missing_from_the_ledger_is_reported_not_swallowed(self):
        cards, missing = rd.build_cards({"cards": [card(key="ghost")]}, ledger())
        self.assertEqual(missing, ["ghost"])
        self.assertIsNone(cards[0]["lat"])

    def test_wrapper_url_on_a_card_is_normalized(self):
        cards, _ = rd.build_cards({"cards": [card(sources=[{
            "label": "Zillow",
            "url": "https://www.zillow.com/routing/email/property-notifications"
                   "/zpid_target/77_zpid"}])]}, ledger(k1=record()))
        self.assertEqual(cards[0]["sources"][0]["url"],
                         "https://www.zillow.com/homedetails/77_zpid/")


# --------------------------------------------------------------------------
# Past 7 days
# --------------------------------------------------------------------------

class BuildWeekly(unittest.TestCase):

    def setUp(self):
        self.ledger = ledger(
            today=record(first_seen="2026-08-21"),
            edge_in=record(first_seen="2026-08-15"),
            edge_out=record(first_seen="2026-08-14"),
            gone=record(first_seen="2026-08-18", status="dead"),
            ancient=record(first_seen="2026-01-01"),
            undated=record(first_seen=None),
        )

    def test_window_is_seven_days_inclusive_of_both_ends(self):
        rows, start = rd.build_weekly(self.ledger, date(2026, 8, 21), set())
        self.assertEqual(start, date(2026, 8, 15))
        self.assertEqual({r["key"] for r in rows},
                         {"today", "edge_in", "gone"})

    def test_dead_listings_are_included(self):
        # The board answers "what should I look at". This answers "what did
        # the scan see", and something that came and went inside the week is
        # part of that answer.
        rows, _ = rd.build_weekly(self.ledger, date(2026, 8, 21), set())
        self.assertEqual([r["status"] for r in rows if r["key"] == "gone"],
                         ["dead"])

    def test_unparseable_first_seen_is_skipped_not_crashed_on(self):
        rows, _ = rd.build_weekly(
            ledger(bad=record(first_seen="not-a-date")), date(2026, 8, 21), set())
        self.assertEqual(rows, [])

    def test_newest_day_first(self):
        rows, _ = rd.build_weekly(self.ledger, date(2026, 8, 21), set())
        self.assertEqual([r["date"] for r in rows],
                         sorted((r["date"] for r in rows), reverse=True))

    def test_board_membership_is_flagged(self):
        rows, _ = rd.build_weekly(self.ledger, date(2026, 8, 21), {"today"})
        flags = {r["key"]: r["onBoard"] for r in rows}
        self.assertTrue(flags["today"])
        self.assertFalse(flags["edge_in"])


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

class Render(unittest.TestCase):

    def setUp(self):
        self.template = TEMPLATE.read_text(encoding="utf-8")

    def render(self, cards, weekly=(), subtitle="sub"):
        return rd.render(self.template, cards, list(weekly),
                         {"start": "2026-08-15", "end": "2026-08-21"},
                         subtitle, '    <div class="archive-nav" '
                         'id="archiveNav">\n      x\n    </div>')

    def test_no_null_href_reaches_the_page(self):
        cards, _ = rd.build_cards(
            {"cards": [card()]},
            ledger(k1=record(sources=[{"source": "redfin", "url": None}])))
        html = self.render(cards)
        self.assertNotIn('"url": "null"', html)
        self.assertIn('"url": null', html)

    def test_listing_text_cannot_close_the_script_block(self):
        cards, _ = rd.build_cards(
            {"cards": [card(address="Loft </script><img src=x> Ave")]},
            ledger(k1=record()))
        html = self.render(cards)
        self.assertNotIn("</script><img", html)
        # Entity-escaped by _escape, which is the layer that actually fires.
        # (_js also rewrites a bare `<` to <, but it is unreachable while
        # _escape runs first -- kept as a second layer, not asserted here.)
        self.assertIn("Loft &lt;/script&gt;&lt;img src=x&gt; Ave", html)

    def test_quotes_in_listing_text_cannot_break_an_attribute(self):
        cards, _ = rd.build_cards(
            {"cards": [card(address='The "Ludlow"')]}, ledger(k1=record()))
        self.assertIn("&quot;Ludlow&quot;", self.render(cards))

    def test_both_data_arrays_are_replaced(self):
        # The sample rows shipped in the template must not survive into a
        # rendered page -- a stale mockup listing on the real board reads as a
        # real apartment.
        html = self.render([], [])
        self.assertIn("const listings = [];", html)
        self.assertIn("const weekly = [];", html)
        self.assertNotIn("1620-n-damen-4c", html)

    def test_missing_anchor_fails_loudly(self):
        with self.assertRaises(SystemExit):
            rd.render("<html>no anchors here</html>", [], [],
                      {"start": "a", "end": "b"}, "s", "nav")


class ArchiveNav(unittest.TestCase):

    def test_current_day_is_marked_and_order_is_newest_first(self):
        with TemporaryDirectory() as tmp:
            docs = Path(tmp)
            for name in ("2026-08-19.html", "2026-08-21.html",
                         "2026-08-20.html", "index.html", "notes.html"):
                (docs / name).write_text("x", encoding="utf-8")
            nav = rd.archive_nav(docs, "2026-08-21")
        self.assertIn('<span class="current">2026-08-21</span>', nav)
        self.assertNotIn('href="index.html"', nav)   # index is not a day
        self.assertNotIn("notes.html", nav)
        self.assertLess(nav.index("2026-08-20"), nav.index("2026-08-19"))

    def test_todays_page_appears_before_it_has_been_written(self):
        with TemporaryDirectory() as tmp:
            nav = rd.archive_nav(Path(tmp), "2026-08-21")
        self.assertIn('<span class="current">2026-08-21</span>', nav)


class EndToEnd(unittest.TestCase):
    """The 2026-08-20 failure, as a test: the board must not lose the near tier."""

    def _run(self, argv):
        return rd.main(argv)

    def test_near_count_mismatch_stops_the_render(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "cards.json").write_text(json.dumps(
                {"cards": [card(), card(key="k2")]}), encoding="utf-8")
            (tmp / "ledger.json").write_text(json.dumps(
                ledger(k1=record(), k2=record())), encoding="utf-8")
            with self.assertRaises(SystemExit):
                self._run(["--date", "2026-08-21",
                           "--cards", str(tmp / "cards.json"),
                           "--ledger", str(tmp / "ledger.json"),
                           "--template", str(TEMPLATE),
                           "--docs", str(tmp / "docs"),
                           "--expect-near", "1"])
            self.assertFalse((tmp / "docs" / "2026-08-21.html").exists())

    def test_matching_near_count_writes_both_pages(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "cards.json").write_text(json.dumps(
                {"subtitle": "s", "cards": [card()]}), encoding="utf-8")
            (tmp / "ledger.json").write_text(json.dumps(
                ledger(k1=record())), encoding="utf-8")
            self._run(["--date", "2026-08-21",
                       "--cards", str(tmp / "cards.json"),
                       "--ledger", str(tmp / "ledger.json"),
                       "--template", str(TEMPLATE),
                       "--docs", str(tmp / "docs"),
                       "--expect-near", "1"])
            dated = (tmp / "docs" / "2026-08-21.html").read_text(encoding="utf-8")
            index = (tmp / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(dated, index)
        self.assertIn('"key": "k1"', dated)

    def test_no_index_leaves_the_live_page_alone(self):
        # Backfilling an old day must not overwrite today's index.html.
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            docs = tmp / "docs"
            docs.mkdir()
            (docs / "index.html").write_text("TODAY", encoding="utf-8")
            (tmp / "cards.json").write_text(json.dumps(
                {"cards": [card()]}), encoding="utf-8")
            (tmp / "ledger.json").write_text(json.dumps(
                ledger(k1=record())), encoding="utf-8")
            self._run(["--date", "2026-08-13",
                       "--cards", str(tmp / "cards.json"),
                       "--ledger", str(tmp / "ledger.json"),
                       "--template", str(TEMPLATE),
                       "--docs", str(docs), "--no-index"])
            self.assertEqual((docs / "index.html").read_text(encoding="utf-8"),
                             "TODAY")
            self.assertTrue((docs / "2026-08-13.html").exists())


if __name__ == "__main__":
    unittest.main()
