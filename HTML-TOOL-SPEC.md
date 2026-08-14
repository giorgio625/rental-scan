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
| `sources` | `_sources`, joined | |
| `available` | `available_date` | pass through as-is, including `null` |

**Regeneration, not append:** `docs/index.html` is fully overwritten each
run from the current `active.md` equivalent (all live listings scoring 50+),
same as `active.md` itself. It is a view of current state, not a running log.

**Do not touch the file structure of `mockup.html` beyond the data-loading
change.** The layout, filters, and styling are confirmed. If `reporter`
wants to change the visual design, that's a separate conversation, not
something to drift into while wiring up real data.

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
