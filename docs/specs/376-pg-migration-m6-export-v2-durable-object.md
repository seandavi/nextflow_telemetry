# Spec #376: PG migration M6, export v2 Durable Object state and import it into Postgres

Issue: [#376](https://github.com/seandavi/nextflow_telemetry/issues/376). Parent:
[#381](https://github.com/seandavi/nextflow_telemetry/issues/381), step M6. Blocked by M1
(#371, merged as `fa0b1c2d`). Runs alongside M3 (#373) and M4 (#374). Feeds M7 (#377,
staging rehearsal) and M8 (#378, cutover). ADR-0013 decision 8: "jobs and runs are
migrated, not reprocessed".

## Goal

1. **Export.** `POST /admin/export` on the v2 Worker. It writes every ControlDO table to R2
   as gzipped NDJSON under `exports/<export_id>/`, along with a `manifest.json`. The
   manifest records the R2 event cursor at the moment of the snapshot, and that cursor is
   where M5's ingester starts. One call gives one consistent snapshot.
2. **Import.** `python -m nextflow_telemetry.v2_import DIR` loads a local copy of an export
   into the M1 schema in a single transaction. It keeps ids and timestamps, fills
   `readset_runs`, derives each open run's deadline, writes `ingest_cursor`, and resets
   the sequences.
3. **Verify.** The same command then prints a JSON verification report and exits 0 only
   if the report is clean. The report covers:
   - counts by collection, by workflow and status, and by run status;
   - every `readset_id` and `sample_id` recomputed from `ncbi_accession`;
   - every workflow's scope and params;
   - a full-row comparison of every table.
4. **Repeatable.** `--replace` re-imports into the same scratch or staging database, so the
   whole loop can be rehearsed as often as needed.

Production data checked while writing this spec (read-only GETs and the 2026-10-30 daily
snapshot):
- 72,159 samples, 76,031 jobs and 945 runs.
- Recomputing gives 0 `sample_id` and 0 `readset_id` mismatches, and no run accession
  belongs to more than one readset.
- No terminal run lacks `completed_at`. Every job satisfies `ck_jobs_run_name`,
  `ck_jobs_completed_at` and `ck_jobs_failed_at`.
- Every timestamp has the form `YYYY-MM-DDTHH:MM:SS.sssZ`.
- `sqlite_sequence` is readable inside a Durable Object (checked under miniflare).
- Max job id is 123,188 against 17,218 rows (2026-10-30 snapshot), so sequences have to
  be set from the high-water mark, not from the row count.

## Non-goals

- No ingester, no sweep and no new control endpoints (those are M5, M4 and M3). This step
  only writes the `ingest_cursor` row that M5 reads.
- No deploy, either of the Worker or of the service. No production migration, and no
  production export from the implement stage. See "Done-when and the production
  rehearsal".
- No change to the daily cron snapshot (`ControlDO.snapshot()`, `snapshots/<day>.json.gz`).
- No `studies`, `study_aliases`, `curation_decisions` or submission provenance data (v2
  has none). `readset_runs` structure columns (`biosample`, `sra_sample`, `experiment`,
  `bioproject`) stay null; #384 fills them.
- No new Python dependency, no new env var, and no Alembic migration.
- No change to `nf-client`, `etl/v2.py`, the frontend, `health_report.py` or the `contract/`
  suite. `/admin/export` is v2-only and goes away with ControlDO in M10.

## Departures and decisions

1. **Deadlines are derived, not read from RunDO alarms.** v2 keeps deadlines as alarms on
   one RunDO per run, outside ControlDO. Reading them would mean a call per run outside the
   single consistent call. The import derives them instead, from the run row and the
   v2 settings recorded in the manifest. The rules are in "Deadline derivation".
   - Claim and backstop deadlines come out the same as v2's.
   - Liveness uses `last_heartbeat_at`. ControlDO writes it on every heartbeat
     (`applyRunEvent`), at the same moment RunDO re-arms, so it also matches.
2. **`job_counts` is exported but not imported** (M1 Departure 3: Postgres has no counter
   table). The report uses it only for a `v2_job_counts_drift` note, which doesn't affect
   `clean`.
3. **SinkDO `flush` is serialized, and its object keys get a monotonic suffix.** This is
   needed for an exact cursor:
   - Today, an alarm flush and an explicit flush can run concurrently and write the same
     rows twice.
   - The random 8-hex suffix means two objects flushed in the same second sort in random
     order, so "everything after key K" can miss one.
   - After this change, `flush` runs inside `ctx.blockConcurrencyWhile`, and the suffix is
     a per-SinkDO counter (8 hex digits). Keys keep the shape
     `telemetry/events/dt=YYYY-MM-DD/HHMMSS-xxxxxxxx.ndjson.gz` that `EventArchive`
     globs, and they now sort in write order.
4. **The export lives under `exports/<export_id>/`, not `snapshots/`.** The daily snapshot
   stays as it is. `exports/` is added to `R2_PREFIXES`, so `POST /admin/reset` (staging
   only) clears it.
5. **The import commits, then verifies on a fresh connection.** The report describes what
   is actually stored. A failed verification exits 1 and leaves the data in place, so it
   can be inspected. Re-run with `--replace` after a fix.
6. **The report adds a full-row comparison** to the checks the issue lists. Counts alone
   would not catch a column mapped to the wrong place or a shifted timestamp.
7. **`readset_runs` is filled from `samples.ncbi_accession`** at import (M1 spec,
   Non-goals). Samples whose `readset_id` is null (non-run entries) get no rows.

## Design

### Files

| File | Change |
|---|---|
| `cf/src/sink-do.ts` | `flush()` serialized, monotonic suffix, returns `FlushedObject \| null`; new `flush_state` table |
| `cf/src/control-do.ts` | `EXPORT_TABLES`, `EXPORT_CHUNK`, `exportSnapshot(prefix, chunk?)` |
| `cf/src/index.ts` | `POST /admin/export`; `"exports/"` added to `R2_PREFIXES` |
| `cf/src/schemas.ts` | `ExportManifest` Zod schema |
| `cf/test/export.test.ts` | **new** |
| `cf/README.md` | Section "Export for the Postgres migration (M6)": the route, the layout, the cursor rule, and the two `just` recipes |
| `src/nextflow_telemetry/v2_import.py` | **new**: reading, import, verification, CLI |
| `tests/test_v2_import.py` | **new** |
| `justfile` | Recipes `v2-export` and `v2-import` in the v2 section |

### Worker: SinkDO (`cf/src/sink-do.ts`)

```ts
/** The last NDJSON object written under telemetry/events/, and its line count. */
export interface FlushedObject { key: string; lines: number }
```

Add this to the constructor's `create table` block:

```sql
create table if not exists flush_state (
  id    integer primary key check (id = 1),
  seq   integer not null,
  key   text not null,
  lines integer not null
);
```

`async flush(): Promise<FlushedObject | null>`. The whole body runs inside
`return this.ctx.blockConcurrencyWhile(async () => { ... })`:
1. Read `buf` rows and the `flush_state` row (if there is one).
2. If `buf` is empty, return `{ key, lines }` from `flush_state`, or `null` if there is no
   row. This happens only before the first flush after deploy.
3. Otherwise set `seq = (state?.seq ?? 0) + 1` and
   `key = telemetry/events/dt=${dt}/${HHMMSS}-${seq.toString(16).padStart(8, "0")}.ndjson.gz`.
4. Do the R2 `put` exactly as today.
5. Delete the flushed `buf` rows, then upsert
   `flush_state (1, seq, key, rows.length)`, then `deleteAlarm()`.
6. Return `{ key, lines: rows.length }`.

`write()` and `alarm()` keep calling `this.flush()` and ignore its result. Nothing else
in SinkDO changes. The 60 s and 500-row bounds stay; M5 changes `FLUSH_MS`.

### Worker: ControlDO (`cf/src/control-do.ts`)

```ts
/** Every ControlDO table, in the issue's order (#376). */
const EXPORT_TABLES = ["samples", "collections", "collection_samples", "workflows", "jobs",
  "job_counts", "runs", "dead_letter", "daemons", "run_task_counts", "run_failed_tasks"];
/** Rows per export object. */
const EXPORT_CHUNK = 10_000;

/**
 * Consistent whole-DO export for the Postgres migration (ADR-0013 §8, #376).
 * blockConcurrencyWhile holds every other request until the last object is written,
 * so the R2 puts between chunks can't interleave a claim or an event.
 */
async exportSnapshot(prefix: string, chunk = EXPORT_CHUNK): Promise<{
  exported_at: string;
  sequences: Record<string, number>;
  tables: Record<string, { rows: number; objects: string[] }>;
}>
```

The body runs inside `this.ctx.blockConcurrencyWhile(async () => { ... })`:
1. `exported_at = iso()`.
2. `sequences = Object.fromEntries(select name, seq from sqlite_sequence)`.
3. For each table `t` in `EXPORT_TABLES`, keyset-page by rowid. Every v2 table is a rowid
   table.
   - Query: `select rowid as __export_rowid, * from ${t} where rowid > ? order by rowid limit ?`.
   - Delete `__export_rowid` from each row.
   - Put `await gzip(ndjson(rows))` at `${prefix}/${t}/${String(i).padStart(4, "0")}.ndjson.gz`.
   - Continue until a page comes back shorter than `chunk`.
   - An empty table gives `rows: 0, objects: []`.
4. Rows are written exactly as SQLite holds them: JSON text stays text (`params`,
   `collections`, `metadata`), and timestamps stay ISO strings. Nothing is hydrated.

Cloudflare runtime fact: `blockConcurrencyWhile` resets the object if its callback runs
longer than 30 s (see Risks). At production scale the export is about 30 puts.

### Worker: route (`cf/src/index.ts`)

```ts
api.post("/admin/export", describeRoute({
  summary: "Consistent ControlDO export to R2 (exports/<id>/) with the event cursor, for the Postgres import (#376)",
  tags: ["admin"],
  responses: { "200": { description: "OK", content: { "application/json": { schema: resolver(S.ExportManifest) } } } },
}), async (c) => { ... });
```

1. `const flushed = await sink(c.env).flush();`. This comes **first**, so every event
   already in R2 at or before `flushed.key` was applied to ControlDO before the snapshot.
   Each route awaits its ControlDO calls before `sink.write`.
2. `export_id = new Date().toISOString().replace(/[-:.]/g, "")`, for example
   `20261030T231500123Z`. Then `prefix = exports/${export_id}`.
3. `const snap = await control(c.env).exportSnapshot(prefix);`
4. Build the manifest:

   ```ts
   {
     format: 1,
     export_id,
     exported_at: snap.exported_at,
     settings: {
       CLAIM_TTL_MINUTES: num(c.env.CLAIM_TTL_MINUTES, 5),
       SUBMIT_BACKSTOP_HOURS: num(c.env.SUBMIT_BACKSTOP_HOURS, 48),
       LIVENESS_MINUTES: num(c.env.LIVENESS_MINUTES, 10),
     },
     sequences: snap.sequences,
     tables: snap.tables,
     events_cursor: flushed
       ? { stream: "telemetry/events", object_key: flushed.key, line_offset: flushed.lines }
       : null,
   }
   ```

   The settings defaults are the ones the dispatch routes pass to `mins` and `hours`, so
   the manifest records what RunDO actually armed.
5. `await c.env.STORE.put(`${prefix}/manifest.json`, JSON.stringify(manifest, null, 2))`.
   The manifest is written last, so its presence means the export is complete.
6. Return `c.json(manifest)`.

Auth: the route is a POST that `authExempt` doesn't list, so the existing bearer
middleware covers it. No code change is needed for that.

`R2_PREFIXES` gains `"exports/"`.

**Cursor rule** (documented in `cf/README.md` and binding on M5):
- Events in objects whose key is ≤ `object_key` (R2 list order), plus the first
  `line_offset` lines of `object_key`, are reflected in the snapshot.
- The ingester starts with R2 `list({ prefix: "telemetry/events/", startAfter: object_key })`.
- `line_offset` always equals the object's full line count here.
- The only overlap: an event whose ControlDO call finished before the snapshot but whose
  `sink.write` landed after the flush. That window is milliseconds, and the event lands in
  a later object. M5's idempotent transitions absorb it. The one exception is
  `recordTask`, whose task counters are not idempotent (see Risks).

### Worker: schema (`cf/src/schemas.ts`)

```ts
export const ExportManifest = z.object({
  format: z.literal(1),
  export_id: z.string(),
  exported_at: z.string().describe("ISO time the ControlDO snapshot began"),
  settings: z.object({ CLAIM_TTL_MINUTES: z.number(), SUBMIT_BACKSTOP_HOURS: z.number(), LIVENESS_MINUTES: z.number() }),
  sequences: z.record(z.string(), z.number().int()).describe("sqlite_sequence high-water marks"),
  tables: z.record(z.string(), z.object({ rows: z.number().int(), objects: z.array(z.string()) })),
  events_cursor: z.object({ stream: z.string(), object_key: z.string(), line_offset: z.number().int() }).nullable(),
});
```

### Export layout (local copy)

`rclone copy r2:nf-telemetry/exports/<id> DIR` gives this layout:
- `DIR/manifest.json`
- `DIR/<table>/<nnnn>.ndjson.gz`

The local path of a manifest object key is `DIR / key.removeprefix(f"exports/{export_id}/")`.

### Python: `src/nextflow_telemetry/v2_import.py`

Module docstring: "Load a v2 ControlDO export (POST /admin/export) into the control-plane
schema and verify it (ADR-0013 §8, #376). For scratch and staging databases and the M8
cutover; deleted with ControlDO in M10."

Imports:
- stdlib: `argparse`, `asyncio`, `gzip`, `json`, `os`, `re`, `collections.Counter`,
  `dataclasses`, `datetime`, `pathlib`;
- `sqlalchemy` (`insert`, `select`, `text`, `func`, `DateTime`) and
  `sqlalchemy.dialects.postgresql` (`ARRAY`, `JSONB`);
- `sqlalchemy.ext.asyncio` (`AsyncEngine`, `create_async_engine`);
- `from . import control_db as cdb` and `from .db import metadata`;
- `from .meta.store import readset_id`. This is the existing in-package twin of
  `nf_client.readset` and `cf/src/readset.ts`, pinned to ADR-0007's vectors.
- `from .utils import parse_srrs, srrs_to_sample_id`.

```python
EXPORT_FORMAT = 1
EVENTS_STREAM = "telemetry/events"
# Insert order (FK parents first). job_counts is read only for the drift note.
IMPORT_TABLES = ("samples", "collections", "collection_samples", "workflows", "runs", "jobs",
                 "dead_letter", "daemons", "run_task_counts", "run_failed_tasks")
SERIAL_TABLES = ("samples", "workflows", "jobs", "dead_letter", "run_failed_tasks")
INSERT_CHUNK = 5_000
_RUN = re.compile(r"^[SED]RR\d+$")


@dataclass(frozen=True)
class Snapshot:
    manifest: dict[str, Any]
    tables: dict[str, list[dict[str, Any]]]  # raw v2 rows, as exported; every EXPORT table


def read_snapshot(directory: Path) -> Snapshot:
    """Read manifest.json and every object it lists.

    Raises ValueError if any of these hold: format != EXPORT_FORMAT; a table in
    IMPORT_TABLES or job_counts is missing; a listed object is missing; a table's
    line count differs from its manifest `rows`."""


def readset_id_for_accession(ncbi_accession: str | None) -> str | None:
    """v2's rule (cf/src/readset.ts readsetIdForRuns): split on ';', drop empties.
    None if no runs or any entry is not an INSDC run accession; else readset_id(runs)."""


def to_pg_rows(snapshot: Snapshot) -> dict[str, list[dict[str, Any]]]:
    """Every IMPORT_TABLES table plus "readset_runs", as rows for the control_db Tables.

    Conversion is driven by the Postgres column type in metadata.tables[t]:
    - DateTime: datetime.fromisoformat (UTC-aware); None stays None.
    - JSONB: json.loads of the text. samples.metadata None becomes {}.
    - ARRAY: json.loads of the text. None and [] both become None (ck_workflows_collections).
    - Anything else is passed through.

    A v2 column with no Postgres column raises ValueError naming table and column.
    runs rows get the three deadline columns (deadlines()). readset_runs is one
    row per run accession of every sample whose readset_id is not None:
    {run_accession, readset_id}. A run under two different readsets raises ValueError
    naming the run and both readsets; a run repeated under the same readset is kept once."""


def deadlines(run: dict[str, Any], settings: dict[str, float]) -> dict[str, datetime | None]:
    """claim_expires_at / submit_deadline / liveness_deadline for one converted run row
    (see Deadline derivation). At most one is set."""


async def import_snapshot(engine: AsyncEngine, snapshot: Snapshot, *, replace: bool) -> None:
    """One transaction (engine.begin()):
    1. If replace: TRUNCATE (one statement, no CASCADE) IMPORT_TABLES + readset_runs, then
       DELETE FROM ingest_cursor WHERE stream = EVENTS_STREAM. Otherwise, if any of
       those tables has a row, or the cursor row exists, raise TargetNotEmpty.
    2. Insert to_pg_rows() in IMPORT_TABLES order, then readset_runs, each via
       con.execute(insert(table), chunk) in INSERT_CHUNK chunks.
    3. If manifest events_cursor is not None: insert ingest_cursor(stream, object_key,
       line_offset). updated_at uses its server default.
    4. Reset each SERIAL_TABLES sequence (SQL below)."""


async def verify(engine: AsyncEngine, snapshot: Snapshot) -> dict[str, Any]:
    """The verification report (below). Read-only."""


class TargetNotEmpty(RuntimeError):
    """The target already holds control-plane rows and --replace was not given."""


def main(argv: list[str] | None = None) -> None:
    """CLI: python -m nextflow_telemetry.v2_import SNAPSHOT_DIR [--replace]."""


if __name__ == "__main__":
    main()
```

Sequence reset, for each `t` in `SERIAL_TABLES`. `t` comes from the constant, and
`:hw = manifest["sequences"].get(t, 0)`:

```sql
select setval(pg_get_serial_sequence(:t, 'id'),
              greatest(:hw, (select coalesce(max(id), 0) from {t}), 1),
              greatest(:hw, (select coalesce(max(id), 0) from {t})) > 0)
```

#### Deadline derivation

All times are in UTC. `s` is the manifest `settings`:

| v2 run | Column set |
|---|---|
| `status = 'claimed'` | `claim_expires_at = claimed_at + s.CLAIM_TTL_MINUTES min` |
| `status = 'submitted'` and `last_heartbeat_at is null` | `submit_deadline = submitted_at + s.SUBMIT_BACKSTOP_HOURS h` |
| `status = 'running'`, or `'submitted'` with a heartbeat | `liveness_deadline = coalesce(last_heartbeat_at, started_at, submitted_at) + s.LIVENESS_MINUTES min` |
| `completed`, `failed`, `expired` | none |

A deadline that is already in the past is imported as it is. M4's sweep, with its outage
grace, decides what happens to it.

#### CLI behavior

- `argparse`, with one positional `snapshot_dir` (`Path`) and `--replace` (`store_true`,
  help: "truncate the control-plane tables first; scratch and staging databases only").
- **Database URI.** Read it from `SQLALCHEMY_URI`, with no fallback. That is the
  `meta/catalog/cli.py` pattern. Exit with code 2 if it's unset or not `postgresql://` or
  `postgresql+asyncpg://`. Rewrite `postgresql://` to `postgresql+asyncpg://`.
- **Run order.** `read_snapshot`, then `import_snapshot`, then `verify` on the same
  engine. Print `json.dumps(report, indent=2, default=str)` to stdout, then
  `engine.dispose()`.
- **Exit codes.** 0 when `report["clean"]`, otherwise 1.
  - `ValueError`, `OSError` and `TargetNotEmpty` exit 2 with `v2 import: <message>`.
  - `SQLAlchemyError` exits 2 with "v2 import: database operation failed; check
    connectivity and migrations". It never prints the URI or row values (catalog pattern).
  - Any of these exits happens before or instead of the report.

#### Verification report

`verify` returns this shape. Mismatch lists are capped at 20 entries, and each section
carries the uncapped count:

```json
{
  "export_id": "20261030T231500123Z",
  "exported_at": "2026-10-30T23:15:00.123Z",
  "clean": true,
  "failures": [],
  "tables": {"samples": {"snapshot": 72159, "postgres": 72159, "row_mismatches": 0, "examples": []}},
  "readset_runs": {"expected": 0, "postgres": 0, "mismatches": 0, "examples": []},
  "collections": {"checked": 197, "mismatches": 0, "examples": [{"collection_id": "x", "snapshot": 3, "postgres": 2}]},
  "jobs_by_workflow_status": {"snapshot": {"7/pending": 1}, "postgres": {"7/pending": 1}, "mismatches": 0},
  "runs_by_status": {"snapshot": {"completed": 1}, "postgres": {"completed": 1}, "mismatches": 0},
  "identity": {"samples_checked": 72159,
               "readset_id_mismatches": 0, "sample_id_mismatches": 0, "examples": []},
  "workflows": {"checked": 5, "mismatches": 0,
                "examples": [{"id": 7, "workflow_id": "w", "version": "1", "field": "params"}]},
  "events_cursor": {"manifest": {"stream": "...", "object_key": "...", "line_offset": 1},
                    "postgres": {"stream": "...", "object_key": "...", "line_offset": 1}},
  "v2_job_counts_drift": [{"workflow_pk": 7, "status": "pending", "job_counts": 10, "jobs": 9}]
}
```

How each section is computed:
- **`tables`**, for each `IMPORT_TABLES` table:
  - Each side is a `Counter` of `json.dumps(row, sort_keys=True, default=str)`.
  - Snapshot side: `to_pg_rows` rows restricted to the v2 columns. Postgres side:
    `select <those columns> from t`.
  - Excluded columns: those that exist only in Postgres (deadlines, `jobs.priority`,
    `collections.study_id`, `run_failed_tasks.id`), and the derived deadline columns.
  - `row_mismatches` is the size of the multiset symmetric difference. `examples` holds up
    to 5 differing serialized rows.
- **`readset_runs`:**
  - `expected` is the set of `(run, readset_id)` pairs recomputed from the **Postgres**
    `samples` rows with `readset_id_for_accession`.
  - That set is compared with `select run_accession, readset_id from readset_runs`.
- **`collections`:** for every `collections` row, the member count in the snapshot's
  `collection_samples` is compared with `select collection_id, count(*) … group by 1`.
  Collections with no members count as 0.
- **`jobs_by_workflow_status` and `runs_by_status`:** `Counter`s from the raw snapshot rows
  versus `group by` in Postgres. The keys are `"<workflow_pk>/<status>"` and `<status>`.
- **`identity`:** over the Postgres `samples` rows:
  - `readset_id_for_accession(ncbi_accession)` must equal `readset_id`;
  - `srrs_to_sample_id(parse_srrs(ncbi_accession))` must equal `sample_id`.
- **`workflows`:** for each snapshot workflow, by `id`, these must be equal:
  - Postgres `params` and `json.loads(params)`;
  - Postgres `collections` and the converted scope (`None` for null or `[]`);
  - `sample_key`.
- **`events_cursor`:** the manifest cursor versus the `ingest_cursor` row for
  `EVENTS_STREAM` (`object_key`, `line_offset`).
- **`v2_job_counts_drift`:** rows where v2's `job_counts.n` differs from the snapshot's
  jobs count for that `(workflow_pk, status)`. A missing row counts as 0. This section is
  informational and stays out of `failures`.
- **`clean`** is true exactly when `failures` is empty. `failures` gets one string per
  failing section, for example `"tables.jobs: 3 row mismatches"`. These conditions fail:
  - any table count difference or row mismatch;
  - a non-zero `readset_runs`, `collections`, `jobs_by_workflow_status`, `runs_by_status`,
    `identity` or `workflows` mismatch;
  - a manifest `events_cursor` that is null ("no event cursor: the sink had never
    flushed");
  - a cursor that differs from the `ingest_cursor` row.

### justfile (v2 section)

```just
# M6 (#376): export v2's ControlDO to R2 and copy it to dir/<export_id> (prints the path).
v2-export dir:
	id=$(curl -sf -X POST -H "Authorization: Bearer $(gcloud secrets versions access latest --secret=cdsci-nf-telemetry-v2-api-token --project=cdsci-infra)" {{v2_url}}/api/admin/export | python3 -c 'import sys,json;print(json.load(sys.stdin)["export_id"])'); \
	  rclone copy r2:nf-telemetry/exports/$id {{dir}}/$id && echo {{dir}}/$id

# M6 (#376): import an export into $SQLALCHEMY_URI (alembic head) and print the verification report.
v2-import dir *flags:
	uv run python -m nextflow_telemetry.v2_import {{dir}} {{flags}}
```

### Endpoints, config, callers

- **Endpoints:** one is added on the Worker, `POST /admin/export` (bearer). There are no
  changes to the Python service's routes.
- **Config and env:**
  - Worker: none (it reuses `STORE`, `API_TOKEN` and the three deadline vars).
  - Import: reads the existing `SQLALCHEMY_URI` only.
- **Callers:** `nf-client`, `etl/v2.py`, the frontend, `health_report.py` and the add-study
  bot are not affected. `EventArchive` still globs `dt=*/*.ndjson.gz`, and the new suffix
  matches it.

## Acceptance criteria

- [ ] `cd cf && npx vitest run` passes, including `openapi.test.ts`, which requires a
      summary on the new route, and the new `export.test.ts`.
- [ ] `cd cf && npx tsc --noEmit` passes.
- [ ] `POST /admin/export` returns a manifest that parses with `S.ExportManifest`, and:
  - for every one of the 11 tables, `rows` equals the ControlDO `count(*)`;
  - every listed object exists in R2 and gunzips to that table's rows, in total;
  - `manifest.json` exists under `exports/<export_id>/`.
- [ ] Rows read back from the objects deep-equal the ControlDO rows, so no
      `__export_rowid` leaks.
- [ ] With `chunk = 2`, a table of 5 rows exports as 3 objects in rowid order.
- [ ] `events_cursor.object_key` exists in R2, and its line count equals `line_offset`.
      Weblog events posted before the export are in objects at or before the cursor key.
- [ ] A second `flush()` with an empty buffer returns the same `{ key, lines }`.
      Successive non-empty flushes produce strictly increasing keys, including within one
      second.
- [ ] `authExempt("POST", "/api/admin/export")` is false.
- [ ] `uv run pytest tests/test_v2_import.py` passes. That covers the items below.
- [ ] Unit (no database):
  - `readset_id_for_accession` matches ADR-0007's golden vectors (`SRR000001` →
    `RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF`, the eight Zeller runs →
    `RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj`);
  - it returns None for `""`, for `None` and for an entry that isn't a run;
  - `to_pg_rows` converts `2026-10-30T23:37:55.294Z` to an aware datetime with 294000 µs;
  - it converts `collections` `"[]"` and null to None, and `metadata` null to `{}`;
  - it raises on an unknown v2 column and on a run under two readsets;
  - `deadlines` gives each table row's result, and none for terminal runs;
  - `read_snapshot` rejects `format: 2`, a missing object, and a line-count mismatch.
- [ ] Integration (the `db_url` fixture's database):
  - importing the fixture export gives `clean: true`;
  - ids and timestamps equal the fixture's;
  - the next default insert into `jobs` gets an id above the manifest high-water mark
    (which is above `max(id)`);
  - `ingest_cursor` holds the manifest cursor;
  - `readset_runs` has one row per run.
- [ ] A second import without `replace` raises `TargetNotEmpty`. With `replace=True` it
      succeeds and is clean again.
- [ ] After a clean import, each of these makes `verify` report `clean: false` with the
      matching failure:
  - changing one sample's `readset_id`;
  - changing one workflow's `params`;
  - deleting one `collection_samples` row;
  - changing one job's `status`.
- [ ] A manifest with `events_cursor: null` imports, writes no cursor row, and reports
      `clean: false`.
- [ ] `main([dir])` with `SQLALCHEMY_URI` set exits 0 and prints the JSON report. Without
      `SQLALCHEMY_URI` it exits 2.
- [ ] `uv run pytest` passes, and so do `uv run pytest tests/test_source_catalog.py` and
      `uv run mypy src/nextflow_telemetry`.
- [ ] The local end-to-end rehearsal in the Test plan gives a clean report. Paste the
      commands and the report summary into the PR.

## Test plan

### `cf/test/export.test.ts`

This uses the same harness as `lifecycle.test.ts`: `SELF.fetch`, `env`,
`runInDurableObject`, and the shared storage with `ALLOW_RESET=true`.
1. `beforeAll` runs `POST /admin/reset`.
2. Seed the state:
   - register a workflow with `collections: ["c1"]`;
   - `POST /samples` with three samples (one with SRR runs in two collections);
   - `POST /admin/reconcile-jobs`;
   - `POST /dispatch/batch` with a limit of 1;
   - `POST /dispatch/submitted`;
   - weblog `started` and `process_completed` events on `/telemetry`;
   - a daemon heartbeat.
3. `POST /admin/export`, then assert the manifest items in the Acceptance criteria. Read
   objects with `env.STORE.get(key)`, then
   `new Response(body.pipeThrough(new DecompressionStream("gzip"))).text()`. Get ControlDO
   counts and rows via `runInDurableObject(control stub, (_, state) => state.storage.sql.exec(...))`.
4. Chunking: `runInDurableObject(stub, (inst) => inst.exportSnapshot("exports/test-chunk", 2))`,
   then assert the samples object count and the order.
5. Flush: call `runInDurableObject(sink stub, (inst) => inst.flush())` twice. Then post
   several weblog events with a flush after each, and assert the keys are strictly
   increasing.

### `tests/test_v2_import.py`

- **Fixture `export_dir(tmp_path)`.** It writes a hand-built v2 export (`format: 1`,
  `export_id: "20261030T000000000Z"`) as gzipped NDJSON plus `manifest.json`, in exactly
  the Worker's layout. Contents:
  - 3 samples. One uses the Zeller runs, one has `metadata: null`, and two are in two
    collections.
  - 2 workflows: `collections` null with `sample_key` `sample_id`, and `'["c1"]'` with
    `params` `'{"a":1}'`.
  - Runs: one each completed, claimed, submitted without a heartbeat, and running with a
    heartbeat.
  - Jobs covering all six statuses, consistent with M1's checks.
  - One `dead_letter` row, one daemon, `run_task_counts`, two `run_failed_tasks`.
  - `job_counts` with one deliberate drift.
  - `sequences: {"jobs": 500, "samples": 10, "workflows": 3, "dead_letter": 7}`, all above
    the max ids.
  - `settings` 5 / 168 / 10, and a non-null `events_cursor`.
- **Unit tests:** as listed in the Acceptance criteria.
- **Integration tests:** use `db_url`. The session schema comes from
  `metadata.create_all`, so it has the control-plane tables but not the workflows trigger,
  and the trigger doesn't matter here. Run `asyncio.run` around
  `create_async_engine(db_url)`, as the existing tests do. The autouse `clean_db` empties
  every table between tests.
- **CLI:** `monkeypatch.setenv("SQLALCHEMY_URI", db_url)`, then
  `pytest.raises(SystemExit)` around `main([str(dir)])`, checking `.code` and `capsys`.

### Local end-to-end rehearsal (manual, recorded in the PR)

1. `just v2-local`, then `just contract` (or `just v2-seed` pointed at
   `http://127.0.0.1:8787/api`), so the local v2 has state.
2. `curl -sf -X POST -H 'Authorization: Bearer contract-token' http://127.0.0.1:8787/api/admin/export > m.json`.
3. For `manifest.json` and every key in `m.json`, run
   `cd cf && npx wrangler r2 object get nf-telemetry/<key> --local --file <dir>/<key minus exports/<id>/>`.
4. Create a scratch database, run `SQLALCHEMY_URI=… uv run alembic upgrade head`, then
   `SQLALCHEMY_URI=… just v2-import <dir>`. It should exit 0 with `clean: true`.
5. Run `just v2-import <dir> --replace` again. It should be clean again.

### Commands

- `cd cf && npx vitest run && npx tsc --noEmit`
- `uv run pytest tests/test_v2_import.py`
- `uv run pytest`
- `uv run pytest tests/test_source_catalog.py`
- `uv run mypy src/nextflow_telemetry`

## Done-when and the production rehearsal

The issue is done when "a production snapshot imports into a scratch database with a
clean verification report". That needs the Worker deployed with this change, which is an
outward-facing production action. The implement stage doesn't do it. After merge, the
operator:
1. deploys the Worker (`cd cf && npx wrangler deploy`);
2. runs `just v2-export /data/davsean/tmp/v2-exports`;
3. creates a scratch database on `pg_main`, then runs `alembic upgrade head` and
   `just v2-import <path>`;
4. pastes the report summary into #376 and closes it.

The PR therefore says "Refs #376", not "Closes".

## Risks

- **The 30 s `blockConcurrencyWhile` limit.** If the export callback runs longer than
  30 s, the runtime resets ControlDO. Stored state survives, but the export fails with no
  manifest. At about 30 puts of at most 10k rows each it should take a few seconds.
  Watch the time in the first production run. If it gets close, raise `EXPORT_CHUNK`.
  While the export runs, claims and events wait for it.
- **The SinkDO change touches the live event path.** `flush` now holds writes for the
  length of one R2 put, about 100 ms. The same lock removes today's alarm-versus-burst
  double-write. Key shape and partitioning are unchanged. Deploy it with the export and
  watch `telemetry/events/` for a few minutes.
- **Overlap at the cursor.** An event can be applied to ControlDO before the snapshot but
  written to the sink after the flush. That event is replayed by M5. Its transitions are
  idempotent, but `recordTask` increments `run_task_counts` and appends
  `run_failed_tasks`, so a replayed `process_completed` in that millisecond window counts
  twice. That only affects triage counts, and M5 should know about it. There's no gap in
  the other direction: every event in R2 at or before the cursor was applied first.
- **Derived deadlines, one known difference.** A run whose wrapper sent `wrapper_started`
  but no heartbeat yet is `submitted` with a null `last_heartbeat_at`, while RunDO has
  already armed liveness. The import gives it the submit backstop instead. Its next
  heartbeat, applied by M5, moves it to liveness, so the window lasts about 60 s.
  Otherwise the derived values match RunDO's alarms: ControlDO's heartbeat write and the
  RunDO re-arm happen in the same request, with the same timestamp.
- **`--replace` on the wrong database** truncates the live control plane. It's a flag,
  `SQLALCHEMY_URI` has no default, and the help text says scratch and staging only. After
  M8 the module is dead code, and M10 deletes it.
- **v2 changes between rehearsal and cutover.** A new ControlDO column makes
  `to_pg_rows` fail loudly ("no Postgres column"), which is the intent.
- **Real-data constraint violations.** M1's checks assume v2's invariants. They held on
  2026-10-30 (see Goal), but a later violation fails the import, by design, and needs a
  data fix in v2 or a documented mapping.
