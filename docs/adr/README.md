# Architecture Decision Records

This directory holds **Architecture Decision Records (ADRs)** — short documents
that capture a single significant decision: the context that forced it, the
choice we made, the alternatives we rejected, and the consequences we accept.

We use ADRs because decisions were previously scattered across `CLAUDE.md`, code
comments, GitHub issues, and PR threads, where they are hard to find and easy to
lose. An ADR is the durable, greppable home for *why* the backend is shaped the
way it is. The sibling pipeline repo (`curatedMetagenomicsNextflow`) uses the
same practice.

## When to write one

Write an ADR when a decision:

- is hard or expensive to reverse (e.g. orchestration model, auth posture, the
  job lifecycle), or
- has non-obvious rationale that a future maintainer would otherwise have to
  reconstruct, or
- was contested — there were real alternatives with real trade-offs.

Do **not** write one for routine, easily-reversible changes (a bug fix, a
dependency bump, a rename). Those belong in the commit message.

## How to write one

1. Copy [`template.md`](template.md) to `NNNN-short-title.md`, where `NNNN` is
   the next zero-padded number in sequence.
2. Fill it in. Keep it short — one decision, one page.
3. Set the status (see below).
4. Add a row to the index below.
5. Commit it alongside the change it documents where possible.

ADRs are immutable once `Accepted`. To change a decision, write a **new** ADR
that supersedes the old one, and update both `Status` lines to point at each
other. This preserves the historical record rather than rewriting it.

## Status values

- **Proposed** — under discussion, not yet acted on.
- **Accepted** — the decision is in force.
- **Accepted (not yet implemented)** — agreed, but the code does not exist yet.
- **Superseded by [NNNN](NNNN-...md)** — replaced by a later decision.
- **Deprecated** — no longer relevant, but kept for the record.

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [0000](0000-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0001](0001-pull-mode-orchestration.md) | Pull-mode HPC orchestration | Accepted |
| [0002](0002-run-death-classification.md) | Run death classification | Accepted |
| [0003](0003-dispatchability-detection.md) | Dispatchability detection (pending work with no active daemon) | Accepted |
| [0004](0004-workflow-version-vs-revision.md) | Distinguish workflow version (logical) from revision (operational) | Accepted |
| [0005](0005-single-collection-membership-seam.md) | Single write seam for collection membership | Accepted |
| [0006](0006-cloudflare-control-plane.md) | Replace the FastAPI + Postgres server with a Cloudflare control plane | Accepted (amended 2026-10-07; in part superseded by [0009](0009-catalog-in-postgres-control-plane-in-worker.md)) |
| [0007](0007-readset-identity.md) | Identify processing units as readsets digested with the refget seqcol algorithm | Accepted |
| [0008](0008-object-storage-on-r2.md) | Keep all cloud object storage on Cloudflare R2 | Accepted |
| [0009](0009-catalog-in-postgres-control-plane-in-worker.md) | Keep the catalog in Postgres and the control plane in the Worker | Accepted |
| [0010](0010-registrations-are-bundles.md) | Registrations are bundles | Accepted |
| [0011](0011-results-storage-and-publication.md) | Results storage and publication | Accepted |
