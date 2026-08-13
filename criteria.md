# Rental Search Criteria — Wicker Park

This file is the single source of truth for the daily scan. All four agents read it.
Edit this file rather than editing agent prompts.

**Last updated:** 2026-08-12

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
| All-in monthly rent | > $3,500 |
| Bedrooms | Studio, or 3BR+ |
| Location | Outside boundaries in §2 |
| **Style** | **Not a loft — see §4a for the qualification test** |
| **Outdoor space** | **No private balcony, deck, patio, or terrace** |
| Availability | Move-in date before 2026-09-15 or after 2026-12-31 *(CONFIRM)* |
| Lease term | Under 12 months |
| Duplicate | Canonical key already in `ledger.json` (see §6) |

**Parking is deliberately NOT a hard filter.** In Wicker Park you can lease a
garage or pad spot separately, usually within a block or two, for $150–250/mo.
Killing an otherwise-perfect loft over parking would be a mistake. It carries
heavy scoring weight instead, and the reporter flags nearby separate-lease
options when a listing has none.

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
| Outdoor space quality | 12 | Private roof deck = 12; large terrace/deck = 10; balcony = 7 |
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

### Expected volume — read this before you get frustrated

Loft + private outdoor space + 1–2BR + under $3,500 all-in, with duplex and
parking weighted heavily on top, is a genuinely narrow intersection. In Wicker
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
  space satisfies the §3 hard filter. Mark shared-only as `shared` and reject.
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
| Key absent 7+ consecutive days | `dead` — mark, stop reporting |
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

## 9. Report format

`reports/{YYYY-MM-DD}.md`

1. **Header** — count of new, price changes, near-misses, possible duplicates
2. **Priority (70+)** — full detail, all-in cost breakdown, the §5 traps flagged,
   loft signals found, and nearby separate-lease parking if the unit has none
3. **Worth a look (50–69)** — one line each: address, beds, all-in, score, link
4. **Near misses** — listings failing exactly one hard filter, capped at 5 per
   run, each labeled with which filter it failed. Rent over ceiling by less than
   10%, or a strong hard loft with no private outdoor space, belongs here rather
   than in the void.
5. **Price changes** — address, old → new, days on market
6. **Possible duplicates** — needing my resolution
7. **Sources that returned zero** — canary for a broken parser

If zero new, zero price changes, and zero near misses: write nothing, exit
silently. **Exception:** if the scan has been silent for 7 consecutive days,
write a one-line heartbeat confirming the sources are still returning data.

`active.md` is overwritten each run with every live listing scoring 50+,
sorted by score, with my own status column preserved across runs.

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
