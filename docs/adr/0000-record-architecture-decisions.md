# 0000. Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** Sean Davis

## Context

The telemetry/orchestration backend has accumulated significant, non-obvious
decisions — why orchestration is pull-mode, why the `/telemetry` weblog endpoint
is unauthenticated, why SQLAlchemy Core is used instead of an ORM, how run
death is classified. Until now these lived in `CLAUDE.md`, code comments, GitHub
issues, and PR threads. That makes them hard to find, easy to lose when an issue
is closed, and impossible to grep. Contributors (human or agent) repeatedly
re-derive or unknowingly contradict earlier reasoning.

The sibling pipeline repo (`curatedMetagenomicsNextflow`) already records
decisions this way; aligning the two keeps a single mental model across the
system.

## Decision

We will record architecturally significant decisions as **Architecture Decision
Records (ADRs)** stored in `docs/adr/`, using a lightweight Nygard-style format
(context / decision / alternatives / consequences). The process and index live
in `docs/adr/README.md`. ADRs are immutable once accepted; a changed decision is
captured by a new ADR that supersedes the old one.

## Alternatives considered

- **Keep decisions in `CLAUDE.md` / issues / PRs** — the status quo. Rejected:
  `CLAUDE.md` is operational guidance, not a decision log; issues and PRs are not
  discoverable or versioned with the code, and are lost when closed or moved.
- **A single `DECISIONS.md` log** — simpler, but grows into an unsearchable wall
  of text with no stable per-decision anchor to link to.
- **A heavier ADR tool** — more structure than this project needs; the format
  can grow later if warranted.

## Consequences

- Each substantive decision gets a durable, linkable home that travels with the
  code and shows up in `git blame`/`grep`.
- Small added ceremony, scoped to decisions that are hard to reverse or
  non-obvious.
- The initial set is seeded from recent decisions (0001–0003); other existing
  decisions (unauthenticated weblog, Core-over-ORM, the job lifecycle) can be
  backfilled as they are touched.

## References

- Michael Nygard, "Documenting Architecture Decisions" (2011).
- `docs/adr/README.md` for the process and index.
- The sibling practice in `curatedMetagenomicsNextflow/docs/adr/`.
