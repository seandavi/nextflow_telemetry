---
name: adr-author
description: >
  Write a new Architecture Decision Record in the cmgd / telemetry repos,
  following the established Nygard format and conventions. Use when a non-trivial,
  behavior-affecting decision is made (retry policy, storage layout, profile
  semantics, framework choice) and should be recorded, or the user says "write an
  ADR" / "record this decision". Trigger words: ADR, architecture decision,
  record decision, document the decision, why did we choose, design rationale.
---

# Authoring an ADR

Both repos keep ADRs in `docs/adr/` with a `template.md` and a `README.md` index.
Match the existing convention exactly — read the latest few ADRs first.

## Conventions

- **Filename:** `NNNN-kebab-title.md`, zero-padded sequential number. Next number
  = highest existing + 1 (`ls docs/adr/`). cmgd is at 0011; telemetry at 0003.
- **Format (Nygard):** sections `Context` → `Decision` → `Alternatives considered`
  → `Consequences`, preceded by a metadata block:
  ```markdown
  # NNNN. <Title in sentence case>

  - **Status:** Accepted   (or: Proposed | Superseded by [NNNN](...))
  - **Date:** YYYY-MM-DD
  - **Deciders:** Sean Davis
  ```
- **Immutable.** Never rewrite an accepted ADR to reflect a new decision. Instead
  write a NEW ADR and mark the old one `Superseded by [NNNN](...)`, and have the
  new one say `supersedes [MMMM](...)` in its Status line. (See cmgd 0010
  superseding the error-action choice in 0009.)
- **Index:** add a line to `docs/adr/README.md`.

## When an ADR is warranted

Record decisions that change runtime behavior or are expensive to reverse and
non-obvious from the code: retry/error policy, storage/publish layout, profile
semantics, container strategy, choosing/declining a framework. Do NOT ADR routine
bug fixes, version bumps, or anything self-evident from the diff.

## Content guidance

- **Context** = the forces and the triggering experience, concretely (e.g. "the
  RGI image that failed to pull made Nextflow terminate the whole run → a wave of
  ABORTED siblings"). Real incidents beat abstractions.
- **Decision** = what we will do, with the actual config/code snippet.
- **Alternatives considered** = each rejected option AND why — this is the part
  future-you actually rereads.
- **Consequences** = what gets better, what new cost we accept.

## Procedure

1. `ls docs/adr/` → pick next number; read the 2-3 latest for tone.
2. Copy `docs/adr/template.md` → `docs/adr/NNNN-<title>.md`, fill it in.
3. If it supersedes one, edit the old ADR's Status to `Superseded by [NNNN](...)`.
4. Add the index line to `docs/adr/README.md`.
5. Commit with the related code change (or alone if purely a decision).

For pipeline behavior ADRs that ship in a release, cross-reference: the CHANGELOG
entry cites the ADR number (see `cmgd-release`).
