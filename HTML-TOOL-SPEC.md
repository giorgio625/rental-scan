# Spec Addendum — Map/HTML Report

Adds a visual, filterable Zillow-style report alongside the existing markdown
digest. Confirmed direction: `mockup.html` in this folder (card list + map,
warm/brick palette, functional price, tier, and outdoor-space filters, move-in
date shown per listing, honest "not listed" state when a date is missing).

This is an ADDITION to the existing pipeline, not a replacement. The
markdown report and `active.md` keep working as-is.

---

## 1. New pipeline step: `geocoder`

Insert between `dedupe-analyst` and `reporter` in `CLAUDE.md`'s daily run.

**New agent file:** `.claude/agents/geocoder.md`

**Job:** for every listing in `classified-{date}.json` lacking cached
coordinates, geocode `address_raw` via Nominatim (OpenStreetMap), cache the
result in `ledger.json` against the listing's canonical key, and never
re-geocode an address already cached.

**Why caching in the ledger, not a separate file:** the canonical key from
`dedupe.py` is already the stable identifier for a unit across its lifetime.
Storing `lat`/`lng` on that same ledger record means geocoding happens once
per unit ever, not once per run.

**Rate limit — non-negotiable:** Nominatim's usage policy requires max 1
request/second and a descriptive `User-Agent` header identifying the
application. On a normal run, only new listings need geocoding (everything
else is cached), so this is rarely more than a few seconds of added runtime.
If a burst of new listings ever makes this slow, that's a sign the cache
isn't being hit — check the ledger lookup before assuming Nominatim is slow.

**Tools:** Read, Write, WebFetch (Nominatim's endpoint:
`https://nominatim.openstreetmap.org/search?q={address}&format=json`)

**Failure handling:** if an address fails to geocode (bad match, network
error), set `lat`/`lng` to `null` on that record and continue — don't block
the run. The `reporter` step below handles null coordinates by omitting the
listing from the map while still including it in the card list.

---

## 2. Extend `criteria.md` §6 schema

Add two fields to the listing JSON schema:

```json
"lat": null,
"lng": null
```

Populated by `geocoder`, not by the harvesters — `inbox-harvester` and
`web-scout` should leave these `null` on their raw output.

---

## 3. Extend `reporter` agent's job

`reporter.md` gets one new responsibility: alongside `reports/{date}.md`,
also write `docs/index.html` using `mockup.html` as the confirmed template.

**Data transform needed:** the mockup's `listings` array is currently
hardcoded JS. The real version needs this array generated from
`classified-{date}.json`, mapped to the mockup's field names:

| Mockup field | Source field | Notes |
|---|---|---|
| `key` | `_key` (canonical key) | **Required.** Saved/removed state is stored against it in `localStorage`. `id` is a render ordinal that gets reassigned every run — keying off it would silently re-point a saved listing at a different apartment |
| `tier` | derived from `score` | `priority` ≥70, `worth` 50–69, `near` = failed exactly one §3 filter |
| `score` | computed score, §4 | |
| `address` | `address_raw` | |
| `zone` | derived from geography match, §2 | |
| `rent` | `rent_gross` | |
| `allIn` | `all_in_monthly` | from `dedupe.py` |
| `lat` / `lng` | from ledger, via `geocoder` | omit marker if null |
| `outdoor` | `outdoor_space` | `private_roof`/`private_terrace`/`balcony` → `private`; `shared`/`none`/`null` → `other` |
| `loft` | `loft_type`, mapped to "Hard loft"/"Soft loft" | |
| `layout` | `layout` | |
| `tags` | derived from `parking_type`, `laundry`, `outdoor_space` | |
| `warn` | `cost_assumptions` + `suspect_pricing` + near-miss reason | |
| `signals` | `loft_signals`, joined | |
| `sources` | `_sources` | array of {label: source name, url: listing url}, rendered as links on card and popup |
| `available` | `available_date` | pass through as-is, including `null` |

**Regeneration, not append — but two outputs now.** `docs/index.html` is
fully overwritten each run from the current `active.md` equivalent (all live
listings scoring 50+ plus today's near-misses): a view of current state, not
a running log. `docs/{YYYY-MM-DD}.html` is the opposite — a permanent
per-day snapshot, the HTML twin of that day's `reports/{date}.md`, written
once and never overwritten by a later run. Both carry the same archive nav
strip linking every `docs/YYYY-MM-DD.html` on disk, newest first, so either
page can reach any day.

### Rendering is a script, not a transcription — added 2026-08-21

`reporter` no longer writes HTML. It writes `raw/dashboard-{date}.json` and
`render_dashboard.py` turns that into both pages.

The split: **`reporter` judges, the script renders.** Tier, score, zone, the
loft call, signals, warnings, tags and the subtitle are §4/§4a judgments and
only `reporter` makes them. Everything mechanical — joining `lat`/`lng`,
`beds`/`baths`/`sqft` and source links from `ledger.json` by canonical key,
building the past-7-days set, normalizing URLs per §6a, the archive nav —
belongs to the script, which decides nothing.

This exists because both dashboard failures to date were transcription
errors, not judgment errors:

- **2026-08-20** — both pages rendered with zero listings of any tier while
  `reports/2026-08-20.md` correctly carried a near miss. The run reported
  success.
- **2026-08-21** — every source link on the board was `href="null"`. Five
  dead links that look live until tapped, which is the exact failure mode
  §6a warns about.

A 700-line hand-copy every day will keep producing those. A 90-line JSON
file will not, and it is countable, diffable, and cheap to validate.

```
python render_dashboard.py --date 2026-08-21 --expect-near 5
```

`--expect-near` is the 2026-08-20 invariant made mechanical: pass the near-miss
count from `reports/{date}.md` and a disagreement is a hard failure that writes
nothing, rather than a blank board that reports success. `--no-index` renders
only the dated page, for backfilling an old day without disturbing the live one.

**`raw/dashboard-{date}.json`:**

```json
{
  "run_date": "2026-08-21",
  "subtitle": "…the header line under the brand…",
  "cards": [
    {"key": "…", "tier": "priority|worth|near", "score": 0,
     "address": "…", "zone": "…", "rent": 0, "allIn": 0,
     "loft": "…", "layout": "…", "tags": [], "warn": "…", "signals": "…",
     "available": null, "outdoor": "private|other"}
  ]
}
```

`key` and a valid `tier` are required — a card missing either is a hard
failure, because saved/removed state is stored against the canonical key.
Omit `beds`/`baths`/`sqft`/`lat`/`lng`/`sources` and the script joins them
from the ledger; state one and yours wins, which is how §4b enrichment
corrections reach the page.

### Beds, baths, and the past-7-days pane — added 2026-08-21

**Bed/bath/sqft line on every card and every week row.** Each part is
independently optional: §6 has sources emit `null` for "not stated", so an
unstated bath count renders "baths not listed" rather than a confident blank.
`beds: 0` is a studio, not a missing value — a truthiness test here turns
every studio into "not listed". `baths` and `sqft` were absent from every
ledger record until 2026-08-21 despite being on the §7a sticky list; the
`dedupe.py` that wrote them predated the widened field list, and
`migrations/2026-08-21-url-and-baths-backfill.py` recovered them from the
archived `raw/classified-*.json` files.

**Past 7 days pane**, a tab in the left pane beside Board. Every canonical
key whose `first_seen` falls in the trailing 7 days, straight from
`ledger.json`, grouped by capture date, newest day first — scored and
unscored, still live and already dead, with pins on the map.

Deliberately unfiltered. The board answers "what should I look at"; this
answers "what did the scan actually see this week", and narrowing it to
qualifiers would answer the board's question twice. It is also the fastest
way to tell a genuinely quiet week from a broken harvester — a day with
nothing under it is a visible hole rather than an absence you infer from an
empty board.

**Do not touch the file structure of `mockup.html` beyond the data-loading
change.** The layout, filters, and styling are confirmed. If `reporter`
wants to change the visual design, that's a separate conversation, not
something to drift into while wiring up real data.

### Saved / removed listings, and the mobile view toggle

Added 2026-08-18, in the template — `reporter` inherits all of it by
replacing only the `listings` array, and must not reimplement any of it:

- **★ save and ✕ remove** on every card, persisted in `localStorage` under
  `wpscan.favorites.v1` / `wpscan.dismissed.v1`, keyed on the **canonical
  key**. Removed listings hide behind a "*n* removed — Show / Restore all"
  bar; nothing is ever destroyed, because a listing removed by accident on a
  phone has to be recoverable. A `★ Saved (n)` pill filters to favourites.
- **List ↔ Map toggle**, a floating control that appears under 860px so the
  map is usable on a phone, where the two-pane desktop layout collapses to
  one pane at a time. It is fixed-position rather than in the header because
  the header lives inside the list pane, which is hidden in map view.
- Because state is same-origin `localStorage`, saves and removals carry
  across `index.html` and every archived `docs/{date}.html` for free.

This is why `key` is required in the field table above. A generated page
that omits it falls back to the address string, which works but breaks the
moment `reporter` rewords an address.

---

## 4. Extend `PLAYBOOK.md`

Add to **Phase 4**, after the four existing agents are built and tested:

> **4.5 — Build and test `geocoder`.** Same one-at-a-time discipline as the
> other agents: test it alone against `fixtures/day1.json` before wiring it
> into the full pipeline. Confirm it (a) skips already-cached addresses,
> (b) respects the 1 req/sec limit, (c) writes `lat`/`lng` back to
> `ledger.json` under the correct canonical key.
>
> Then confirm `reporter` produces a valid `docs/index.html` from real
> classified data — open it in a browser and check that filters work and
> pins land in roughly the right places before trusting it.

Add to **Phase 6** validation checklist:

- [ ] `docs/index.html` opens and renders without errors
- [ ] Price and tier filters actually filter (not just visually toggle)
- [ ] A listing with no move-in date shows "not listed," not a blank or a
      guessed date
- [ ] Map pins match card addresses — spot-check 2–3 against Google Maps

---

## 5. Extend `CLAUDE.md`

Update the daily run sequence:

```
1. inbox-harvester + web-scout (parallel)
2. dedupe-analyst
3. geocoder          <- new
4. reporter          <- now also writes docs/index.html
5. git commit
```

`geocoder` must run after `dedupe-analyst` (needs canonical keys) and before
`reporter` (needs coordinates to build the map).

---

## Open decision for you, not Claude Code

**Hosting `docs/index.html`.** As a local file it opens fine from Claude
Code Desktop or a file browser. If you want it reachable from your phone
without opening the desktop app, that's a separate step — e.g., GitHub Pages
if the repo is pushed there, or just relying on Claude Code Desktop's mobile
remote access. Not required to build; only relevant once this is working and
you decide how you want to check it day to day.
