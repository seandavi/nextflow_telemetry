/**
 * The job lifecycle, end to end, against the real Worker + real Durable
 * Objects (miniflare, no mocks):
 *
 *   pending -> claimed -> submitted -> running -> completed
 *                                            \-> swept -> retry -> dead letter
 *
 * plus the two things that used to need cron sweepers and now hang off DO
 * alarms: claim expiry and heartbeat death.
 */
import { env, runDurableObjectAlarm, runInDurableObject, SELF } from "cloudflare:test";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import type { ControlDO } from "../src/control-do";
import { resetAllowed } from "../src/index";

const WF = {
  workflow_id: "cmgd",
  version: "9.9.9",
  repository_url: "https://github.com/seandavi/cmgd_nextflow",
  revision: "main",
  max_retries: 1,
};

async function post(path: string, body: unknown, init: RequestInit = {}) {
  return SELF.fetch(`https://x${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    ...init,
  });
}

async function get(path: string) {
  return SELF.fetch(`https://x${path}`);
}

/** POST a run-lifecycle event exactly the way run_wrapper does: multipart. */
async function runEvent(runName: string, event: Record<string, unknown>) {
  const fd = new FormData();
  fd.set("event", JSON.stringify({ utc_time: new Date().toISOString(), ...event }));
  return SELF.fetch(`https://x/api/runs/${runName}/event`, { method: "POST", body: fd });
}

async function weblog(runName: string, event: string, extra: Record<string, unknown> = {}) {
  return post("/telemetry", {
    runId: "nf-uuid-1",
    runName,
    event,
    utcTime: new Date().toISOString(),
    ...extra,
  });
}

async function claim(limit = 10) {
  const res = await post("/api/dispatch/batch", { limit });
  return res.status === 204 ? null : ((await res.json()) as any);
}

async function summary(pk: number) {
  return (await (await get(`/api/workflows/${pk}/job-summary`)).json()) as any;
}

let pk: number;

/**
 * The job_counts table is only worth having if it can never disagree with the
 * jobs it counts. Every test in this file churns job status — claim, sweep,
 * expire, dead-letter, requeue — so re-deriving the counts after each one is
 * the check that the triggers cover every write path.
 */
afterEach(async () => {
  const stub = env.CONTROL.get(env.CONTROL.idFromName("v1"));
  await runInDurableObject(stub, (_instance: ControlDO, state) => {
    const sql = state.storage.sql;
    const truth = sql
      .exec(`select workflow_pk, status, count(*) as n from jobs group by workflow_pk, status`)
      .toArray();
    const counters = new Map(
      sql
        .exec(`select workflow_pk, status, n from job_counts where n > 0`)
        .toArray()
        .map((r: any) => [`${r.workflow_pk}:${r.status}`, r.n]),
    );
    for (const r of truth as any[]) {
      expect(counters.get(`${r.workflow_pk}:${r.status}`), `${r.workflow_pk}:${r.status}`).toBe(r.n);
    }
    expect(counters.size).toBe(truth.length);
  });
});

beforeAll(async () => {
  const wf = (await (await post("/api/workflows", WF)).json()) as any;
  pk = wf.id;
  for (const [sample_id, srr] of [
    ["sampleA", "SRR000001"],
    ["sampleB", "SRR000002"],
    ["sampleC", "SRR000003"],
  ]) {
    await post("/api/samples", { sample_id, ncbi_accession: srr, collection: "PRJNA000001" });
  }
});

describe("catalog", () => {
  it("reconciles the samples x active-workflows cross product, idempotently", async () => {
    const first = (await (await post("/api/admin/reconcile-jobs", {})).json()) as any;
    expect(first.jobs_created).toBe(3);
    const second = (await (await post("/api/admin/reconcile-jobs", {})).json()) as any;
    expect(second.jobs_created).toBe(0);
    expect((await summary(pk)).pending).toBe(3);
  });

  it("content-addresses samples and exposes them by SRR and collection", async () => {
    const s = (await (await get("/api/samples/by-srr/SRR000002")).json()) as any;
    expect(s.sample_id).toBe("sampleB");
    expect(s.collections).toEqual(["PRJNA000001"]);
    const facets = (await (await get("/api/samples/facets/collections")).json()) as any;
    expect(facets.collections[0]).toEqual({ collection: "PRJNA000001", count: 3 });
  });
});

describe("happy path", () => {
  it("claims, submits, runs, completes one sample and sweeps the rest", async () => {
    const batch = await claim(2);
    expect(batch.jobs).toHaveLength(2);
    expect(batch.workflow_id).toBe("cmgd");
    expect(batch.repository_url).toBe(WF.repository_url);
    expect((await summary(pk)).claimed).toBe(2);

    expect((await post("/api/dispatch/submitted", { run_name: batch.run_name })).status).toBe(200);
    expect((await summary(pk)).submitted).toBe(2);

    await weblog(batch.run_name, "started");
    expect((await summary(pk)).running).toBe(2);

    const done = batch.jobs[0].sample_id;
    await weblog(batch.run_name, "process_completed", {
      trace: { tag: done, process: "cmgd:MARK_COMPLETE", status: "COMPLETED" },
    });
    expect((await summary(pk)).completed).toBe(1);

    // Run ends without MARK_COMPLETE for the second sample: within retry
    // budget, so it goes back to pending rather than failing.
    await weblog(batch.run_name, "completed");
    const s = await summary(pk);
    expect(s.completed).toBe(1);
    expect(s.running).toBe(0);
    expect(s.pending).toBe(2);

    const run = (await (await get(`/api/runs/${batch.run_name}`)).json()) as any;
    expect(run.status).toBe("completed");
  });

  it("never lets a late MARK_COMPLETE flip a closed job", async () => {
    const before = await summary(pk);
    const runs = (await (await get("/api/runs")).json()) as any;
    const closed = runs.runs[0].run_name;
    await weblog(closed, "process_completed", {
      trace: { tag: "sampleA", process: "MARK_COMPLETE", status: "COMPLETED" },
    });
    expect((await summary(pk)).completed).toBe(before.completed);
  });
});

describe("timers replace sweepers", () => {
  it("expires an unconfirmed claim and returns its jobs to pending", async () => {
    const batch = await claim(1);
    expect((await summary(pk)).claimed).toBe(1);

    // No /dispatch/submitted follows. Fire the claim-TTL alarm the DO armed.
    const stub = env.RUN.get(env.RUN.idFromName(batch.run_name));
    expect(await runDurableObjectAlarm(stub)).toBe(true);

    const s = await summary(pk);
    expect(s.claimed).toBe(0);
    expect(s.pending).toBe(2);
    const run = (await (await get(`/api/runs/${batch.run_name}`)).json()) as any;
    expect(run.status).toBe("expired");
    // An expired claim burns no retry budget — nothing was attempted.
    expect(run.job_status_counts).toEqual({});
  });

  it("closes a run whose heartbeats stop, and dead-letters at retry budget", async () => {
    const batch = await claim(1);
    await post("/api/dispatch/submitted", { run_name: batch.run_name });
    await weblog(batch.run_name, "started");
    expect((await runEvent(batch.run_name, { type: "heartbeat" })).status).toBe(201);

    // Wrapper dies. The liveness alarm — not a cron scan — closes the run.
    const stub = env.RUN.get(env.RUN.idFromName(batch.run_name));
    expect(await runDurableObjectAlarm(stub)).toBe(true);

    const run = (await (await get(`/api/runs/${batch.run_name}`)).json()) as any;
    expect(run.status).toBe("failed");
    expect(run.slurm_reason).toMatch(/presumed dead/);

    // max_retries is 1 and this sample already burned its retry above, so it
    // lands in the dead-letter queue instead of going back to pending.
    const s = await summary(pk);
    expect(s.failed + s.dead_letter).toBeGreaterThan(0);
    expect(s.dead_letter).toBe(1);

    const requeued = (await (await post("/api/admin/requeue-dead-letter", {})).json()) as any;
    expect(requeued.requeued).toBe(1);
    expect((await summary(pk)).dead_letter).toBe(1); // row stays, marked resolved
    expect((await summary(pk)).failed).toBe(0);
  });
});

describe("wire compatibility", () => {
  it("204s an empty claim and no-ops the deprecated sweeper endpoints", async () => {
    await post(`/api/workflows/${pk}/status`, { status: "paused" }, { method: "PATCH" });
    expect((await post("/api/dispatch/batch", { limit: 5 })).status).toBe(204);
    await post(`/api/workflows/${pk}/status`, { status: "active" }, { method: "PATCH" });

    const requeue = (await (await post("/api/dispatch/requeue-expired", {})).json()) as any;
    expect(requeue).toEqual({ requeued_runs: 0 });
  });

  it("serves the same paths with and without the /api prefix", async () => {
    expect((await get("/health")).status).toBe(200);
    expect((await get("/api/health")).status).toBe(200);
    const stats = (await (await get("/api/admin/stats")).json()) as any;
    expect(stats.samples).toBe(3);
    expect(stats.workflows).toBe(1);
  });

  it("stores task logs in R2 and reads them back in v1's shape", async () => {
    const fd = new FormData();
    fd.set("run_name", "r-test");
    fd.set("task_hash", "ab/cdef1234567890");
    fd.set("log_type", "command_err");
    fd.set("content", new File(["boom"], "content", { type: "text/plain" }));
    expect((await SELF.fetch("https://x/api/task-logs", { method: "POST", body: fd })).status).toBe(201);

    const got = (await (await get("/api/task-logs/r-test/ab/cdef12")).json()) as any;
    expect(got.logs).toHaveLength(1);
    expect(got.logs[0].content).toBe("boom");
    expect(got.logs[0].log_type).toBe("command_err");
  });

  it("registers daemons and reports undispatchable work", async () => {
    await SELF.fetch("https://x/api/daemons/heartbeat", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        agent_id: "login07:cmgd",
        hostname: "login07",
        workflow_id: "cmgd",
        mode: "slurm",
        batch_size: 10,
      }),
    });
    const daemons = (await (await get("/api/daemons")).json()) as any[];
    expect(daemons[0].is_active).toBe(true);

    const d = (await (await get("/api/admin/dispatchability")).json()) as any;
    expect(d.active_daemons).toBe(1);
    expect(d.stuck).toEqual([]);
  });

  it("returns cohort completion from live counters", async () => {
    const board = (await (await get("/api/cohorts/leaderboard")).json()) as any[];
    expect(board[0].collection_id).toBe("PRJNA000001");
    expect(board[0].sample_count).toBe(3);
    expect(board[0].samples_completed).toBe(1);
  });
});

// Destructive — empties the jobs table, so it runs last.
describe("retired-version archival", () => {
  it("moves a retired version's jobs to R2 and reclaims the space", async () => {
    const before = (await (await get("/api/admin/stats")).json()) as any;
    const liveJobs = Object.values(before.jobs_by_status as Record<string, number>).reduce(
      (a, b) => a + b,
      0,
    );
    expect(liveJobs).toBe(3);
    expect(before.storage.bytes).toBeGreaterThan(0);

    await post(`/api/workflows/${pk}/status`, { status: "retired" }, { method: "PATCH" });
    const res = (await (await post("/api/admin/archive-retired", {})).json()) as any;
    expect(res).toEqual({ workflows_archived: 1, jobs_archived: expect.any(Number) });

    // Live state is empty; the counters went with it.
    const after = (await (await get("/api/admin/stats")).json()) as any;
    expect(after.jobs_by_status).toEqual({});
    expect((await summary(pk)).total).toBe(0);

    // ...and the rows are on R2, one gzipped NDJSON object under the workflow.
    const listed = await env.STORE.list({ prefix: `archive/jobs/${pk}/` });
    expect(listed.objects).toHaveLength(1);
    const obj = await env.STORE.get(listed.objects[0].key);
    const text = await new Response(
      obj!.body!.pipeThrough(new DecompressionStream("gzip")),
    ).text();
    const lines = text.trim().split("\n").map((l) => JSON.parse(l));
    expect(lines.filter((l) => l.sample_id && l.workflow_pk === pk).length).toBeGreaterThan(0);
    expect(lines.some((l) => l.sample_id === "sampleA" && l.status === "completed")).toBe(true);
  });

  it("leaves a retired version alone while its runs are still in flight", async () => {
    await post("/api/workflows", { ...WF, version: "9.9.10" }, {});
    const wf2 = ((await (await get("/api/workflows")).json()) as any[]).find(
      (w) => w.version === "9.9.10",
    );
    await post("/api/admin/reconcile-jobs", {});
    const batch = await claim(1);
    expect(batch.workflow_version).toBe("9.9.10");

    await post(`/api/workflows/${wf2.id}/status`, { status: "retired" }, { method: "PATCH" });
    const res = (await (await post("/api/admin/archive-retired", {})).json()) as any;
    expect(res.workflows_archived).toBe(0);

    // Once the claim expires the job is pending again — no longer in flight —
    // and the next sweep takes it. Only one job is left to archive: retiring
    // already deleted the two *pending* jobs (v1 semantics, so a retired
    // version stops dispatching immediately), and archival exists for the rest.
    await runDurableObjectAlarm(env.RUN.get(env.RUN.idFromName(batch.run_name)));
    const second = (await (await post("/api/admin/archive-retired", {})).json()) as any;
    expect(second.workflows_archived).toBe(1);
    expect(second.jobs_archived).toBe(1);
  });
});

// Destructive by definition — must be the last describe in the file.
describe("reset", () => {
  it("fails closed on anything but the exact string true", () => {
    expect(resetAllowed({ ALLOW_RESET: "true" })).toBe(true);
    for (const v of ["false", "TRUE", "1", "yes", "", undefined]) {
      expect(resetAllowed({ ALLOW_RESET: v as string | undefined })).toBe(false);
    }
  });

  it("empties every object and the R2 prefixes", async () => {
    // Leave something in each store first, so an empty result afterwards means
    // "cleared" rather than "was never populated".
    await post("/api/samples", { sample_id: "resetme", ncbi_accession: "SRR999999" });
    await weblog("reset-probe", "process_started", {
      trace: { tag: "resetme", process: "RESET_PROBE", status: "RUNNING" },
    });
    expect(((await (await get("/api/admin/stats")).json()) as any).samples).toBeGreaterThan(0);
    expect(((await (await get("/api/metrics/processes/running")).json()) as any).by_process.length)
      .toBeGreaterThan(0);

    const res = (await (await post("/api/admin/reset", {})).json()) as any;
    expect(res.cleared.samples).toBeGreaterThan(0);

    const stats = (await (await get("/api/admin/stats")).json()) as any;
    expect(stats.samples).toBe(0);
    expect(stats.workflows).toBe(0);
    expect(stats.jobs_by_status).toEqual({});
    expect(stats.runs_by_status).toEqual({});
    expect(stats.dead_letter_unresolved).toBe(0);

    // The phantom in-flight counters go too — they are cumulative, so test
    // traffic would otherwise leave permanent residue in the live metrics.
    const running = (await (await get("/api/metrics/processes/running")).json()) as any;
    expect(running.by_process).toEqual([]);
    expect(running.total_running).toBe(0);

    for (const prefix of ["ledger/", "telemetry/", "archive/", "task-logs/", "nextflow-logs/"]) {
      const listed = await env.STORE.list({ prefix });
      expect(listed.objects, `R2 prefix ${prefix}`).toHaveLength(0);
    }
  });
});
