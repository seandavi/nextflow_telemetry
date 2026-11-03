# Spec #420: job priority by collection, in v2 and the Postgres service

Issue: [#420](https://github.com/seandavi/nextflow_telemetry/issues/420). There is no
GitHub parent issue. The need was first recorded in
[#298](https://github.com/seandavi/nextflow_telemetry/issues/298) ("no job priority"),
which tracking issue [#381](https://github.com/seandavi/nextflow_telemetry/issues/381)
folds into M1 (schema, done: `jobs.priority` exists in Postgres) and M3 (claim order,
done: Postgres claims by `priority desc, created_at, id`). Neither control plane can set
priority today. v2 has no priority column.

## Goal

cMD studies run before ZOE PREDICT on `cmgd_mpa4.2` 2.3.0 (workflow_pk 7). About 36k ZOE
jobs (PRJEB75460, PRJEB75462–PRJEB75465) are interleaved with about 19k other pending jobs.
In about 7k claims, ZOE starts taking about two-thirds of the claims.

After this change:

- **Both control planes accept the same request.** v2 (`cf/`) and the Postgres service
  both serve `POST /api/admin/job-priority` with the operator bearer token. It sets
  `jobs.priority` on the **pending** jobs whose sample is a member of a collection,
  optionally limited to one registration (`workflow_pk`).
- **v2 claims by priority.** v2 stores `jobs.priority` (default 0) and claims the
  highest priority first.
- **The value survives the M6 export.** Priority goes through `/admin/export`, and the
  import into Postgres keeps it.

The operator then sets priority −1 on the five ZOE collections for workflow_pk 7 (Rollout).

## Non-goals

- **No registration-level default priority.** The issue asks for a hook only. The hook is
  that `reconcileJobs` (v2) and `_RECONCILE_SQL` (Postgres) leave `priority` out of their
  insert column list, so new jobs take the column default 0. A later registration default
  would add `priority` to that insert, selected from `workflows`. Each statement gets a
  one-line comment saying this. Nothing else is built: no `workflows` column, no request
  field.
- **No change to how the Postgres service claims.** M3 (#373) already orders by
  `priority desc, created_at, id`. Its pick step isn't grouped by `workflow_id,
  workflow_version` the way v2's is. Changing that is out of scope (see Risks).
- **No priority on reads.** Job lists, job summaries and cohort endpoints don't show
  priority, and no list or filter by priority is added. The endpoint's `updated` count,
  and SQL on onclappc02, are how an operator checks the result.
- **No change to retries.** A job swept back to `pending` keeps its priority. Neither
  sweep touches the column.
- **No new v2 index** (see Design §1.3 and Risks).
- **No Alembic migration.** Postgres already has `jobs.priority INTEGER NOT NULL DEFAULT
  0` and `ix_jobs_pending_claim` (M1, `20261030_fa0b1c2d_control_plane_schema.py`).
- **No config or env changes.** No new dependency, in either `pyproject.toml` or
  `cf/package.json`.
- **No other callers change.** `nf-client`, the run wrapper, `etl/`, `health_report.py`,
  the frontend and the add-study bot don't call this route and see no wire change. The
  dashboard doesn't show priority.

### Departures from the design record

- **#373 (M3 spec, "Notes for operators")** says there is no API for priority, and that
  operators set it with SQL. This adds the API. The SQL still works.
- **#376 (M6 spec)** lists `jobs.priority` as a Postgres-only column that `verify`
  excludes. Once v2 exports `priority`, it is a v2 column, so `verify` compares it. That
  follows from `verify`'s existing rule: the v2 columns are the keys in the snapshot rows.
  `v2_import.py` needs no code change.
- **The issue asks to "map it in `v2_import.py`".** `_convert` already passes through
  every v2 column that has a Postgres column of the same name, and an older export without
  `priority` takes the server default 0. So the mapping is covered by tests (§3.4), not
  new code.
- **The issue specifies only `{"updated": int}`.** This spec adds:
  - `404` for an unknown `collection_id` or `workflow_pk`, so a typo in a BioProject
    accession fails loudly instead of returning `{"updated": 0}`;
  - `422` for a malformed body.
- ADR-0013 (control plane moves to Postgres), ADR-0007, ADR-0010 and ADR-0012 are
  unaffected. v2 gets the change only because it still serves production claims until M8.

## Design

### Contract (both services)

`POST /api/admin/job-priority`, also served at `/admin/job-priority`, like every route.

- **Auth.** Bearer token: v2 `API_TOKEN`, Postgres `require_service` (`DISPATCH_TOKEN`),
  as for every other admin write.
- **Request body** (JSON):

  | field | type | rule |
  |---|---|---|
  | `collection_id` | string | required, non-empty |
  | `priority` | integer | required, `-2147483648` ≤ p ≤ `2147483647` (int4); booleans and strings are rejected |
  | `workflow_pk` | integer or null | optional, default null (every registration) |

- **Effect.** One statement:
  - Every job with `status = 'pending'`, whose `sample_id` is in `collection_samples`
    for `collection_id`, and whose `workflow_pk` equals the given one (when not null),
    gets `priority = <priority>`.
  - Jobs in `claimed`, `submitted`, `running`, `completed` and `failed` are untouched.
- **`200` response:** `{"updated": <int>}`. The value is the number of jobs the statement
  **matched**, whether or not their priority changed. So a repeated call returns the same
  `updated` and leaves the same state. Higher priority is claimed first, and the default
  is 0.
- **`404` response:** `{"detail": str}` when `collection_id` is not a row in
  `collections`, or when `workflow_pk` is given and is not a row in `workflows`. The
  detail is exactly:
  - `Collection '<collection_id>' not found` when `workflow_pk` is null;
  - `Collection '<collection_id>' or workflow <workflow_pk> not found` otherwise.
- **`422` response:** `{"detail": str}` for a missing or invalid field. The text differs
  between the services (v2 uses a fixed message; Postgres uses `ControlRoute`'s joined
  message), as for the other routes.
- **`401` response:** no token or a wrong token.

### 1. v2 (`cf/`)

#### 1.1 `cf/src/schema.ts`

Add one column to `create table if not exists jobs`, after `retry_count`:

```sql
  priority         integer not null default 0, -- claimed highest first; set by POST /admin/job-priority (#420)
```

Fresh objects get the column from `SCHEMA`. Existing ones get it from §1.2. No index
changes.

#### 1.2 `cf/src/control-do.ts`: guarded migration

Add a private method, in the same style as `addWorkflowBundleColumns`:

```ts
  /** #420: objects created before priority get the column; every existing job reads 0. */
  private addJobPriority(): void {
    if (!this.columns("jobs").has("priority")) {
      this.sql.exec(`alter table jobs add column priority integer not null default 0`);
    }
  }
```

Call it in the constructor right after `this.addWorkflowBundleColumns();`. It is
synchronous, so it doesn't go inside `blockConcurrencyWhile`. In SQLite, `ADD COLUMN`
with a constant default changes only the schema, so it costs the same on a 55k-row table
as on an empty one. A second call is a no-op.

#### 1.3 `cf/src/control-do.ts`: `claimBatch`

Two `order by` clauses change. Nothing else in the method changes.

- **Pick** (chooses the workflow version):
  `order by j.priority desc, j.workflow_id, j.workflow_version, j.created_at, j.id`.
  The version whose best pending job has the highest priority wins. Ties go to the
  lexicographically first `(workflow_id, workflow_version)`, as today.
- **Take** (jobs of that version):
  `order by j.priority desc, j.created_at, j.id`.

Update the method's doc comment. "Oldest pending job decides the workflow" becomes:
"the highest-priority pending job decides the workflow version (ties as before: lowest
`workflow_id`, `workflow_version`), and that version's jobs are taken highest priority
first, then oldest."

Add a `ponytail:` comment above the pick query:

```ts
    // ponytail: no index leads with priority, so both queries sort every pending job in
    // scope (~55k rows read per claim at 2026-11 volume, a few hundred claims a day).
    // Add (status, priority desc, workflow_id, workflow_version, created_at) if rows read
    // ever matter; v2 retires at M10.
```

#### 1.4 `cf/src/control-do.ts`: `setJobPriority`

A public method in the "Jobs: birth, claim, transitions" section, after `reconcileJobs`:

```ts
  /**
   * Operator priority (#420): every pending job whose sample is in the collection,
   * optionally of one registration. Claimed and later jobs keep theirs. Returns the jobs
   * matched (the same on a repeat call), or null for an unknown collection or workflow.
   */
  setJobPriority(collectionId: string, priority: number, workflowPk: number | null): number | null {
    if (!this.first(`select 1 as x from collections where collection_id = ?`, collectionId)) return null;
    if (workflowPk !== null && !this.first(`select 1 as x from workflows where id = ?`, workflowPk)) return null;
    return this.run(
      `update jobs set priority = ?
        where status = 'pending' and (? is null or workflow_pk = ?)
          and sample_id in (select sample_id from collection_samples where collection_id = ?)`,
      priority,
      workflowPk,
      workflowPk,
      collectionId,
    );
  }
```

- The `in` subquery is uncorrelated: SQLite evaluates it once, through the
  `collection_samples` primary key.
- `this.run` appends `returning 1`, so the count is the matched rows.
- It uses four bound parameters, well under DO SQLite's 100.
- The method doesn't `await`, so it is atomic against claims.

`reconcileJobs` also gets a one-line comment above its `insert`:
`// priority is left to the column default (0); a registration-level default would select it from w here (#420).`

#### 1.5 `cf/src/schemas.ts`

In the requests section, after `CloseRunRequest`:

```ts
export const JobPriorityRequest = z.object({
  collection_id: z.string().min(1),
  priority: z.number().int().min(-2147483648).max(2147483647),
  workflow_pk: z.number().int().nullish(),
});
```

In the responses section:

```ts
export const JobPriorityUpdated = z.object({ updated: z.number().int() });
```

zod is 4.6.5 (`cf/package-lock.json`). `z.number().int()` rejects booleans, strings and
non-integers, and `.nullish()` accepts both a missing value and `null`. These are the same
APIs `schemas.ts` already uses.

#### 1.6 `cf/src/index.ts`: route

In the admin section, after `/admin/requeue-dead-letter`:

```ts
api.post("/admin/job-priority", describeRoute({
  summary: "Set the claim priority of a collection's pending jobs (higher is claimed first)",
  tags: ["admin"],
  requestBody: { content: { "application/json": { schema: resolver(S.JobPriorityRequest) } } },
  responses: {
    "200": { description: "OK", content: { "application/json": { schema: resolver(S.JobPriorityUpdated) } } },
    "404": { description: "Unknown collection or workflow" },
    "422": { description: "Invalid body" },
  },
}), async (c) => {
  const parsed = S.JobPriorityRequest.safeParse(await json(c));
  if (!parsed.success) {
    return c.json({ detail: "collection_id (non-empty string) and priority (integer) are required; workflow_pk is an integer or null" }, 422);
  }
  const { collection_id, priority, workflow_pk } = parsed.data;
  const pk = workflow_pk ?? null;
  const updated = await control(c.env).setJobPriority(collection_id, priority, pk);
  if (updated === null) {
    const what = pk === null ? `Collection '${collection_id}'` : `Collection '${collection_id}' or workflow ${pk}`;
    return c.json({ detail: `${what} not found` }, 404);
  }
  return c.json({ updated });
});
```

- **Auth.** The existing bearer middleware covers the route. `authExempt` is unchanged:
  it is a POST outside the exempt paths.
- **OpenAPI.** The route is documented, so `openapi.test.ts` ("documents every route")
  still passes.
- **Sink-only mode.** Nothing is gated: like every other admin route, it writes ControlDO
  in both modes.

#### 1.7 Export (`/admin/export`, M6)

No code change. `exportSnapshot` runs `select rowid as __export_rowid, *`, so once the
column exists, `priority` is in every exported `jobs` row.

#### 1.8 `cf/README.md`

In the "Endpoint status" table, the admin row becomes:
`/admin/{reconcile-jobs,reset-running,close-run,requeue-dead-letter,job-priority,dispatchability,stats}`.

### 2. Postgres service

#### 2.1 `src/nextflow_telemetry/control_models.py`

Add `StrictInt` to the pydantic import (`from pydantic import BaseModel, ConfigDict,
Field, StrictInt, field_validator`). After `RequeueDeadLetterRequest`:

```python
class JobPriorityRequest(BaseModel):
    collection_id: str = Field(min_length=1)
    priority: Annotated[StrictInt, Field(ge=-(2**31), le=2**31 - 1)]
    workflow_pk: StrictInt | None = None
```

After `ResetRunning`:

```python
class JobPriorityUpdated(BaseModel):
    updated: int
```

Use `StrictInt`, not `int`. Checked against the locked pydantic 2.13.3: plain `int`
accepts `true` and `"3"`, which Zod rejects. Unknown body keys are ignored (the
`BaseModel` default), as Zod `z.object` strips them.

#### 2.2 `src/nextflow_telemetry/services/transitions.py`

Add `collections_tbl` to the `..control_db` import. After `requeue_dead_letter`:

```python
async def set_job_priority(
    conn: AsyncConnection, *, collection_id: str, priority: int, workflow_pk: int | None
) -> int | None:
    """Set ``priority`` on the pending jobs whose sample is in ``collection_id`` (of ``workflow_pk`` when given).
    Returns the jobs matched, the same on a repeat call, or None for an unknown collection or workflow."""
    if (await conn.execute(select(collections_tbl.c.collection_id).where(collections_tbl.c.collection_id == collection_id))).first() is None:
        return None
    if workflow_pk is not None and (await conn.execute(select(workflows_tbl.c.id).where(workflows_tbl.c.id == workflow_pk))).first() is None:
        return None
    members = select(collection_samples_tbl.c.sample_id).where(collection_samples_tbl.c.collection_id == collection_id)
    stmt = update(jobs_tbl).where(jobs_tbl.c.status == "pending", jobs_tbl.c.sample_id.in_(members)).values(priority=priority)
    if workflow_pk is not None:
        stmt = stmt.where(jobs_tbl.c.workflow_pk == workflow_pk)
    return (await conn.execute(stmt)).rowcount
```

- **Why in `transitions.py`.** The function lives there because it writes jobs, next to
  `requeue_dead_letter`, which has the same collection filter. It doesn't touch `status`.
- **Count.** Postgres `UPDATE` reports matched rows, including rows whose value doesn't
  change, so a repeat call returns the same count.
- **Concurrency.** A claim that holds a row lock (`FOR UPDATE SKIP LOCKED`) makes the
  update wait. Under READ COMMITTED, the update then re-checks `status = 'pending'` on the
  committed row, so a job claimed in the meantime is skipped.

`_RECONCILE_SQL` gets the same one-line hook comment as v2, inside the text above the
`insert`, as a SQL `--` comment:
`-- priority is left to the column default (0); a registration-level default would select it from w here (#420).`

#### 2.3 `src/nextflow_telemetry/routers/admin.py`

Import `JobPriorityRequest` and `JobPriorityUpdated`. After `/requeue-dead-letter`:

```python
    @router.post(
        "/job-priority",
        response_model=JobPriorityUpdated,
        dependencies=[Depends(require_service)],
        summary="Set the claim priority of a collection's pending jobs (higher is claimed first)",
        description=(
            "Sets `priority` on every pending job whose sample is in `collection_id`, limited to `workflow_pk` "
            "when given. Claimed, running and finished jobs keep theirs. `updated` counts the jobs matched, so a "
            "repeat call returns the same. 404 for an unknown collection or workflow."
        ),
    )
    async def job_priority(req: JobPriorityRequest):
        async with engine.begin() as conn:
            updated = await transitions.set_job_priority(
                conn, collection_id=req.collection_id, priority=req.priority, workflow_pk=req.workflow_pk
            )
        if updated is None:
            what = f"Collection '{req.collection_id}'" if req.workflow_pk is None else (
                f"Collection '{req.collection_id}' or workflow {req.workflow_pk}"
            )
            raise HTTPException(status_code=404, detail=f"{what} not found")
        return {"updated": updated}
```

`ControlRoute` turns body-validation failures into a string `detail`. `require_service`
runs before body validation, so a request with no token gets 401 even with a bad body.

#### 2.4 `README.md`

In the API table, the Admin row becomes:
`POST /admin/reconcile-jobs`, `/admin/close-run`, `/admin/requeue-dead-letter`,
`/admin/job-priority`, then the rest of the row as it is.

### 3. Tests

#### 3.1 `cf/test/job-priority.test.ts` (new, vitest under `@cloudflare/vitest-pool-workers`)

Use the helpers that `export.test.ts` and `requeue-many.test.ts` use: `SELF.fetch`, plus
`runInDurableObject` on `env.CONTROL`.

**Ordering, with no HTTP.** Each `it` uses its own object,
`env.CONTROL.idFromName("job-priority-<n>")`, and seeds it with raw SQL:
- workflows `(id 1, 'wf-a', '1')` and `(id 2, 'wf-b', '1')`, all with `status 'active'`,
  `sample_key 'sample_id'`, `repository_url 'u'`, `revision 'r'`, and
  `created_at`/`updated_at` set to `'2026-01-01T00:00:00.000Z'`;
- `jobs` rows with explicit `priority` and `created_at`.

Then call `instance.claimBatch(...)`.

1. **"claims a priority-0 job ahead of a priority −1 job created earlier"**
   - Seed `wf-a` jobs: `old` (priority −1, created 2026-01-01) and `new` (priority 0,
     created 2026-01-02).
   - `claimBatch(1, ["wf-a"])` returns jobs `[{sample_id: "new"}]`.
2. **"picks the version whose best pending job has the highest priority"**
   - Seed `wf-a`: `a1` (priority −1, older). Seed `wf-b`: `b1` (priority 0, newer).
   - `claimBatch(10)` returns `workflow_id: "wf-b"` and jobs `[b1]`.
3. **"breaks equal-priority ties by workflow id, as before"**
   - Seed `wf-a`: `a1` (priority 0, newer). Seed `wf-b`: `b1` (priority 0, older).
   - `claimBatch(10)` returns `workflow_id: "wf-a"`.

**Endpoint and migration, on the `v1` object.** `beforeAll` POSTs `/api/admin/reset`
(as `export.test.ts` does), then registers:
- `P1`: `workflow_id "prio-a"`, `version "1"`, `collections ["PZ", "PC"]`;
- `P2`: `workflow_id "prio-b"`, `version "1"`, `collections ["PC"]`;
- samples `Z` (`SRR100001`, collection `PZ`) and `C` (`SRR100002`, collection `PC`);
- then POSTs `/api/admin/reconcile-jobs`, which creates jobs Z/P1, C/P1 and C/P2.

A helper `priorities()` reads `select sample_id, workflow_pk, priority from jobs` through
`runInDurableObject` and returns a map keyed `"<sample_id>/<workflow_pk>"`.

4. **"updates only pending members of the collection, optionally one registration"**
   - POST `{collection_id: "PC", priority: 2}` returns `{updated: 2}`. Then C/P1 = 2,
     C/P2 = 2 and Z/P1 = 0.
   - POST `{collection_id: "PC", priority: 3, workflow_pk: P2}` returns `{updated: 1}`.
     Then C/P2 = 3 and C/P1 = 2.
   - POST `/api/dispatch/batch {limit: 10, workflow_id: "prio-b"}` claims C/P2.
   - POST `{collection_id: "PC", priority: 9}` returns `{updated: 1}`. C/P1 = 9, and
     C/P2 is still 3.
   - The response parses with `S.JobPriorityUpdated`.
5. **"gives the same result when run twice"**
   - POST `{collection_id: "PZ", priority: -1}` twice. Both responses are
     `{updated: 1}`, and `priorities()` is equal after each call.
6. **"404s an unknown collection or workflow and 422s a bad body"**
   - `{collection_id: "NOPE", priority: 1}` returns 404 with detail
     `Collection 'NOPE' not found`.
   - `{collection_id: "PZ", priority: 1, workflow_pk: 999999}` returns 404.
   - These bodies each return 422 with a string `detail`:
     - `{priority: 1}`;
     - `{collection_id: "PZ", priority: "high"}`;
     - `{collection_id: "PZ", priority: 1.5}`;
     - `{collection_id: "PZ", priority: true}`.
   - `authExempt("POST", "/api/admin/job-priority")` is `false`.
7. **"adds jobs.priority to a jobs table created before it"**
   - In `runInDurableObject` on `v1`, run `alter table jobs drop column priority`, then
     call `(instance as any).addJobPriority()` twice.
   - Every job row then reads priority 0.
   - `pragma_table_info('jobs')` shows `priority` with `notnull = 1` and
     `dflt_value = '0'`.

`cf/test/export.test.ts`, in "writes every table, the manifest and the event cursor": after
the per-table loop, also assert that every row read back for `jobs` has a numeric
`priority`.

#### 3.2 `tests/test_control_transitions.py`

**`test_set_job_priority_pending_members_only`**

Setup:
- `pk_a, _ = await seed(engine, "pri-a", samples=2, collection="pri-col-a")`
- `pk_b, _ = await seed(engine, "pri-b", samples=1, collection="pri-col-b")`

`set_job_priority` returns, in order:

| arguments | returns |
|---|---|
| `collection_id="pri-col-a", priority=-1, workflow_pk=None` | 2 |
| the same, again | 2 |

Then `claim(engine, "pri-a", limit=1)`, and continue:

| arguments | returns |
|---|---|
| `"pri-col-a", 4, None` | 1 |
| `"pri-col-b", 3, workflow_pk=pk_a` | 0 |
| `"pri-col-b", 3, workflow_pk=pk_b` | 1 |
| `"nosuch", 1, None` | None |
| `"pri-col-a", 1, workflow_pk=999999` | None |

Assert on `select status, priority from jobs`:
- for `pk_a`: the claimed job is −1 and the pending job is 4;
- for `pk_b`: the pending job is 3.

#### 3.3 `tests/test_control_api.py`

- **`test_writes_need_token_events_do_not`:** add `("POST", "/api/admin/job-priority")`
  to `writes`.
- **New `test_job_priority_over_http`.** Setup: `workflow(control_client, "jp")` and
  `sample(control_client, "jp", "SRR7000001")`, then POST `/api/admin/reconcile-jobs`.
  Then, with `TOKEN`:
  - `{"collection_id": "jp", "priority": 5}` returns 200 `{"updated": 1}`, twice.
  - `{"collection_id": "jp", "priority": 5, "workflow_pk": <pk>}` returns 200
    `{"updated": 1}`.
  - `{"collection_id": "nope", "priority": 1}` returns 404 with
    `detail == "Collection 'nope' not found"`.
  - Each of these returns 422 with a string `detail`:
    - `{"collection_id": "jp", "priority": True}`;
    - `{"collection_id": "jp", "priority": "3"}`;
    - `{"priority": 1}`;
    - `{"collection_id": "jp", "priority": 2**31}`.

#### 3.4 `tests/test_v2_import.py`

**New `test_import_and_verify_carry_job_priority(db_url, tmp_path)`**
- `tables = v2_tables()`. Set `"priority"` on every jobs row: −1 for id 5, 0 for the
  rest.
- `snapshot = read_snapshot(write_export(tmp_path, tables, cursor=CURSOR))`.
- `import_and_verify(db_url, snapshot)["clean"]` is true.
- `select priority from jobs where id = 5` is −1.
- Run `update jobs set priority = 0 where id = 5`, then `verify` again:
  - `clean` is false;
  - one failure starts with `tables.jobs`.

Existing tests keep fixture rows without `priority`. They still pass, which shows an
older export imports with the default 0.

#### 3.5 Contract suite

**`contract/schemas.py`:**

```python
class JobPriorityUpdated(Envelope):
    """Zod `JobPriorityUpdated`."""

    updated: int
```

**`contract/test_auth.py`:** add `("POST", "admin/job-priority")` to `calls` in
`test_writes_need_bearer`.

**`contract/test_dispatch_admin.py`** (module already `pytestmark = pytest.mark.writes`):
new `test_job_priority(api, ns)`.

Setup:
- `name = f"{ns}-prio"` and `a, b = f"{name}-a", f"{name}-b"`;
- `wf = register_workflow(api, name, collections=[a, b])`;
- `first = register_sample(api, a)`, then `second = register_sample(api, b)`;
- `reconcile(api)`.

`resp = api.post("admin/job-priority", json=...)` and
`validate(schemas.JobPriorityUpdated, resp).updated`, in order:

| body | result |
|---|---|
| `{"collection_id": b, "priority": 5, "workflow_pk": wf["id"]}` | 1 |
| the same, again | 1 |

Then `batch = claim(api, name, limit=1)`. Assert
`batch["jobs"][0]["sample_id"] == second["readset_id"]`: new registrations are
readset-keyed. Continue:

| body | result |
|---|---|
| `{"collection_id": b, "priority": 5}` | 0 (the job is claimed) |
| `{"collection_id": a, "priority": -1, "workflow_pk": wf["id"]}` | 1 |
| `{"collection_id": f"{ns}-nosuch", "priority": 1}` | `validate(schemas.Detail, resp, 404)` |
| `{"collection_id": a, "priority": "high"}` | `validate(schemas.Detail, resp, 422)` |

Import `register_workflow`, `register_sample` and `reconcile` from `contract.support`.

## Acceptance criteria

- [ ] `POST /api/admin/job-priority` exists in v2 and in the Postgres service with the
      request, responses and status codes in "Contract (both services)", and needs the
      bearer token in both.
- [ ] v2 `jobs` has `priority integer not null default 0` on fresh objects (`SCHEMA`) and
      on existing ones (`addJobPriority`, idempotent).
- [ ] Both v2 `claimBatch` queries order by `j.priority desc` first. The pick keeps
      `workflow_id, workflow_version, created_at, id` as tie-breakers, and the take keeps
      `created_at, id`.
- [ ] Only `pending` jobs change. `updated` is the matched count, and a second identical
      call returns the same value and leaves the same state, in both services.
- [ ] `/admin/export` jobs rows carry `priority`. `v2_import` imports it, and `verify`
      compares it. Exports without the column still import, as 0.
- [ ] `reconcileJobs` and `_RECONCILE_SQL` don't set priority, and each has the
      one-line hook comment.
- [ ] `cf/README.md` and `README.md` list `/admin/job-priority`.
- [ ] All tests in §3 exist and pass:
  - `cd cf && npx vitest run`;
  - `uv run pytest tests/test_control_transitions.py tests/test_control_api.py tests/test_v2_import.py`;
  - `uv run mypy src/nextflow_telemetry`;
  - the contract suite against both `just v2-local` and `just pg-local` (CI
    `contract.yml`, both jobs green).
- [ ] No Alembic migration, no new dependency, no env or config change, and no change in
      `packages/nf_client`, `src/nextflow_telemetry/etl`, `frontend/` or the health
      report.

## Test plan

1. `cd cf && npm ci && npx tsc --noEmit && npx vitest run`. Covers §3.1, plus the
   existing `lifecycle`, `export` and `openapi` suites, which must stay green.
2. `uv run pytest tests/test_control_transitions.py tests/test_control_api.py
   tests/test_v2_import.py` (testcontainers Postgres).
3. `uv run mypy src/nextflow_telemetry`.
4. Run the contract suite against both targets:
   - v2: `just v2-local`, then `just contract`;
   - Postgres: `just pg-local`, then
     `just contract http://127.0.0.1:8000 contract-token ""`.

   CI runs both in `contract.yml`.
5. `uv run pytest`, the full suite, as the regression check.

### Rollout (operator, after merge; not part of the PR)

1. `cd cf && npx wrangler deploy`. Check that the daemons are claiming and that the
   contract suite passes as on 2026-11-01. The first request after deploy runs
   `addJobPriority`.
2. For each of `PRJEB75460`, `PRJEB75462`, `PRJEB75463`, `PRJEB75464` and `PRJEB75465`,
   POST `/api/admin/job-priority` with body
   `{"collection_id": "<id>", "priority": -1, "workflow_pk": 7}`. Record each `updated`.
   They should sum to about 36k, and a 404 means the collection id is wrong.
3. Check that the next claims come from non-ZOE collections: the `collections` field of
   the newest rows in `GET /api/workflows/7/jobs?status=claimed`, or the cohort
   leaderboard.

At the M6/M8 import, the −1 values move to Postgres with the export (§3.4), and Postgres
claims honour them (M3).

## Risks

- **v2 claim cost.** Without an index that leads with priority, each claim sorts every
  pending job in scope: about 55k rows read for the pick and as many for the take.
  - At a few hundred claims a day (daemons don't claim while at their concurrency limit),
    that is roughly 10–30M rows read a day, around 1–4% of the 25-billion-row monthly
    allowance.
  - Each sort adds tens of milliseconds to the request.
  - The `ponytail:` comment names the index to add. v2 is decommissioned at M10.
- **Readset-keyed duplicates.** A readset-keyed registration's job is keyed by the
  earliest sample row that has the readset. If that row is outside the collection and a
  duplicate row with the same readset is inside it, the job isn't matched. This is the
  same ceiling as the `reconcileJobs` ponytail note, and it doesn't happen with the ZOE
  BioProjects.
- **Different tie-breaks across workflows.** Within a version both services order by
  `priority desc, created_at, id`. Across versions:
  - v2 breaks ties by lowest `(workflow_id, workflow_version)`;
  - Postgres breaks them by the oldest job (M3).

  For the ZOE rollout only workflow_pk 7 matters, so the behaviour is the same. Aligning
  the two is not part of this issue.
- **A claim racing the update in v2.** None: the Durable Object is single-threaded, and
  `setJobPriority` runs without an `await`. In Postgres, a claim and the update serialize
  on row locks (§2.2).
- **Retried jobs keep their priority.** A ZOE job swept back to pending stays at −1. That
  is intended, and it matches #373's note.
