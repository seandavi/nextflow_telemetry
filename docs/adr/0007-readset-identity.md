# 0007. Identify processing units as readsets digested with the refget seqcol algorithm

- **Status:** Accepted (not yet implemented)
- **Date:** 2026-10-03
- **Deciders:** Sean Davis

## Context

The pipeline processes a set of sequencing runs, not a biological sample. Today
that set is keyed by `sample_id` = md5 of the sorted, deduplicated,
semicolon-joined run accessions (`nf_client/srr.py`, the server's `utils`, and
`cf/src/control-do.ts`, pinned to each other by golden-value tests). Outputs are
published under `<publish_base>/cmgd_nextflow/<version>/<sample_id>/`.

Forces:

- Every sample will be re-run, so existing ids and output paths need not survive.
- Ids will be cited in a manuscript, so their derivation must be a documented,
  reproducible algorithm rather than "md5 of a semicolon-joined string".
- Inputs will extend beyond INSDC to other read collections. Read data is
  assumed to be staged in cloud storage; the access layer (possibly DRS-like) is
  undecided.
- A separate metadata repo (discovery, harmonization) will compute the same ids
  without calling this service, so the id must be derivable from the run list alone.
- A BioSample is a poor key: local data has none, a BioSample's run list grows
  over time, it can mix amplicon and shotgun runs, curators sometimes select a
  subset of runs, and some cMD studies have no INSDC runs.

## Decision

We will key jobs and outputs by a **readset id**, computed with the
[refget Sequence Collections v1.0.0](https://ga4gh.github.io/refget/seqcols/)
encoding algorithm over the following schema.

**Readset object** (level 2):

```json
{ "units": ["insdc.sra:ERR478958", "insdc.sra:ERR478959"] }
```

- `units` is the only inherent attribute. It is a set: deduplicated and sorted
  by Unicode code point before digesting, because run order carries no meaning.
- Non-inherent attributes (file checksums, layout, read counts, storage
  locations) may be added later. Per seqcol, they never change the id.

**Unit identifiers** are CURIEs:

- INSDC runs: `insdc.sra:<run accession>`, matching `^insdc\.sra:[SED]RR\d+$`.
  `insdc.sra` is the identifiers.org prefix for SRA objects; only run accessions
  are valid units.
- Other sources: reserved. They will be defined with the storage-access decision
  and will be content-derived, never path-derived.

**Encoding**, following seqcol steps 1–5 with `sha512t24u` (SHA-512, first 24
bytes, base64url) and RFC 8785 canonical JSON:

```python
level1 = {"units": sha512t24u(jcs(sorted(set(units))))}
readset_id = "RS." + sha512t24u(jcs(level1))
```

The `RS.` prefix follows refget's `SQ.` convention. It makes the id
self-describing and keeps a base64url digest that starts with `-` from being
parsed as a command-line flag.

**Golden vectors.** Every implementation must reproduce these. The reference
code also reproduces the seqcol spec's published example digests
(`g04lKdxiYtG3dOGeUC5AdKEifw65G0Wp`, `sjNNwm4zov3Dl0FRWbRTcZwzqrTQKIqL`).

| Units (input order as given) | `units` attribute digest | Readset id |
|---|---|---|
| `insdc.sra:SRR000001` | `YT21j4gzsIn8wOjEyeLa3MLDtgDplqBj` | `RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF` |
| `insdc.sra:` + ERR478958, ERR478959, ERR478960, ERR478961, ERR480454, ERR480455, ERR480456, ERR480457 | `dPkNQbShezkLh6trclk7YEYbYPskOOfP` | `RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj` |
| same eight, shuffled, ERR478958 twice | `dPkNQbShezkLh6trclk7YEYbYPskOOfP` | `RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj` |
| `insdc.sra:SRR1`, `insdc.sra:ERR2`, `insdc.sra:DRR3` | `SiHrRpm6pYqQOnfGN-6yg_ZssJWLjjGP` | `RS.uFRI93utvCk3llTO2fQPqARuz-e6Lj_-` |

**What the id does not carry.** The biological sample (BioSample or local
label), the rule that selected these runs for it, and any curated metadata are
links held by the metadata repo. New runs for a BioSample produce a new
readset; outputs for the old readset remain correct for exactly the runs they
used.

## Alternatives considered

- **Keep the md5 scheme and namespace new sources** (bare INSDC accessions,
  prefixed others). No rehash and no path changes. Rejected: everything is
  being re-run anyway, and the derivation is ad hoc, unversioned and hard to
  state precisely in a methods section.
- **BioSample accession as the key.** Readable and easy to join. Rejected for
  the reasons under Context: it identifies a biological sample, not the input
  that was processed.
- **Minted ids from a registry.** Opaque surrogate keys with a uniqueness
  constraint. Rejected: needs a single authority, so the metadata repo could not
  derive ids offline, and re-submissions are not idempotent by construction.
- **Digest of the read bytes as the readset id.** Rejected: the data must be
  downloaded before it can be named, SRA and ENA serve different bytes for the
  same run, and recompression or re-staging changes the digest. Byte checksums
  are kept as non-inherent attributes for integrity and duplicate detection.
- **Seqcol with `units` as an ordered, collated array.** Rejected: order is not
  meaningful for a set of runs and would make identical inputs differ.

## Consequences

- Clean cutover. `sample_id` becomes the readset id everywhere: v2 control
  plane, `nf-client`, the pipeline input TSV, the GCS output path
  (`.../cmgd_nextflow/<version>/RS.<digest>/`), the outputs catalog and the
  metadata repo. The v1 server is not ported; v2 replaces it ([0006](0006-cloudflare-control-plane.md)).
  The `srr.py` md5 helpers and their golden tests are deleted.
- Ids are case-sensitive and contain `-` and `_`. They are safe in GCS/R2 keys,
  SLURM job names and shells; avoid case-insensitive filesystems for output
  trees.
- A methods section can describe the id in one sentence: a refget Sequence
  Collections digest over the set of INSDC run accessions, with a schema whose
  only inherent attribute is `units`.
- Old md5 ids map deterministically to readset ids from their stored run
  lists, if a comparison with earlier runs is ever needed.
- Follow-up decisions: unit identifiers for non-INSDC data and the cloud
  storage access layer (DRS-like or not), recorded together in a later ADR.

## Conforming implementations

- metacurator SPEC 170 (`src/metacurator/readset.py`) exists and reproduces the
  same golden vectors.
- The nextflow_telemetry implementation (`nf-client`, `cf/`, the pipeline input
  TSV) is Phase 1 of #195. This ADR moves to plain "Accepted" when that lands.

## References

- refget Sequence Collections v1.0.0 — https://ga4gh.github.io/refget/seqcols/
  (encoding steps 1–5; footnote F3 defines `sha512t24u`)
- RFC 8785, JSON Canonicalization Scheme — https://www.rfc-editor.org/rfc/rfc8785
- identifiers.org `insdc.sra` — pattern `^[SED]R[APRSXZ]\d+$`
- Current scheme: `packages/nf_client/src/nf_client/srr.py`, `cf/src/control-do.ts`
- `docs/sample-metadata-design.md`, `docs/study-sample-version-identity.md`
