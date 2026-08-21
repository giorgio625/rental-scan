#!/usr/bin/env python3
"""Render the two HTML dashboards from data. Deterministic, stdlib only.

Why this exists
---------------
`reporter` used to hand-write ~700 lines of HTML every run by copying
`mockup.html` and swapping the data array. Two failures came out of that:

  2026-08-20  both dashboards rendered with zero listings of any tier while
              `reports/2026-08-20.md` correctly carried a near miss. The run
              reported success.
  2026-08-21  every source link on the board was `href="null"` -- five dead
              links that looked live until tapped.

Neither is a judgment error; both are transcription errors in a mechanical
step. So the mechanical step moves here and the judgment stays with
`reporter`, which now emits `raw/dashboard-{date}.json` -- a small, countable
file that is hard to silently truncate to nothing and trivial to check.

Split of responsibility
-----------------------
`reporter` decides: tier, score, zone, loft call, signals, warnings, tags,
the subtitle. Those are section 4/4a judgments and nothing here second-guesses
them.

This script decides nothing. It joins `lat`/`lng`/`beds`/`baths`/`sqft` and
source links from `ledger.json` by canonical key, builds the past-7-days set
from `first_seen`, normalizes URLs per section 6a, and writes the files. Where
the reporter states a value the ledger also has, the reporter wins --
enrichment corrects the harvest, per section 4b.
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
WEEK_DAYS = 7
TIERS = ("priority", "worth", "near")

# Display names for the `source` values harvesters emit. An unknown source is
# title-cased rather than dropped: a new source appearing is information, and
# silently rendering it as nothing is how you fail to notice one arrived.
SOURCE_LABELS = {
    "zillow": "Zillow", "redfin": "Redfin", "domu": "Domu",
    "craigslist": "Craigslist", "apartments": "Apartments.com",
    "web": "Web", "rentcafe": "RentCafe",
}

# Tracking params section 6a says to strip. `rgid`/`mid`/`s_trk`/`signature`
# encode the recipient; none are needed to resolve a listing.
TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z_]+|rgid|mid|s_trk|fromEmail|signature|cid|eid|rtoken)$", re.I)

ZPID_WRAPPER = re.compile(
    r"zillow\.com/rout(?:ed|ing)/email/property-notifications/zpid_target/(\d+)_zpid",
    re.I)


# --------------------------------------------------------------------------
# Section 6a -- URL normalization
# --------------------------------------------------------------------------

def normalize_url(url):
    """Apply section 6a. Returns None for anything that is not a usable link.

    Run here as well as in the harvesters on purpose. A tracking wrapper that
    slips through does not error -- it lands on a generic Zillow page, so a
    dead link and a live one look identical right up until you tap it. This is
    the last point before the link reaches a phone, which makes it the right
    place for a belt-and-braces pass. Two unnormalized `routing/email`
    wrappers were sitting in the ledger when this was written.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None

    # click.mail.zillow.com/...?target=<urlencoded real url> -- unwrap first,
    # then run the result through the rest of the rules.
    if "click.mail.zillow.com" in url.lower():
        target = parse_qs(urlsplit(url).query).get("target")
        if target and target[0]:
            return normalize_url(target[0])
        return None                      # wrapper with nothing inside it

    match = ZPID_WRAPPER.search(url)
    if match:
        return "https://www.zillow.com/homedetails/%s_zpid/" % match.group(1)

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    kept = [pair for pair in parts.query.split("&")
            if pair and not TRACKING_PARAMS.match(pair.split("=", 1)[0])]
    return urlunsplit(("https", parts.netloc, parts.path,
                       "&".join(kept), parts.fragment))


def source_entries(record):
    """`sources` from a ledger record as [{label, url}], deduped, order kept.

    A source whose URL does not survive section 6a is KEPT with `url: None`.
    The template renders that as plain text saying there is no link, which is
    the honest state -- dropping the row instead would hide that the listing
    was seen at all.
    """
    seen, out = set(), []
    for source in record.get("sources") or []:
        name = (source.get("source") or "").lower()
        label = SOURCE_LABELS.get(name, name.title() or "Source")
        url = normalize_url(source.get("url"))
        if (label, url) in seen:
            continue
        seen.add((label, url))
        out.append({"label": label, "url": url})
    return out


# --------------------------------------------------------------------------
# Data assembly
# --------------------------------------------------------------------------

def _pick(card, record, field):
    """Reporter's value if it stated one, else the ledger's."""
    value = card.get(field)
    return record.get(field) if value is None else value


def build_cards(payload, ledger):
    """Join the reporter's judged cards onto ledger facts."""
    cards, missing = [], []
    for index, card in enumerate(payload.get("cards") or [], start=1):
        key = card.get("key")
        if not key:
            raise SystemExit(
                "card %d has no `key`. Saved/removed state is stored against "
                "the canonical key; a card without one silently re-points a "
                "favourite at a different apartment every run." % index)
        if card.get("tier") not in TIERS:
            raise SystemExit("card %s has tier %r, expected one of %s"
                             % (key, card.get("tier"), ", ".join(TIERS)))
        record = ledger["listings"].get(key)
        if record is None:
            missing.append(key)
            record = {}

        sources = card.get("sources")
        if sources:
            sources = [{"label": s.get("label"),
                        "url": normalize_url(s.get("url"))} for s in sources]
        else:
            sources = source_entries(record)

        rent = card.get("rent")
        all_in = card.get("allIn")
        cards.append({
            "id": index,
            "key": key,
            "tier": card["tier"],
            "score": card.get("score", 0),
            "address": card.get("address") or record.get("address_raw") or key,
            "zone": card.get("zone") or "Zone not resolved",
            "rent": record.get("rent_gross") if rent is None else rent,
            "allIn": record.get("all_in_monthly") if all_in is None else all_in,
            "beds": _pick(card, record, "beds"),
            "baths": _pick(card, record, "baths"),
            "sqft": _pick(card, record, "sqft"),
            "lat": record.get("lat"),
            "lng": record.get("lng"),
            "loft": card.get("loft") or "Loft status not assessed",
            "layout": card.get("layout") or "Layout not listed",
            "tags": card.get("tags") or [],
            "warn": card.get("warn") or "",
            "signals": card.get("signals") or "",
            "sources": sources,
            "available": card.get("available"),
            "outdoor": card.get("outdoor") or "other",
        })
    return cards, missing


def build_weekly(ledger, run_day, board_keys):
    """Every key first seen in the trailing 7 days, newest day first.

    Deliberately not filtered by score, tier, or status. The board answers
    "what should I look at"; this answers "what did the scan actually see",
    and a week that comes back thin is worth being able to see plainly rather
    than inferring from an empty board.
    """
    start = run_day - timedelta(days=WEEK_DAYS - 1)
    rows = []
    for key, record in ledger["listings"].items():
        first_seen = _parse_day(record.get("first_seen"))
        if first_seen is None or not (start <= first_seen <= run_day):
            continue
        rows.append({
            "key": key,
            "date": record["first_seen"],
            "status": record.get("status") or "active",
            "onBoard": key in board_keys,
            "address": record.get("address_raw") or key,
            "unit": record.get("unit"),
            "rent": record.get("rent_gross"),
            "allIn": record.get("all_in_monthly"),
            "beds": record.get("beds"),
            "baths": record.get("baths"),
            "sqft": record.get("sqft"),
            "lat": record.get("lat"),
            "lng": record.get("lng"),
            "sources": source_entries(record),
        })
    rows.sort(key=lambda r: (r["date"], -(r["rent"] or 0), r["key"]),
              reverse=True)
    return rows, start


def _parse_day(value):
    if not value:
        return None
    try:
        return date(*(int(part) for part in str(value).split("-")))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Template injection
# --------------------------------------------------------------------------

def _escape(value):
    if isinstance(value, str):
        return (value.replace("<", "&lt;").replace(">", "&gt;")
                     .replace('"', "&quot;"))
    if isinstance(value, list):
        return [_escape(item) for item in value]
    if isinstance(value, dict):
        return {key: _escape(item) for key, item in value.items()}
    return value


def _js(value):
    """JSON safe to paste into a <script> block and into innerHTML.

    `<` becomes a \\u003c escape so a stray closing script tag in listing text
    cannot end the block early, and `<`/`>`/`"` inside strings are entity-
    escaped because the template interpolates these values through innerHTML.
    """
    dumped = json.dumps(_escape(value), indent=2, ensure_ascii=False)
    return dumped.replace("<", "\\u003c")


def _replace_array(html, name, payload):
    pattern = re.compile(r"const %s = \[.*?\n\];\n" % re.escape(name), re.S)
    replacement = "const %s = %s;\n" % (name, payload)
    html, count = pattern.subn(lambda _: replacement, html, count=1)
    if count != 1:
        raise SystemExit(
            "template anchor `const %s = [` not found in the template. The "
            "template changed shape; fix the anchor, not the data." % name)
    return html


def _replace_once(html, pattern, replacement, what):
    html, count = re.subn(pattern, lambda _: replacement, html, count=1,
                          flags=re.S)
    if count != 1:
        raise SystemExit("template anchor for %s not found" % what)
    return html


def archive_nav(docs_dir, current):
    """Nav strip over every dated dashboard on disk, newest first."""
    dates = sorted((path.stem for path in docs_dir.glob("*.html")
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem)),
                   reverse=True)
    if current not in dates:
        dates = sorted(dates + [current], reverse=True)
    links = []
    for day in dates:
        links.append('      <span class="current">%s</span>' % day
                     if day == current
                     else '      <a href="%s.html">%s</a>' % (day, day))
    return ('    <div class="archive-nav" id="archiveNav">\n%s\n    </div>'
            % "\n".join(links))


def render(template, cards, weekly, window, subtitle, nav):
    html = _replace_array(template, "listings", _js(cards))
    html = _replace_array(html, "weekly", _js(weekly))
    html = _replace_once(html, r"const weekWindow = \{.*?\};",
                         "const weekWindow = %s;" % _js(window), "weekWindow")
    html = _replace_once(html, r'<div class="subtitle">.*?</div>',
                         '<div class="subtitle">%s</div>' % _escape(subtitle),
                         "the subtitle")
    html = _replace_once(html,
                         r'    <div class="archive-nav" id="archiveNav">.*?</div>',
                         nav, "the archive nav")
    return html


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the HTML dashboards.")
    parser.add_argument("--date", required=True, help="run date, YYYY-MM-DD")
    parser.add_argument("--cards", help="default raw/dashboard-{date}.json")
    parser.add_argument("--ledger", default=str(ROOT / "ledger.json"))
    parser.add_argument("--template", default=str(ROOT / "mockup.html"))
    parser.add_argument("--docs", default=str(ROOT / "docs"))
    parser.add_argument("--expect-near", type=int, default=None,
                        help="near-miss count from reports/{date}.md; the two "
                             "are the same list rendered twice and a mismatch "
                             "is a hard failure")
    parser.add_argument("--no-index", action="store_true",
                        help="write only the dated page (backfilling an old day)")
    args = parser.parse_args(argv)

    run_day = _parse_day(args.date)
    if run_day is None:
        raise SystemExit("--date must be YYYY-MM-DD, got %r" % args.date)

    cards_path = (Path(args.cards) if args.cards
                  else ROOT / "raw" / ("dashboard-%s.json" % args.date))
    if not cards_path.exists():
        raise SystemExit("no card file at %s -- reporter writes it" % cards_path)

    payload = json.loads(cards_path.read_text(encoding="utf-8"))
    ledger = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
    ledger.setdefault("listings", {})
    template = Path(args.template).read_text(encoding="utf-8")

    cards, missing = build_cards(payload, ledger)
    near = sum(1 for card in cards if card["tier"] == "near")
    if args.expect_near is not None and near != args.expect_near:
        raise SystemExit(
            "near-tier mismatch: %d card(s) here, %d in the markdown report. "
            "They are the same list rendered twice; the board silently losing "
            "the near tier is exactly the 2026-08-20 failure."
            % (near, args.expect_near))

    board_keys = {c["key"] for c in cards if c["tier"] in ("priority", "worth")}
    weekly, start = build_weekly(ledger, run_day, board_keys)
    window = {"start": start.isoformat(), "end": args.date}

    docs = Path(args.docs)
    docs.mkdir(exist_ok=True)
    html = render(template, cards, weekly, window,
                  payload.get("subtitle", "Live results"),
                  archive_nav(docs, args.date))

    (docs / ("%s.html" % args.date)).write_text(html, encoding="utf-8")
    written = ["%s.html" % args.date]
    if not args.no_index:
        (docs / "index.html").write_text(html, encoding="utf-8")
        written.append("index.html")

    nolink = sum(1 for c in cards for s in c["sources"] if not s["url"])
    print("wrote %s in %s" % (", ".join(written), docs))
    print("  cards   %d (%d priority, %d worth, %d near)"
          % (len(cards),
             sum(1 for c in cards if c["tier"] == "priority"),
             sum(1 for c in cards if c["tier"] == "worth"), near))
    print("  week    %d first seen %s..%s"
          % (len(weekly), window["start"], window["end"]))
    if nolink:
        print("  NOTE    %d card source(s) carry no link -- rendered as text, "
              "not as a dead <a>. Check the harvest if this is not normally "
              "zero." % nolink)
    for key in missing:
        print("  WARN    card key %r is not in the ledger -- no coords, no "
              "beds/baths" % key, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
