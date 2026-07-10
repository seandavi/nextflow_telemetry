# Single write seam for collection membership; `metadata.cohort` retired

## Context

"Which collection is this sample in?" was written three ways — the accession
path wrote `collections` + `collection_samples`, `POST /samples` wrote a scalar
`metadata.cohort`, and curated import wrote its own island — and read two ways
(the Cohorts dashboard read `collections`, the Samples page read
`metadata.cohort`). The two surfaces therefore disagreed: a sample was visible
to one and invisible to the other depending on how it was registered.

## Decision

Collection membership has **one write seam**: `add_to_collection(conn, …)` in
`services/collection.py`. Every registration path (accession `_register`, single
`POST /samples`, curated later) routes through it, and every reader reads the
`collection_samples` join. `metadata.cohort` is retired as a membership encoding
— backfilled into collections, then stripped from the sample row. A collection
is identified by an exact-string natural key (its name / the accession); there
is no surrogate id.

## Notes

- The seam is a plain `conn`-taking async function, **not** a
  Service-with-its-own-engine (the repo's usual shape), so that "insert sample +
  insert membership" stays inside the caller's single transaction. This is a
  deliberate deviation from the `services/*.py` pattern — atomicity over
  uniformity.
- `metadata.cohort` is dropped, not kept as a derived cache: a Sample row is
  identity only (see `CONTEXT.md`). Descriptive attributes move to a separate
  many-to-one table later.
- Membership is many-to-many and purely operational (completion monitoring), so
  Samples-page collection chips are overlap-allowed, not a partition.
