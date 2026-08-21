# Wicker Park Rental Scan — Orchestrator

Daily scan for loft rentals in Wicker Park, with Bucktown, East Village, and
Lincoln Park as secondary zones. Full criteria in `criteria.md`.

## Your role

You delegate. You do not extract, score, judge, or match.

Specifically: do not read alert emails yourself, do not fetch listing pages
yourself, do not compare addresses yourself. Fetching listing pages belongs
to `enricher` for exactly the same reason reading emails belongs to
`inbox-harvester`: page bodies are large and would crowd out everything
else in this window. Every one of those belongs to a
subagent with its own context window. If you start reading emails directly,
your context fills with hundreds of listing bodies and the architecture stops
working — which is the whole reason it's split into five subagents.

## Daily run

1. **In parallel**, spawn:
   - `inbox-harvester` — Gmail `rental-alerts` label
   - `web-scout` — non-syndicating sources

   Spawn both in a single message so they run concurrently. They write to
   `raw/` and return only file paths and counts.

2. Then `dedupe-analyst` — verifies the §8a harvest manifests, merges the
   raw files, runs `dedupe.py`, updates `ledger.json`. If it reports a
   blocking manifest anomaly it will have stopped before touching the
   ledger: do not work around it, do not re-run a harvester to paper over
   it, and do not continue to steps 3–5. Report it and stop.

3. Then `geocoder` — fills cached `lat`/`lng` in `ledger.json` for any new
   canonical keys (Nominatim, 1 req/sec, cache-first).

4. Then `enricher` — runs `enrich_select.py` to pick a budget-capped set of
   candidates, fetches those listing pages, and records the §4a loft
   evidence the alert emails never carry into `enrichment.json`. A run where
   every fetch comes back `blocked` means this stage stopped working, not
   that the listings lack loft features — say which.

5. Then `reporter` — applies §4a and §4, writes `reports/{date}.md`,
   `active.md`, and the two `docs/*.html` dashboards.

6. Commit: `git add -A && git commit -m "scan {date}"`

7. Push: `git push origin master`. Best-effort — if it fails (auth expired,
   network, conflict), do not fail the run or block anything above. Report
   the push failure plainly in what you report back (see below); the commit
   already happened locally, so no data is lost, but `docs/index.html` on
   GitHub Pages goes stale until the next successful push. If pushes have
   been failing for multiple consecutive runs, say so explicitly rather than
   repeating the same one-line failure note each day — a run of failures is
   the thing worth noticing, not any single one.

`ledger.json` is the only irreplaceable artifact here. Weeks of alert emails
cannot be re-collected, so commit every run — a bad run then costs a
`git checkout` instead of a rebuild from nothing.

## Sequencing

Steps 2–5 are strictly serial and depend on the prior step's output file.
Only step 1 parallelizes. Step 7 depends on step 6 (nothing to push without
a commit) but its failure never rolls back or blocks step 6.

`enricher` is the one step that is safe to skip on a bad day: it only adds
evidence, so a failed enrichment run costs detail in the report, never
correctness. Never skip step 2 or 3 to save time.

If a harvester fails, continue with whatever the other returned and note the
gap in the report. A partial scan is useful; a skipped day is a hole in the
ledger.

The one exception is a §8a manifest **mismatch** — a manifest whose `file`
is missing, or whose `count` disagrees with the file it names. That is not a
partial scan, it is an unreliable one, and `dedupe-analyst` stops the run
rather than writing a `last_seen` it can't stand behind. A missing manifest
is the ordinary failed-harvester case and does not stop anything.

## What you report back

Only what needs a human: the report path, headline counts, any
`possible_duplicates` awaiting a call, any source that returned zero when
it normally returns something, any §8a manifest anomaly, and any `git push`
failure (step 7).

Do not summarize the listings. That's the report's job, and restating it
here just means it gets read twice.

## Files

```
criteria.md          the spec — everything reads this, edit it not the agents
dedupe.py            deterministic matching, §7
enrich_select.py     deterministic fetch-candidate selection, §4b
ledger.json          persistent state, every key ever seen
enrichment.json      cached listing-page evidence, §4b — a cache, not state
active.md            rolling shortlist, my status column is preserved
raw/                 per-run intermediate JSON
reports/             daily reports
tests/               regression suite for dedupe.py — stdlib unittest
migrations/          one-time ledger rewrites, kept for provenance
```

Run the tests after touching `dedupe.py` or `enrich_select.py`:

```
python -m unittest discover -s tests
```

`unittest` rather than pytest because both scripts advertise "stdlib only"
and the scan runs in a sandbox with no outbound network — a suite needing
`pip install` first is a suite that does not run where it matters.

## Standing constraints

- Never edit `dedupe.py` or `enrich_select.py` to work around an error.
  Report it and stop. If either is changed deliberately, `python -m unittest
  discover -s tests` must pass before the run continues — the suite exists
  because these two files are the deterministic layer everything downstream
  trusts without re-checking.
- Never write enrichment data into `ledger.json`. `dedupe.py` rewrites every
  ledger record each run and preserves only `verdict`, `lat`, and `lng`, so
  anything else put there is erased on the next scan.
- Never let an agent resolve `possible_duplicates` — those are mine.
- `criteria.md` is the single source of truth. If behavior needs to change,
  change `criteria.md`, not an agent prompt.
