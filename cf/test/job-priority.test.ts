/**
 * Job priority (#420): claim order by priority, POST /admin/job-priority, and the
 * jobs.priority migration, against the real Worker + Durable Objects (no mocks).
 */
import { env, runInDurableObject, SELF } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";
import type { ControlDO } from "../src/control-do";
import { authExempt } from "../src/index";
import * as S from "../src/schemas";

async function post(path: string, body: unknown) {
  return SELF.fetch(`https://x${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

const STAMP = "2026-01-01T00:00:00.000Z";
let objects = 0;

/** A fresh ControlDO seeded with workflows wf-a (1) and wf-b (2) and the given jobs. */
async function seeded(jobs: { sample: string; wf: 1 | 2; priority: number; created: string }[]) {
  const stub = env.CONTROL.get(env.CONTROL.idFromName(`job-priority-${objects++}`));
  return runInDurableObject(stub, (instance: ControlDO, state) => {
    const sql = state.storage.sql;
    for (const [id, name] of [[1, "wf-a"], [2, "wf-b"]] as const) {
      sql.exec(
        `insert into workflows (id, workflow_id, version, repository_url, revision, status, sample_key, created_at, updated_at)
         values (?, ?, '1', 'u', 'r', 'active', 'sample_id', ?, ?)`, id, name, STAMP, STAMP);
    }
    for (const j of jobs) {
      sql.exec(
        `insert into jobs (sample_id, workflow_pk, workflow_id, workflow_version, status, priority, created_at)
         values (?, ?, ?, '1', 'pending', ?, ?)`, j.sample, j.wf, j.wf === 1 ? "wf-a" : "wf-b", j.priority, j.created);
    }
    return instance;
  }).then(() => stub);
}

describe("claimBatch orders by priority", () => {
  it("claims a priority-0 job ahead of a priority -1 job created earlier", async () => {
    const stub = await seeded([
      { sample: "old", wf: 1, priority: -1, created: "2026-01-01" },
      { sample: "new", wf: 1, priority: 0, created: "2026-01-02" },
    ]);
    const batch = await runInDurableObject(stub, (i: ControlDO) => i.claimBatch(1, ["wf-a"]));
    expect(batch!.jobs.map((j: any) => j.sample_id)).toEqual(["new"]);
  });

  it("picks the version whose best pending job has the highest priority", async () => {
    const stub = await seeded([
      { sample: "a1", wf: 1, priority: -1, created: "2026-01-01" },
      { sample: "b1", wf: 2, priority: 0, created: "2026-01-02" },
    ]);
    const batch = await runInDurableObject(stub, (i: ControlDO) => i.claimBatch(10));
    expect(batch!.workflow_id).toBe("wf-b");
    expect(batch!.jobs.map((j: any) => j.sample_id)).toEqual(["b1"]);
  });

  it("breaks equal-priority ties by workflow id, as before", async () => {
    const stub = await seeded([
      { sample: "a1", wf: 1, priority: 0, created: "2026-01-02" },
      { sample: "b1", wf: 2, priority: 0, created: "2026-01-01" },
    ]);
    const batch = await runInDurableObject(stub, (i: ControlDO) => i.claimBatch(10));
    expect(batch!.workflow_id).toBe("wf-a");
  });
});

describe("POST /admin/job-priority", () => {
  let p1: number;
  let p2: number;

  const stub = () => env.CONTROL.get(env.CONTROL.idFromName("v1"));
  const priorities = () =>
    runInDurableObject(stub(), (_i: ControlDO, state) =>
      Object.fromEntries(
        state.storage.sql.exec(`select sample_id, workflow_pk, priority from jobs`).toArray()
          .map((r: any) => [`${r.sample_id}/${r.workflow_pk}`, r.priority]),
      ));
  const setPriority = (body: unknown) => post("/api/admin/job-priority", body);

  beforeAll(async () => {
    await post("/api/admin/reset", {});
    p1 = ((await (await post("/api/workflows", {
      workflow_id: "prio-a", version: "1", repository_url: "u", revision: "r", collections: ["PZ", "PC"],
    })).json()) as any).id;
    p2 = ((await (await post("/api/workflows", {
      workflow_id: "prio-b", version: "1", repository_url: "u", revision: "r", collections: ["PC"],
    })).json()) as any).id;
    await post("/api/samples", { sample_id: "Z", ncbi_accession: "SRR100001", collection: "PZ" });
    await post("/api/samples", { sample_id: "C", ncbi_accession: "SRR100002", collection: "PC" });
    await post("/api/admin/reconcile-jobs", {});
  });

  it("updates only pending members of the collection, optionally one registration", async () => {
    let res = await setPriority({ collection_id: "PC", priority: 2 });
    expect(res.status).toBe(200);
    expect(S.JobPriorityUpdated.parse(await res.json())).toEqual({ updated: 2 });
    expect(await priorities()).toEqual({ [`C/${p1}`]: 2, [`C/${p2}`]: 2, [`Z/${p1}`]: 0 });

    res = await setPriority({ collection_id: "PC", priority: 3, workflow_pk: p2 });
    expect(await res.json()).toEqual({ updated: 1 });
    expect(await priorities()).toMatchObject({ [`C/${p2}`]: 3, [`C/${p1}`]: 2 });

    const claim = (await (await post("/api/dispatch/batch", { limit: 10, workflow_id: "prio-b" })).json()) as any;
    expect(claim.jobs).toHaveLength(1); // C/P2; registrations are readset-keyed, so the id is not "C"

    res = await setPriority({ collection_id: "PC", priority: 9 });
    expect(await res.json()).toEqual({ updated: 1 });
    expect(await priorities()).toMatchObject({ [`C/${p1}`]: 9, [`C/${p2}`]: 3 });
  });

  it("gives the same result when run twice", async () => {
    const body = { collection_id: "PZ", priority: -1 };
    const first = await setPriority(body);
    expect(await first.json()).toEqual({ updated: 1 });
    const after = await priorities();
    const second = await setPriority(body);
    expect(await second.json()).toEqual({ updated: 1 });
    expect(await priorities()).toEqual(after);
  });

  it("404s an unknown collection or workflow and 422s a bad body", async () => {
    let res = await setPriority({ collection_id: "NOPE", priority: 1 });
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ detail: "Collection 'NOPE' not found" });
    res = await setPriority({ collection_id: "PZ", priority: 1, workflow_pk: 999999 });
    expect(res.status).toBe(404);

    for (const bad of [
      { priority: 1 },
      { collection_id: "PZ", priority: "high" },
      { collection_id: "PZ", priority: 1.5 },
      { collection_id: "PZ", priority: true },
    ]) {
      res = await setPriority(bad);
      expect(res.status, JSON.stringify(bad)).toBe(422);
      expect(typeof ((await res.json()) as any).detail).toBe("string");
    }
    expect(authExempt("POST", "/api/admin/job-priority")).toBe(false);
  });

  it("adds jobs.priority to a jobs table created before it", async () => {
    await runInDurableObject(stub(), (instance: ControlDO, state) => {
      const sql = state.storage.sql;
      sql.exec(`alter table jobs drop column priority`);
      (instance as any).addJobPriority();
      (instance as any).addJobPriority();
      const rows = sql.exec(`select priority from jobs`).toArray();
      expect(rows.length).toBeGreaterThan(0);
      for (const r of rows) expect(r.priority).toBe(0);
      const col = sql.exec(`select "notnull" as nn, dflt_value from pragma_table_info('jobs') where name = 'priority'`).one();
      expect(col).toEqual({ nn: 1, dflt_value: "0" });
    });
  });
});
