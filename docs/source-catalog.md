# Metadata source catalog

The compendium catalogs metadata **before and independently of processing**.
A source can be useful even if its reads are unavailable or nothing has been
curated yet. The first increment ([#333](https://github.com/seandavi/nextflow_telemetry/issues/333))
records where metadata is maintained, which assets it contains, and which
original files we have retained. It does not interpret sample values.

## Concepts

| Record | Meaning | Example |
|---|---|---|
| Source | A maintained upstream resource with a stable local ID | ZOE's open Zenodo record, a curation repo, a study-tracking sheet |
| Asset | An identifiable file, table or tab within a source | `P3_US21_metadata.tsv.bz2`, sheet tab `gid=0` |
| Snapshot | One immutable observation of an asset's bytes at a named version | The complete compressed ZOE file, a timestamped full-sheet CSV export |
| Descriptor event | An attributed change to our source or asset description | Correcting a locator or recording an access/license review |

**Table legend:** these are catalog records, not operational collections,
processing samples or inferred biological identities. An inventory is an asset
whose role is to point to studies/metadata, rather than contain clinical values.

Source IDs are operator-assigned lowercase slugs. Asset IDs are source-local
slugs. Neither changes when a URL, filename or title changes. An asset locator
may be a repository path/pattern, tab ID or filename; it is descriptive, not an
executable download recipe. Assets can be cataloged before a snapshot exists.
A tar/zip bundle can be a single asset; individual files can also be separate
assets when they have independent identity or access terms.

Descriptors include access (`unknown`, `public`, `restricted`) and data license
(nullable). A source also has review status (`unreviewed`, `reviewed`, `retired`).
**Reviewed means the descriptor was reviewed, not that all its scientific values
are correct.** Unknown license is not permission to redistribute. An asset's
access/license is explicit: it does not inherit a repository's code license.
Access labels are inventory information, not an authorization system.

## Where records and bytes live

- Catalog records and descriptor history: four `metadata_source*` tables in the
  existing **Postgres catalog**, configured through `SQLALCHEMY_URI`.
- Original snapshot bytes: a **private content-addressed archive** chosen with
  `--archive-dir`, stored as `sha256/<digest>` (whole input, no filtering). Production
  uses the private R2 bucket `cmgd-private`: `--archive-dir r2:cmgd-private/metadata-sources`
  (an rclone path, using the host's existing `r2:` remote). A local directory also works
  and is what the tests use.
- Bulk interpreted metadata values: the existing lake `cmgd_meta` loader, unchanged.
  It is **not yet connected** to this inventory. Its `source_releases` records
  loads of interpreted values; source-catalog snapshots record acquired bytes.

The CLI does not create schemas or run migrations. A maintainer applies Alembic
migrations to the intended catalog before use. No defaults point writes at
production. Source commands require an explicit `SQLALCHEMY_URI`; lake settings
and credentials are not consulted. This increment has no HTTP/UI endpoints.

Raw files do not belong in git or Postgres. Back up the archive with the catalog;
a database backup alone does not retain the bytes. This local archive is a
dev/offline adapter. For R2 the object is uploaded, then read back and hashed
before the receipt is written; an existing object at a digest is verified, never
overwritten. Today's `r2:` credentials are account-wide, not scoped to this bucket.
There is no automatic garbage collection or deletion command.

## Workflow

### 1. Describe and review a source

Use one JSON manifest per source, containing `schema_version: 1`, `source` and
`assets`. Working examples: [`examples/source-catalog/`](../examples/source-catalog/).

```bash
# Pure validation: no database, network requests or lake connection.
uv run nf-meta sources validate examples/source-catalog/zoe.json

# Select the catalog DB using the normal PostgreSQL URI environment variable.
# Do not put credentials in manifests or in command arguments.
export SQLALCHEMY_URI='postgresql://...'

# Preview is the default: no writes, schema creation or audit rows.
uv run nf-meta sources apply examples/source-catalog/zoe.json --actor curator

# Explicit write. Exact reapplication is a no-op, including audit/timestamps.
uv run nf-meta sources apply examples/source-catalog/zoe.json --actor curator --commit
```

Descriptor and asset changes commit together with before/after audit events.
Omitting an existing asset from an edited manifest **does not delete it**.
There is no implicit retirement or upstream polling. Edit the source's
`review_status` to retire it; historical captures remain.

### 2. Acquire the original asset, then capture it

Download/export using the upstream's authorized mechanism. This increment does
not fetch arbitrary URLs, manage spreadsheet credentials or execute upstream
code. For sheets, export the complete tab, including rows not currently of
interest. For compressed metadata, retain the original compressed bytes.

```bash
uv run nf-meta sources capture zoe-predict-open p3-us21-metadata \
  --version zenodo-15308000-v1 \
  --file /path/to/P3_US21_metadata.tsv.bz2 \
  --archive-dir r2:cmgd-private/metadata-sources \
  --actor curator                         # preview only

# Same invocation with --commit retains bytes and inserts the receipt.
```

Use an exact upstream commit/record version where available. For a mutable
spreadsheet, use a new timestamped export identifier for each observation,
e.g. `export-2026-10-18T18:30:00Z`. The SHA-256 identifies the actual file;
operator-provided version labels are not independently verified upstream.
Capture time is **our** acquisition time, not an asserted upstream publication date.

The receipt contains source/asset/version, byte count, SHA-256, artifact URI,
actor and timestamp. It freezes the source and asset descriptors too, so later
license/access/locator edits do not rewrite a snapshot's provenance.

- Repeating the same version and bytes is a no-op; the first artifact URI wins.
- Different bytes under an existing version are rejected. Use a new version.
- A new version may reuse an existing content-addressed blob.
- Repeated committed captures check retained bytes for corruption/missing files.
- Existing archive directories must have mode `700`, blobs `600`. New ones
  are private. Do not point at a temporary download directory or shared public tree.
- Placement is atomic and doesn't overwrite blobs. A failed DB write can leave
  an unreferenced blob, but never intentionally a committed receipt without bytes.
  External filesystem deletion/corruption still requires backup/recovery.
- A preview is not a reservation; commit rechecks current catalog state and bytes.

### 3. Inspect the inventory

All commands output JSON. Errors go to stderr with a nonzero exit status.

```bash
uv run nf-meta sources list
uv run nf-meta sources list --access restricted
uv run nf-meta sources list --role inventory
uv run nf-meta sources show zoe-predict-open
uv run nf-meta sources history zoe-predict-open
```

The access filter refers to the **source** descriptor; the role filter matches
any asset belonging to it. `show` includes assets and snapshot receipts, not file
contents. `history` includes actor/time and before/after descriptor values.

## Target schema: pinned cMD dictionary

Harmonized fields aim at the cMD schema. `src/nextflow_telemetry/meta/schema/` holds a
vendored `cMD_data_dictionary.csv` (authority: `waldronlab/curatedMetagenomicDataCuration`
master, `inst/extdata/`), `pin.json` (upstream commit, sha256, and the `cmd-curation`
catalog snapshot at that commit) and `cmd.yaml`, the LinkML schema generated by
`scripts/cmd_schema.py`. Never edit `cmd.yaml` by hand. To upgrade, run
`just cmd-schema-pin <commit>`, capture `cmd-curation` at the same commit, set
`catalog_snapshot`, and review the diff in a PR. `tests/test_cmd_schema.py` checks the
pin, the no-op regeneration, `linkml lint`, the generators, and that every field we emit
is either in the schema or listed in `LOCAL_FIELDS` with a reason. Dynamic enums
(ontology `reachable_from`) are declared but not validated (#341).

## Public examples and restricted sources

The checked-in manifests are examples, not preloaded records or a declaration
that they have been reviewed. cMD's data license is deliberately unknown pending
review. The spreadsheet example is a **placeholder**; it does not publish the
shared Segata tracker URL or private study details. Replace it in a private
working location before use.

Catalogs and descriptor history can themselves contain sensitive URLs, titles,
notes or personal information. Only trusted operators with catalog database
access should use this CLI. Do not add credentials, bearer/signed URLs or secret
locations to descriptors. `--actor` is operator-supplied attribution, not an
authenticated identity. The archive's private permissions don't grant the right
to obtain, retain, or redistribute its contents; establish those rights first.

## Deliberately deferred

Automatic acquisition connectors/polling; study/specimen identity; assertions,
conflict resolution and harmonization; publication joins; mapping existing lake
loads into this catalog; public catalog export; web UI and role-based endpoints.
Nothing here registers samples, creates collections, starts processing, uploads
snapshots or publishes restricted metadata.

## Verification

```bash
uv run pytest tests/test_source_catalog.py
uv run mypy src/nextflow_telemetry
```

Tests use synthetic manifests, local temporary archives and a disposable
Postgres container, not real spreadsheets, upstream services or production state.
