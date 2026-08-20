# Rental Search Criteria — Wicker Park

This file is the single source of truth for the daily scan. All four agents read it.
Edit this file rather than editing agent prompts.

**Last updated:** 2026-08-20 (added §8a harvest manifests, §4b enrichment)

---

## 1. Scope

Rental only. No for-sale listings, no sublets under 6 months, no room shares,
no short-term/furnished corporate rentals.

---

## 2. Geography

### In bounds (primary)
Wicker Park, defined as:

- **North:** Bloomingdale Ave (1800 N)
- **South:** Division St (1200 N)
- **East:** Ashland Ave (1600 W)
- **West:** Western Ave (2400 W)

### In bounds (secondary — include, but tag `adjacent`)
- **Bucktown:** Bloomingdale Ave to Fullerton Ave, Ashland to Western
- **East Village / Ukrainian Village:** Division St to Chicago Ave, Ashland to Western
- **Lincoln Park:** North Ave (1600 N) to Diversey Pkwy (2800 N),
  Lake Michigan east to the Chicago River / Clybourn corridor west

> Lincoln Park has thin loft inventory compared to Wicker Park — mostly vintage
> greystones and newer condo product. What loft stock exists clusters along the
> **Clybourn corridor** and the old industrial pockets near Cortland and
> Southport. Weight the web sweep there accordingly.

### Out of bounds
Everything else, including Logan Square, West Loop, Humboldt Park, and
anything west of Western.

> Listings often mislabel neighborhood. **Trust the address, not the listing's
> neighborhood tag.** Geocode against the boundaries above.

---

## 3. Hard filters — auto-reject, do not report

| Filter | Rule |
|---|---|
| All-in monthly rent | > $3,800 |
| Bedrooms | Studio, or 3BR+ |
| Location | Outside boundaries in §2 |
| **Style** | **Not a loft — see §4a for the qualification test** |
| Availability | Move-in date before 2026-09-15 or after 2026-12-31 *(CONFIRM)* |
| Lease term | Under 12 months |
| Duplicate | Canonical key already in `ledger.json` (see §6) |

**Parking is deliberately NOT a hard filter.** In Wicker Park you can lease a
garage or pad spot separately, usually within a block or two, for $150–250/mo.
Killing an otherwise-perfect loft over parking would be a mistake. It carries
heavy scoring weight instead, and the reporter flags nearby separate-lease
options when a listing has none.

**Outdoor space is NOT a hard filter either** (changed 2026-08-13). Units with
no private outdoor space — or none stated — stay in scope. It carries 12
points of §4 scoring weight, and the HTML report has an outdoor-space filter
to slice the shortlist either way on demand.

**All-in monthly rent** = base rent + parking + mandatory building/amenity fees
+ heat, if not included. Amortize one-time fees (move-in fee, broker fee) over
12 months and include them. See §5 on net-effective rent.

---

## 4. Scoring — 100 points

| Factor | Weight | Notes |
|---|---|---|
| Loft authenticity | 20 | Hard loft = 20; soft loft = 10. See §4a |
| Duplex / two floors | 18 | Duplex-up = 18; two-story = 15; duplex-down = 8 |
| Parking | 14 | Included garage = 14; deeded/paid on site = 9; separate lease nearby = 5; street only = 0 |
| Outdoor space quality | 12 | Private roof deck = 12; large terrace/deck = 10; balcony = 7; shared or none = 0 |
| Transit access | 10 | Walk time to Damen, Division, or Western Blue Line |
| Value vs. comps | 10 | $/sqft against the running median in `ledger.json` |
| In-unit laundry | 8 | In-building shared = 3; none = 0 |
| Condition / build | 5 | Renovated or well-maintained conversion |
| The 606 proximity | 3 | Within 3 blocks of the Bloomingdale Trail |

**Penalties:** garden/basement unit −10. Ground floor on a main arterial
(Milwaukee, Damen, North, Division, Ashland, Western) −5. Lincoln Park or other
secondary zone −5, since Wicker Park is the target.

Report anything scoring 50+. Flag 70+ as priority.

---

## 4a. Loft qualification

"Loft" is used loosely in Chicago listings. Apply this test rather than
trusting the word.

**Hard loft (authentic conversion) — needs 3 of 5:**
- Former industrial, warehouse, or factory building
- Exposed brick or exposed timber structure
- Exposed ductwork, piping, or sprinkler lines
- Ceilings 11 ft or higher
- Timber post-and-beam or mushroom-cap concrete columns

**Soft loft (new construction in loft idiom) — needs 3 of 4:**
- Purpose-built, typically 1990s or later
- Ceilings 10 ft or higher
- Open-plan main living area, minimal interior walls
- Oversized or factory-style windows, concrete or exposed-duct finishes

**Not a loft, reject:** a standard apartment described as "loft-like,"
"lofted ceilings," or containing a sleeping loft/mezzanine in an otherwise
conventional unit. A raised sleeping platform is not a loft conversion.

Populate `loft_signals` in the schema with which specific signals were found,
so a borderline call is auditable rather than a black-box verdict.

**Search terms for the web sweep:** loft, timber loft, authentic loft, hard
loft, concrete loft, warehouse conversion, factory conversion, post and beam,
exposed brick duplex.

## 4b. Enrichment — closing the §4a evidence gap

Added 2026-08-20. `enrich_select.py` implements the selection; the
`enricher` agent does the fetching. Runs after `geocoder`, before
`reporter`.

**The problem this solves.** §4a is a test against evidence, and alert
emails carry almost none of it. They give price, beds, and an address; they
do not say whether there is exposed brick or how high the ceilings are. So
every listing arrived with `loft_signals: null`, failed the 3-of-5 test by
mechanical default, and the scan reported nothing for a week — 297 records
ingested, 19 with any loft signal at all. The harvesters were right to
refuse to invent evidence. The gap was that nothing ever went and looked at
the listing page.

**The rule stays the same.** Enrichment does not relax §4a; it feeds it. A
field the page does not state stays `null`, exactly as in §6.

### What gets fetched

Fetching costs time and hits other people's servers, so there is a per-run
budget — 20 pages by default — spent on the listings where the answer could
actually change the outcome. The number is sized to observed volume: a busy
day produces roughly 15–25 eligible candidates, so 20 keeps the deferred
queue from growing while leaving the cap meaningful on an unusual day.
Anything over budget lands in `deferred` and comes up first next run.
`enrich_select.py` picks the set deterministically. An agent choosing
twenty records out of a hundred is a set operation, and those drift
invisibly; the same reasoning that keeps `dedupe.py` deterministic applies
here.

A listing is eligible when it has a URL, sits on a non-excluded host, has
1–2 beds, is at or under the all-in ceiling, is in bounds **by its cached
coordinates rather than its claimed neighborhood**, has not already been
fetched, and does not already have §4a *settled* — 3 or more of the 5 hard
criteria already confirmed. A listing with 1 or 2 confirmed signals is not
skipped: it is exactly the case where one more page could decide the
outcome, so it stays eligible until the test is actually resolved either
way.

Candidates are then ranked by **score ceiling** — the highest §4 score the
listing could still reach if every unknown field came back at its best.
Known fields count what they earn; unknown fields count their maximum,
because an unknown field is upside and upside is the reason to look.
Anything that cannot reach 50 even at its best case is dropped: it could
not be reported no matter what the page said. Ties break toward Wicker
Park, then toward cheaper.

**No host is excluded from fetching** (decided 2026-08-20). The selector
keeps an `EXCLUDED_HOSTS` tuple and it is deliberately empty.

It briefly held Zillow and Redfin, generalised from a line in the sibling
condo project about its provider-adapter ingest layer. The cost showed up
immediately: 10 of 23 eligible candidates in a single day, including **all
three Wicker Park listings** — the primary target zone. One of them,
`2048-w-evergreen-1`, already had exposed brick and 12 ft ceilings on
record and needed one more signal to clear §4a. Refusing to open the page
that could settle it, for the best candidate in the target neighbourhood,
is not caution — it is the search failing at its own purpose.

The standing instruction is that every listing should reach the report for
personal review. `web-scout` already fetches listing pages routinely, so
nothing here is a new kind of access. The mechanism stays: a host that
should be left alone for some other reason goes in the tuple and the
selector stops offering it.

**In practice the question turned out to be moot.** With the tuple emptied,
all five Zillow and Redfin candidates returned a clean HTTP 403 — including
two attempts at `2048-w-evergreen-1`. Both sites decline automated reads at
the door, so the debate about whether we *should* fetch them was settled by
their servers rather than by us. Do not try to work around it. The
practical consequences are worth stating plainly:

- **Portal-only listings cannot be enriched, ever.** A listing whose only
  URL is Zillow or Redfin stays at whatever the alert email carried. It is
  recorded `blocked`, retried once after 14 days, and otherwise stands on
  email data alone.
- **They are still reported.** `blocked` is not a rejection — the listing
  flows to the report as normal, and the reporter says the page could not
  be read rather than implying the features are absent.
- **A blocked listing sitting near the §4a bar is a manual job.** Opening
  one URL in a browser is thirty seconds of human time and is the only way
  those get resolved.

### Where it is cached

`enrichment.json`, keyed by canonical key, one entry per unit ever — same
economics as the geocoder's coordinate cache, and for the same reason: a
building's bones do not change.

**Not in `ledger.json`.** `dedupe.py` rewrites every ledger record each run
and preserves only `verdict`, `lat`, and `lng` from the prior version, so
any other field written there is silently erased on the next scan. The
separation also keeps a regenerable cache out of the one irreplaceable
file: losing `enrichment.json` costs re-fetching, losing `ledger.json`
costs the search.

A failed fetch is recorded too, with `fetch_status` of `blocked`,
`not_found`, or `error`. Otherwise the selector would retry the same dead
page every day forever. Failures are retried once after 14 days.

### Evidence, not verdicts

The enricher records signals and **quotes the page's own wording for each
one**. It never sets `loft_type` — the reporter applies §4a. A signal
without a quote is not auditable, and §4a exists precisely so a borderline
loft call can be inspected rather than taken on faith.

The enricher also fills §4 scoring fields the emails routinely miss
(`layout`, `outdoor_space`, `laundry`, `parking_type`, `unit_level`) and
two §5 trap fields: `heat_included`, and any mandatory monthly amenity or
utility package. That second one is a genuine gap — a "$95 utility package"
is a mandatory fee under §3's all-in definition, and `dedupe.py` never saw
it because the email never mentioned it. It is recorded in
`mandatory_fees_monthly`; the reporter surfaces it as an adjustment to the
all-in figure rather than silently restating a number now known to be low.

---

### Expected volume — read this before you get frustrated

Loft + 1–2BR + under $3,800 all-in, with outdoor space, duplex, and parking
weighted heavily on top, is a genuinely narrow intersection. In Wicker
Park that is likely a **handful of listings per month**, not per day. Most days
the scan will correctly report nothing.

That is why §9 includes a near-miss tier. A silent scan and a broken scan look
identical from the outside, so the near-miss tier doubles as proof of life.

---

## 5. Chicago-specific traps to encode

These are the things that make a listing look cheaper than it is. The reporter
agent must call each one out explicitly when present.

- **Heat not included.** Common in vintage walk-ups. Budget $120–200/mo
  November–March. If the listing doesn't say, mark `heat_included: null` and
  flag it as an open question rather than assuming.
- **Net effective rent.** "One month free" spread across a 12-month lease makes
  the advertised number ~8% below the actual monthly. Always capture and report
  **gross rent**, with net effective as a secondary figure.
- **Move-in fee vs. security deposit.** Most Chicago landlords use a
  non-refundable move-in fee ($300–700) instead of a deposit. Capture it.
- **Broker fee.** Some listings carry a fee of up to one month's rent. Capture it.
- **Parking is usually a separate lease.** Wicker Park is permit-zone street
  parking. A garage or tandem spot typically runs $150–275/mo on top of rent.
- **Vintage stock.** Pre-war 2-flats and 3-flats often have no in-unit laundry,
  no dishwasher, and no central air. Don't infer these amenities from silence.
- **Bait listings.** Craigslist and some Zillow FSBO posts in WP are bait.
  Any listing more than 20% below the running median for its bed count gets
  tagged `suspect_pricing` rather than scored as a great deal.
- **"Loft" as a marketing word.** The single most common mislabel in this
  search. A conventional 1BR with a sleeping mezzanine gets listed as a "loft"
  constantly. Run the §4a test every time.
- **Ceiling height claims.** Listings quote the highest point in the unit, not
  the typical height. Where photos are available, sanity-check against window
  and door proportions before crediting 11 ft.
- **"Duplex" is ambiguous in Chicago.** Locally it usually means a single unit
  spanning two floors, but it sometimes means a two-flat building. Confirm from
  the floor plan or photos which is meant, and distinguish **duplex-up**
  (main floor plus upper) from **duplex-down** (main floor plus lower level).
  Duplex-down units are typically half below grade — scored much lower.
- **Balcony vs. "outdoor space."** Shared roof access, a common courtyard, and
  a fire escape all get described as outdoor space. Only private, unit-exclusive
  space earns §4 outdoor points. Mark shared-only as `shared` — it scores 0,
  same as none.
- **Loft heating and cooling.** High ceilings and single-pane factory windows
  make winter heat bills materially higher than the neighborhood norm. If heat
  is tenant-paid in a hard loft, budget toward the top of the $120–200/mo range
  or above, and say so in the report.

---

## 6. Listing schema

Both harvester agents return a JSON array of objects in exactly this shape.
Missing data is `null`. **Never infer, never guess.**

**Exception: `lat`/`lng`.** Harvesters always leave these `null`. They're
populated downstream by the `geocoder` step, cached in `ledger.json` against
the canonical key — see HTML-TOOL-SPEC.md §1.

```json
{
  "source": "zillow | redfin | apartments | domu | craigslist | web",
  "url": "string",
  "first_seen": "YYYY-MM-DD",
  "address_raw": "string, as printed in the listing",
  "street_number": "string",
  "street_directional": "n | s | e | w | null",
  "street_name": "string",
  "unit": "string | null",
  "neighborhood_claimed": "string | null",
  "beds": 1,
  "baths": 1.0,
  "sqft": null,
  "rent_gross": 2800,
  "rent_net_effective": null,
  "concession": "string | null",
  "parking_cost": null,
  "parking_type": "included_garage | paid_garage | paid_lot | street | null",
  "heat_included": null,
  "move_in_fee": null,
  "broker_fee": null,
  "laundry": "in_unit | in_building | none | null",
  "dishwasher": null,
  "central_air": null,
  "loft_type": "hard | soft | not_loft | null",
  "loft_signals": ["exposed_brick", "timber_beams", "high_ceilings"],
  "ceiling_height_ft": null,
  "layout": "single_floor | duplex_up | duplex_down | two_story | null",
  "outdoor_space": "private_roof | private_terrace | balcony | shared | none | null",
  "outdoor_space_sqft": null,
  "lat": null,
  "lng": null,
  "pets": "cats | dogs | both | none | null",
  "available_date": "YYYY-MM-DD | null",
  "lease_term_months": null,
  "unit_level": "garden | ground | upper | null"
}
```

---

## 6a. Listing URLs — normalize the tracking wrappers

The `url` field has to still work when I tap it a week later, on a phone,
not signed in. Alert emails do not give you that by default.

**Zillow.** Alert emails wrap listing links two different ways, sometimes
nested one inside the other:

```
https://www.zillow.com/routed/email/property-notifications/zpid_target/464607265_zpid
https://www.zillow.com/routing/email/property-notifications/zpid_target/2102308204_zpid
https://click.mail.zillow.com/f/a/<token>/AAAAARA~/<token>?target=<urlencoded real url>
```

Both spellings (`routed`, `routing`) appear. The `click.mail.zillow.com`
form carries the real URL urlencoded in its `target=` query param — unwrap
that first, then apply the rule below to what comes out.

These are single-use tracking wrappers tied to the email that carried them —
they expire, and an expired one silently lands on a generic Zillow page
rather than erroring, so a dead link looks exactly like a working one until
you tap it.

The ZPID in the path is the stable identifier. Rewrite to the canonical
permalink:

```
464607265_zpid   →   https://www.zillow.com/homedetails/464607265_zpid/
```

**Any source.** Rewrite `http://` to `https://`. Strip tracking query
parameters (`utm_*`, `rgid`, `mid`, `s_trk`, `fromEmail`, `signature`) —
they carry nothing needed to resolve the listing and some encode the
recipient.

**What NOT to do:** if an email only gives a building-level or floorplan URL
(`zillow.com/apartments/chicago-il/the-ludlow`, a RentCafe `default.aspx`, a
property's own `/floorplans/` page), keep it as-is. It is the real link the
source published. Do not synthesize a unit-level URL that was never given —
a fabricated link that 404s is worse than an honest building page.

---

## 7. Canonical key and address normalization

`dedupe.py` implements this. Spec lives here.

**Primary key:** `{street_number}-{directional}-{normalized_street}-{unit}`

Normalization rules:

1. Lowercase everything.
2. **Keep the directional, normalize its form.** `North` → `n`, `W.` → `w`.
   This is non-negotiable in Chicago — 1600 N Damen and 1600 S Damen are four
   miles apart.
3. Strip street suffixes: `st`, `street`, `ave`, `avenue`, `blvd`, `dr`, `pl`,
   `ct`, `pkwy`, `ter`.
4. Strip all punctuation and collapse whitespace to single hyphens.
5. Unit: strip `unit`, `apt`, `apartment`, `#`, `suite`. Uppercase what remains.
   `Unit 2R` → `2R`. `#3-N` → `3N`.

Example: `1547 N. Damen Ave, Unit 3W` → `1547-n-damen-3W`

**Fallback key** when unit is null:
`{street_number}-{directional}-{normalized_street}-{beds}br-{rent rounded to nearest 50}`

**Death is measured in calendar days, not runs.** `DAYS_ABSENT_UNTIL_DEAD`
in `dedupe.py` is compared against `last_seen`, so a listing dies 7 days after
the last run that saw it no matter how often the scan runs. The cadence only
decides which run notices — on an every-other-day schedule that's the run at
day 8, not day 14.

**Ambiguity rule:** if two records share a building key, rents are within $50,
and at least one record is missing its unit number, do **not** auto-merge.
Emit both under a single `possible_duplicate` group and let me resolve it.
(Two records with the same known unit merge cleanly by primary key; two
different known units are different apartments — neither case is ambiguous.)

**Status transitions:**

| Condition | Classification |
|---|---|
| Key not in ledger | `new` |
| Key in ledger, rent changed | `price_change` — report the delta |
| Key in ledger, unchanged | `seen` — suppress from report |
| Key absent 7+ days | `dead` — mark, stop reporting |
| Key returns after `dead` 30+ days | `relist` — not `new` |

---

## 8. Sources

**Email (via Gmail label `rental-alerts`):**
Zillow, Redfin, Apartments.com, Domu, Craigslist (Save Search email
alerts — Craigslist retired RSS, so this runs through the same
sender-domain filter as the other four)

**Web sweep (`web-scout` agent):**
- Wicker Park / Bucktown management companies that don't syndicate
- New lease-ups along Milwaukee Ave and the 606 corridor
- @properties, Compass, Dream Town rental pages

---

## 8a. Harvest manifests — the handoff contract

Added 2026-08-20, after a run silently discarded an entire 111-record web
sweep. The harvester wrote its file under a name the next step didn't look
for; the next step found nothing, re-ran a thinner sweep, and the run
completed looking perfectly normal. Nothing anywhere reported a problem.

Two failures made that possible, and this section closes both:

1. The filename was **constructed by convention** at both ends, so the two
   ends could disagree.
2. Nothing compared **what was written** against **what was read**.

**Every harvester writes a manifest alongside its data file:**

```
raw/manifest-inbox-{YYYY-MM-DD}.json     (inbox-harvester)
raw/manifest-web-{YYYY-MM-DD}.json       (web-scout)
```

Separate files per agent — the two harvesters run concurrently and must
never write to the same path.

```json
{
  "agent": "inbox-harvester",
  "run_date": "2026-08-20",
  "file": "raw/inbox-2026-08-20.json",
  "count": 45,
  "sources_present": ["zillow", "redfin", "craigslist"],
  "sources_zero": ["apartments", "domu"]
}
```

- `file` — the path actually written, relative to the repo root. **This is
  the authoritative location of the data.** Downstream reads this value; it
  never rebuilds the path from a template. A harvester that names its file
  something unexpected is then harmless, because the manifest points at it.
- `count` — the number of records in that array, counted after writing.
- `sources_zero` — sources that normally appear and returned nothing this
  run. This makes the §9 zero-source canary machine-readable instead of
  surviving only as prose in a return message.

**`dedupe-analyst` verifies before it merges:**

| Condition | Action |
|---|---|
| Manifest present, file exists, `count` == array length | Merge normally |
| Manifest absent | That harvester failed. Proceed with the other, report the gap. **First check `raw/` for any same-date file it may have written before its manifest step** — report what you find; never discard it silently |
| Manifest present, `file` missing on disk | **Blocking anomaly.** Report and stop |
| `count` != array length | **Blocking anomaly.** Report both numbers and stop. This is the truncation signature |

A blocking anomaly stops the run *before* `dedupe.py` touches the ledger.
That ordering is the point: a stopped run costs a day and is trivially
re-run, while a run that merges a truncated harvest writes a wrong
`last_seen` for everything it didn't see, and the reaper then kills live
listings seven days later with nothing indicating why.

---

## 9. Report format

`reports/{YYYY-MM-DD}.md`

1. **Header** — count of new, price changes, near-misses, possible duplicates
2. **Priority (70+)** — full detail, all-in cost breakdown, the §5 traps flagged,
   loft signals found, and nearby separate-lease parking if the unit has none
3. **Worth a look (50–69)** — one line each: address, beds, all-in, score, link
4. **Near misses** — listings failing exactly one hard filter, capped at 10 per
   run, each labeled with which filter it failed. Rent over ceiling by less than
   10% is the classic case.
5. **Price changes** — address, old → new, days on market
6. **Possible duplicates** — needing my resolution
7. **Sources that returned zero** — canary for a broken parser. Built from
   the `sources_zero` arrays in the §8a manifests, not from memory
8. **Harvest anomalies** — any §8a manifest that was absent, pointed at a
   missing file, or disagreed with its file's record count. Omit the section
   entirely when all manifests verified clean; a run that degraded silently
   is the failure this whole pipeline is least able to notice on its own

If zero new, zero price changes, and zero near misses: write nothing, exit
silently. **Exception:** if the scan has been silent for 7 consecutive days,
write a one-line heartbeat confirming the sources are still returning data.

`active.md` is overwritten each run with every live listing scoring 50+,
sorted by score, with my own status column preserved across runs.

### State vs. diff — the two outputs are deliberately different

`reports/{date}.md` is a **diff**: what changed since the last run. New,
price changes, relists, near misses. It stays that way — a cumulative digest
is not skimmable, which is the only thing a daily digest is for.

`active.md`, `shortlist.json`, and `docs/index.html` are **state**: every
listing that is *currently available* and scores 50+, however long ago it was
found. A listing discovered three runs back that is still on the market and
still scores 50+ appears in all three on every run until it dies. Rebuild
them from `dedupe.py`'s `live_keys` — the full set of non-dead ledger keys —
intersected with the scored shortlist. Never from the run's `new` bucket, and
never by subtracting `newly_dead`, which only fires on the single run a
listing dies and silently strands it if that run is missed.

`docs/{date}.html` is the exception on the state side: it's the permanent
HTML twin of that day's digest, so it keeps that day's near-misses and
rejects even after they age out.

These three run on every scan, including a silent one. Only
`reports/{date}.md` is conditional on there being something to report.

**Invariant, check it before finishing:** the number of `tier: 'near'`
entries in `docs/{date}.html` equals the number of near misses in
`reports/{date}.md`. The two are the same list rendered twice. On
2026-08-20 the markdown carried one near miss and the HTML carried none —
the dashboard was empty on a day the report was not, and nothing caught it.
A silent run is `0` on both sides; `0` on one side alone is a bug.

---

## 10. Open items to confirm

- [ ] Move-in window — currently 2026-09-15 to 2026-12-31. Widening this is the
      single biggest lever on inventory volume given how narrow the style
      criteria are.
- [ ] **Soft lofts** — currently in scope at half the loft score. Cut them and
      go hard-loft-only? That roughly halves an already thin pool.
- [ ] **Duplex** — currently an 18-point preference, not a hard filter. Promote
      to hard filter? Would likely take the scan to near-zero when combined
      with the outdoor space requirement.
- [ ] Pets — no filter set. Add if needed.
- [ ] Garden/basement units — currently penalized −10, not excluded
- [ ] Secondary zones (Bucktown, East Village, Lincoln Park) — in scope, tagged,
      −5 score penalty. Keep or cut?
