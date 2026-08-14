# Build Playbook — Phases 2 through 7

Phase 1 is done. Alerts are flowing into `rental-alerts` and the clock is
running on data collection.

**Time:** ~2 hours across two sittings. Phases 2–5 can be done tonight
against fixtures. Phase 6 needs 3–4 days of real alerts first — around
**Aug 17**. Phase 7 follows immediately after.

---

## Phase 2 — Scaffold (15 min)

```bash
mkdir -p ~/rental-scan/{reports,raw,fixtures,.claude/agents}
cd ~/rental-scan
```

Copy in: `criteria.md`, `dedupe.py`, `CLAUDE.md`, `fixtures/day1.json`, and
the four files into `.claude/agents/`.

```bash
cat > .gitignore << 'EOF'
*.tmp
raw/
EOF

git init
git add -A
git commit -m "initial: criteria, dedupe, agents"
python dedupe.py --help    # confirm it runs
```

**Why `raw/` is gitignored but `ledger.json` is not.** The raw per-run JSON is
regenerable noise. `ledger.json` is the one artifact you cannot rebuild —
it holds every key ever seen, assembled from alert emails you won't have
access to again. Commit it every run.

---

## Phase 3 — Prove out dedupe (30 min)

Do this before touching agents. If `dedupe.py` is wrong, every downstream
agent inherits the error invisibly.

```bash
# no --dry-run: day 2's transition test needs the ledger written
python dedupe.py --listings fixtures/day1.json --ledger ledger.json
```

### Expected output — verify all five

The fixture contains deliberately nasty cases. You should get:

```
ingested: 6      unique_units: 5      possible_duplicates: 1
```

| Check | Expected | Why it matters |
|---|---|---|
| Zillow + Redfin collapsed | Both become `1547-n-damen-3W`, one record with two entries in `_sources` | Cross-portal dedupe works despite `N.`/`North` and `Unit 3W`/`#3-W` |
| Field merge | That record has `ceiling_height_ft: 13` | Redfin's data filled Zillow's gap — most complete version wins |
| **N vs S separate** | `1547-n-damen-3W` **and** `1547-s-damen-3W` both exist | The Chicago trap. If these collapse, dedupe.py is broken |
| All-in math | `1547-n-damen-3W` → **$3,651.67** | $3,200 + $200 parking + $210 loft heat + $41.67 amortized fee |
| Domu no-unit | Gets fallback key `1547-n-damen-2br-3200`, flagged as possible duplicate | Correct — refuses to guess which unit it is |

That $451 gap between the advertised $3,200 and the real $3,651.67 is the
entire reason §5 exists. (Under the original $3,500 ceiling it was also a hard reject; at the current $3,800 ceiling it stays in scope.)

### Then test state transitions

```bash
cp fixtures/day1.json fixtures/day2.json
# edit day2.json: change the Zillow rent 3200 -> 2950, delete the Craigslist entry
python dedupe.py --listings fixtures/day2.json --ledger ledger.json --dry-run
```

Expect `price_change: 1` with `rent_delta: -250`, and the unchanged records
suppressed as `seen`.

```bash
rm ledger.json    # start the real one clean
```

**Day 1 writes a throwaway ledger; day 2 uses `--dry-run`.** The transition test needs day 1's state on disk, so day 1 runs live and the `rm ledger.json` at the end throws it away. Any further iteration on day-2-style tests should use `--dry-run` against that throwaway ledger.

---

## Phase 4 — Agents, one at a time (45 min)

The four files are written. Two things to handle on import:

**Tool names.** `inbox-harvester.md` lists `mcp__gmail__search_threads` and
`mcp__gmail__get_thread`. Your actual Gmail connector tool names may differ.
Open `/agents` in Claude Code, check the tool picker, and correct the
frontmatter to match what's actually there. Wrong tool names fail silently —
the agent just can't reach Gmail.

**Loading.** Agents created through `/agents` take effect immediately. Files
dropped on disk require a session restart to be picked up. If you copy the
files in manually, restart before testing.

### Test each one alone before building the next

```
> Use inbox-harvester to pull yesterday's rental alerts.
```

Check: did it write `raw/inbox-{date}.json`? Are the records in §6 schema?
Are missing fields `null` rather than invented?

Then `web-scout`, then `dedupe-analyst`, then `reporter`. Four untested
agents chained together produce a failure you cannot localize.

### The design decision worth understanding

Harvesters **write to files and return only counts**. They don't return the
JSON itself.

This is deliberate. Subagents return a summary to the parent, and a large
JSON array passed that way gets truncated or paraphrased — you'd lose
listings silently, with no error. Writing to disk and passing a path keeps
the data intact and the parent's context small.

### 4.5 — Geocoder + map report (see HTML-TOOL-SPEC.md)

Confirmed direction: `mockup.html` (card list + Leaflet map, functional
price/tier filters, move-in date on every listing). Full build details are
in `HTML-TOOL-SPEC.md` — read it before building this piece.

- [ ] Build `.claude/agents/geocoder.md` per spec §1. Test alone against
      `fixtures/day1.json` first: confirm it (a) skips addresses already
      cached in `ledger.json`, (b) respects the 1 req/sec Nominatim limit,
      (c) writes `lat`/`lng` under the correct canonical key
- [ ] Extend `reporter` to also write `docs/index.html` from real classified
      data, per spec §3's field mapping table. Do not change the layout or
      styling that's already confirmed in `mockup.html` — only wire real
      data into it
- [ ] Update `CLAUDE.md`'s run sequence per spec §5: `geocoder` slots in
      after `dedupe-analyst`, before `reporter`
- [ ] Open the generated `docs/index.html` in a browser. Confirm filters
      actually filter and pins land in plausible locations before trusting it

---

## Phase 5 — Orchestrator (10 min)

`CLAUDE.md` is written. The load-bearing instruction is that it delegates
and does nothing itself.

Verify by running a full pass and checking that the parent session never
read an email or fetched a listing page directly. If it did, tighten the
"Your role" section — an orchestrator that starts doing the work fills its
context with listing bodies and the four-agent split stops buying you
anything.

---

## Phase 6 — Validation with real data (Aug 17, ~45 min)

```
> Run the daily scan.
```

### The validation that actually matters

Reading the report tells you what the scan **found**. It tells you nothing
about what it silently **dropped** — and that's the failure that costs you
an apartment.

So: open 3–4 source emails in Gmail yourself. Count the listings in them.
Compare against `raw/inbox-{date}.json`. Every listing in the email should
appear in the JSON, including ones obviously outside your criteria.

If listings are missing, the harvester is filtering when it shouldn't be.
Tighten the "What you do NOT do" section in its prompt.

### Also check

- [ ] `loft_type` calls cite specific §4a signals, not just the verdict
- [ ] `all_in_monthly` shows its assumptions rather than presenting an
      estimate as fact
- [ ] `possible_duplicates` appear in the report rather than being
      auto-resolved
- [ ] Near Misses tier is populated — if it's always empty, the hard filters
      may be silently eating everything
- [ ] `docs/index.html` opens without errors and matches `active.md`'s
      listings
- [ ] Price and tier filters actually filter, not just visually toggle
- [ ] A listing missing a move-in date shows "not listed," never a guess
- [ ] Map pins spot-checked against 2–3 real addresses on Google Maps

Re-run until two consecutive clean passes.

---

## Phase 7 — Schedule (10 min)

1. `claude.ai/code/scheduled` → new cloud task, pointed at the repo
2. Prompt: `Run the daily scan per CLAUDE.md.`
3. **Weekdays, 7:00 AM Central**
4. Phone reminder at 7:15 for the first week — the run has no push
   notification, so you have to go look

**Weekdays before daily.** Five runs gives you enough signal to calibrate
without a weekend of unattended failures. New Chicago rental inventory posts
on weekdays anyway. Move to daily after a clean week.

---

## Week 1 — Calibration

- Check every run for the first five
- After ~20 listings have passed through, revisit the §4 weights. Your
  reactions to real listings will disagree with the weights I guessed at,
  and scoring is only useful once it matches your instinct.
- Resolve `possible_duplicates` manually each time — permanently a human job
- Watch the zero-source canary. One portal going quiet is the most likely
  failure and looks exactly like a slow market.

### Brace for silence

Loft + 1–2BR + under $3,800 all-in is a narrow
intersection. Expect **a handful of listings per month**, not per day. Most
days the scan correctly reports nothing.

If two weeks pass with nothing at all, the lever to pull is the **move-in
window** (§10), not the style criteria. You're rent-free with no pressure —
widening that is close to free.

---

## Still needing your call

From `criteria.md` §10:

- [ ] Move-in window — currently Sept 15 to Dec 31
- [ ] Soft lofts in or out
- [ ] Duplex as hard filter or preference (currently 18-point preference)
- [ ] Pets
- [ ] Secondary zones — keep at −5 penalty, or cut
