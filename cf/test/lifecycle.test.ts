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
import { createExecutionContext, env, runDurableObjectAlarm, runInDurableObject, SELF, waitOnExecutionContext } from "cloudflare:test";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import type { ControlDO } from "../src/control-do";
import worker, { authExempt, resetAllowed } from "../src/index";
import { readsetIdForRuns } from "../src/readset";
import * as S from "../src/schemas";

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

  it("serves a run's nextflow and wrapper logs under v1's nextflow_log sentinel", async () => {
    await env.STORE.put("nextflow-logs/r-logs/nextflow.log", "nf says hi");
    await env.STORE.put("nextflow-logs/r-logs/wrapper_output.log", "OOM");
    const got = (await (await get("/api/task-logs/r-logs/nextflow_log")).json()) as any;
    expect(got.logs.map((l: any) => [l.log_type, l.content])).toEqual([
      ["nextflow_log", "nf says hi"],
      ["wrapper_output_log", "OOM"],
    ]);
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
describe("responses match the published contract", () => {
  // Runs against the corpus the suite above left behind. A handler that
  // changes shape fails here before it fails in the frontend (#184, #173).
  const parse = async (path: string, schema: { parse: (v: unknown) => unknown }) => {
    const res = await SELF.fetch(`https://x${path}`);
    expect(res.status, path).toBe(200);
    schema.parse(await res.json());
  };
  it("stats, workflows, samples, cohorts, runs", async () => {
    await parse("/api/admin/stats", S.Stats);
    await parse("/api/workflows", S.Workflow.array());
    await parse("/api/samples?limit=5", S.SampleList);
    await parse("/api/samples/facets/collections", S.CollectionFacets);
    await parse("/api/cohorts", S.Cohort.array());
    await parse("/api/runs?limit=5", S.RunList);
    await parse("/api/metrics/processes/running", S.RunningProcesses);
    const runs = (await (await SELF.fetch("https://x/api/runs?limit=1")).json()) as { runs: { run_name: string }[] };
    if (runs.runs[0]) await parse(`/api/runs/${runs.runs[0].run_name}`, S.RunDetail);
    const wf = (await (await SELF.fetch("https://x/api/workflows")).json()) as { id: number }[];
    if (wf[0]) await parse(`/api/workflows/${wf[0].id}/job-summary`, S.JobSummary);
  });
});

describe("auth exemptions", () => {
  it("lets the token-less wrapper and weblog through, nothing else that writes", () => {
    expect(authExempt("POST", "/api/runs/r01abc/event")).toBe(true);
    expect(authExempt("POST", "/runs/r01abc/event")).toBe(true);
    expect(authExempt("POST", "/telemetry")).toBe(true);
    expect(authExempt("POST", "/api/task-logs")).toBe(true);
    expect(authExempt("POST", "/task-logs")).toBe(true);
    expect(authExempt("GET", "/api/admin/stats")).toBe(true);
    for (const p of ["/api/dispatch/batch", "/api/samples", "/api/admin/reset", "/api/runs/r01abc/events", "/api/workflows"]) {
      expect(authExempt("POST", p), p).toBe(false);
    }
  });
});

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

describe("production hardening", () => {
  // Calls the Worker with a swapped env so the suite-wide ALLOW_RESET=true is untouched.
  async function withEnv(overrides: Record<string, string | undefined>, path: string, init: RequestInit) {
    const ctx = createExecutionContext();
    const res = await worker.fetch(new Request(`https://x${path}`, init), { ...env, ...overrides }, ctx);
    await waitOnExecutionContext(ctx);
    return res;
  }

  it("POST /admin/reset is 403 when ALLOW_RESET is false or unset, even with a valid token", async () => {
    const init = { method: "POST", headers: { authorization: "Bearer t" } };
    for (const ALLOW_RESET of ["false", undefined]) {
      const res = await withEnv({ API_TOKEN: "t", ALLOW_RESET }, "/admin/reset", init);
      expect(res.status, String(ALLOW_RESET)).toBe(403);
    }
  });

  it("with API_TOKEN set, unauthenticated writes are 401 except /telemetry and the run event", async () => {
    const t = { API_TOKEN: "t" };
    const reconcile = await withEnv(t, "/api/admin/reconcile-jobs", { method: "POST" });
    expect(reconcile.status).toBe(401);

    const telemetry = await withEnv(t, "/telemetry", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ runId: "x", runName: "auth-probe", event: "started", utcTime: new Date().toISOString() }),
    });
    expect(telemetry.status).not.toBe(401);

    const fd = new FormData();
    fd.set("event", JSON.stringify({ utc_time: new Date().toISOString(), event: "x" }));
    const event = await withEnv(t, "/runs/x/event", { method: "POST", body: fd });
    expect(event.status).not.toBe(401);
  });

  async function uploadLog(fields: Record<string, string | File>) {
    const fd = new FormData();
    for (const [k, v] of Object.entries(fields)) fd.set(k, v);
    return SELF.fetch("https://x/api/task-logs", { method: "POST", body: fd });
  }

  it("rejects task logs with a missing, empty or literal-null run_name", async () => {
    const base = { log_type: "command_out", task_hash: "ab/cdef12", content: new File(["hi"], "log.txt") };
    expect((await uploadLog(base)).status).toBe(400);
    expect((await uploadLog({ ...base, run_name: "" })).status).toBe(400);
    expect((await uploadLog({ ...base, run_name: "null" })).status).toBe(400);
    expect((await env.STORE.list({ prefix: "task-logs/null/" })).objects).toHaveLength(0);
  });

  it("strips NUL bytes from uploaded task logs before writing to R2", async () => {
    const res = await uploadLog({
      run_name: "nul-probe",
      log_type: "command_out",
      task_hash: "ab/cdef12",
      content: new File(["he\x00llo\x00"], "log.txt"),
    });
    expect(res.status).toBe(201);
    const obj = await env.STORE.get("task-logs/nul-probe/ab/cdef12/command_out");
    expect(await obj!.text()).toBe("hello");
  });
});

describe("failed runs", () => {
  it("closes a run as failed, with the reason, when nextflow reports success=false", async () => {
    await post("/api/workflows", { ...WF, workflow_id: "failwf" });
    await post("/api/samples", { sample_id: "sampleF", ncbi_accession: "SRR000009", collection: "PRJNA000009" });
    await post("/api/admin/reconcile-jobs", {});
    const batch = (await (await post("/api/dispatch/batch", { limit: 10, workflow_id: "failwf" })).json()) as any;
    await weblog(batch.run_name, "started");
    await weblog(batch.run_name, "completed", {
      metadata: { workflow: { success: false, errorMessage: null, errorReport: "Failed to pull singularity image" } },
    });
    const run = (await (await get(`/api/runs/${batch.run_name}`)).json()) as any;
    expect(run.status).toBe("failed");
    expect(run.slurm_reason).toBe("Failed to pull singularity image");
  });

  it("counts task outcomes per run and lists the failed ones", async () => {
    await post("/api/samples", { sample_id: "sampleG", ncbi_accession: "SRR000010", collection: "PRJNA000009" });
    await post("/api/admin/reconcile-jobs", {});
    const batch = (await (await post("/api/dispatch/batch", { limit: 10, workflow_id: "failwf" })).json()) as any;
    await weblog(batch.run_name, "started");
    const t = { tag: "sampleG", process: "cmgd:kneaddata", hash: "ab/cdef12" };
    await weblog(batch.run_name, "process_completed", { trace: { ...t, status: "FAILED", exit: 137, attempt: 1, error_action: "RETRY" } });
    await weblog(batch.run_name, "process_completed", { trace: { ...t, status: "COMPLETED", exit: 0, attempt: 2 } });
    const run = (await (await get(`/api/runs/${batch.run_name}`)).json()) as any;
    expect(run.task_status_counts).toEqual({ FAILED: 1, COMPLETED: 1 });
    expect(run.failed_tasks).toEqual([
      { process: "cmgd:kneaddata", sample_id: "sampleG", exit_code: "137", task_hash: "ab/cdef12", attempt: 1, error_action: "RETRY" },
    ]);
  });
});

describe("registrations are bundles (ADR-0010)", () => {
  const BUNDLE = {
    ...WF,
    workflow_id: "cmgd_humann4a1",
    version: "2.3.0",
    params: { humann_bundle: "humann4.0.0a1", skip_humann: false },
    collections: ["PILOT1"],
  };
  let bundlePk: number;

  it("adds params and collections to a workflows table created before them", async () => {
    const stub = env.CONTROL.get(env.CONTROL.idFromName("v1"));
    await runInDurableObject(stub, (instance: ControlDO, state) => {
      const sql = state.storage.sql;
      sql.exec(`alter table workflows drop column params`);
      sql.exec(`alter table workflows drop column collections`);
      (instance as any).addWorkflowBundleColumns();
      (instance as any).addWorkflowBundleColumns();
      const rows = sql.exec(`select params, collections from workflows`).toArray();
      expect(rows.length).toBeGreaterThan(0);
      expect(rows.every((r) => r.params === "{}" && r.collections === null)).toBe(true);
    });
  });

  it("round-trips params and collections, and 409s a version re-registered with different params", async () => {
    const res = await post("/api/workflows", BUNDLE);
    expect(res.status).toBe(201);
    const wf = S.Workflow.parse(await res.json());
    bundlePk = wf.id;
    expect(wf.params).toEqual(BUNDLE.params);
    expect(wf.collections).toEqual(["PILOT1"]);

    // Key order is not a difference.
    const same = await post("/api/workflows", { ...BUNDLE, params: { skip_humann: false, humann_bundle: "humann4.0.0a1" } });
    expect(same.status).toBe(201);
    // Re-registering without collections keeps the pilot scope (no silent widening).
    const { collections: _omit, ...noScope } = BUNDLE;
    expect(((await (await post("/api/workflows", noScope)).json()) as any).collections).toEqual(["PILOT1"]);
    const clash = await post("/api/workflows", { ...BUNDLE, params: { ...BUNDLE.params, skip_humann: true } });
    expect(clash.status).toBe(409);
    expect(((await (await get(`/api/workflows/${bundlePk}`)).json()) as any).params).toEqual(BUNDLE.params);

    for (const bad of [{ params: { nested: { a: 1 } } }, { params: ["x"] }, { collections: "PILOT1" }, { collections: [] }]) {
      expect((await post("/api/workflows", { ...BUNDLE, version: "bad", ...bad })).status, JSON.stringify(bad)).toBe(422);
    }
  });

  it("reconciles a collection-scoped registration over its collections only", async () => {
    await post("/api/samples", { sample_id: "pilot1", ncbi_accession: "SRR100001", collection: "PILOT1" });
    await post("/api/samples", { sample_id: "other1", ncbi_accession: "SRR100002", collection: "PRJNA000009" });
    await post("/api/admin/reconcile-jobs", {});
    const s = await summary(bundlePk);
    expect(s.total).toBe(1);
    expect(s.pending).toBe(1);
  });

  it("carries the registration's params on the claimed batch", async () => {
    const res = await post("/api/dispatch/batch", { limit: 10, workflow_id: "cmgd_humann4a1" });
    const batch = (await res.json()) as any;
    expect(batch.params).toEqual(BUNDLE.params);
    expect(batch.jobs.map((j: any) => j.sample_id)).toEqual([await readsetIdForRuns("SRR100001")]);
  });
});

describe("readset ids (ADR-0007)", () => {
  const RS_WF = { ...WF, workflow_id: "cmgd_rs", version: "3.0.0", collections: ["RSPILOT"] };

  it("backfills readset ids and marks every existing registration md5-keyed, idempotently", async () => {
    // The suite reset the corpus above; this is the pre-ADR-0007 state.
    await post("/api/workflows", WF);
    await post("/api/samples", { sample_id: "sampleA", ncbi_accession: "SRR000001" });
    const stub = env.CONTROL.get(env.CONTROL.idFromName("v1"));
    await runInDurableObject(stub, async (instance: ControlDO, state) => {
      const sql = state.storage.sql;
      sql.exec(`drop index samples_readset`);
      sql.exec(`alter table samples drop column readset_id`);
      sql.exec(`alter table workflows drop column sample_key`);
      await (instance as any).addReadsetIds();
      await (instance as any).addReadsetIds();
      const a = sql.exec(`select readset_id from samples where sample_id = 'sampleA'`).one();
      expect(a.readset_id).toBe("RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF"); // ADR-0007 golden: SRR000001
      expect(sql.exec(`select count(*) as n from samples where readset_id is null`).one().n).toBe(0);
      const keys = sql.exec(`select distinct sample_key from workflows`).toArray().map((r) => r.sample_key);
      expect(keys).toEqual(["sample_id"]);
    });
    // Re-registering a pre-existing version keeps its key, and so its output folders.
    const wf = (await (await post("/api/workflows", WF)).json()) as any;
    expect(wf.sample_key).toBe("sample_id");
  });

  it("looks a sample up by either id", async () => {
    const byMd5 = (await (await get("/api/samples/sampleA")).json()) as any;
    expect(byMd5.readset_id).toBe("RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF");
    const byRs = (await (await get("/api/samples/RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF")).json()) as any;
    expect(byRs.sample_id).toBe("sampleA");
    const found = (await (await get("/api/samples?search=29BkNp8wx")).json()) as any;
    expect(found.items.map((s: any) => s.sample_id)).toEqual(["sampleA"]);
  });

  it("hands a readset-keyed registration's pipeline readset ids, one job per readset", async () => {
    const wf = (await (await post("/api/workflows", RS_WF)).json()) as any;
    expect(wf.sample_key).toBe("readset_id");
    // Two md5 ids for one run set (client strings differ, canonical lists don't),
    // and a row with a placeholder that has no readset.
    await post("/api/samples", { sample_id: "rsA", ncbi_accession: "SRR200002;SRR200001", collection: "RSPILOT" });
    await post("/api/samples", { sample_id: "rsA-dup", ncbi_accession: "SRR200001,SRR200002", collection: "RSPILOT" });
    await post("/api/samples", { sample_id: "rsB", ncbi_accession: "n/a;SRR200003", collection: "RSPILOT" });
    await post("/api/admin/reconcile-jobs", {});
    expect((await summary(wf.id)).total).toBe(1);

    const batch = (await (await post("/api/dispatch/batch", { limit: 10, workflow_id: "cmgd_rs" })).json()) as any;
    const rs = await readsetIdForRuns("SRR200001;SRR200002");
    expect(batch.jobs).toEqual([{ sample_id: rs, ncbi_accession: "SRR200001;SRR200002", metadata: {} }]);

    await weblog(batch.run_name, "started");
    await weblog(batch.run_name, "process_completed", {
      trace: { tag: rs, process: "cmgd:MARK_COMPLETE", status: "COMPLETED" },
    });
    expect((await summary(wf.id)).completed).toBe(1);
    // The leaderboard still joins jobs to collections through the md5 key.
    const board = (await (await get("/api/cohorts/leaderboard")).json()) as any[];
    expect(board.find((c) => c.collection_id === "RSPILOT").samples_completed).toBeGreaterThanOrEqual(1);
  });

  it("leaves an md5-keyed registration unchanged: md5 in the batch, md5 MARK_COMPLETE", async () => {
    await post("/api/samples", { sample_id: "md5only", ncbi_accession: "SRR300001" });
    await post("/api/admin/reconcile-jobs", {});
    const batch = (await (await post("/api/dispatch/batch", { limit: 50, workflow_id: "cmgd" })).json()) as any;
    const ids = batch.jobs.map((j: any) => j.sample_id);
    // Every sample, as its md5 id: the duplicate run set and the readset-less row included.
    expect(ids).toContain("md5only");
    expect(ids).toEqual(expect.arrayContaining(["rsA", "rsA-dup", "rsB"]));
    expect(ids.some((id: string) => id.startsWith("RS."))).toBe(false);
    await weblog(batch.run_name, "started");
    await weblog(batch.run_name, "process_completed", {
      trace: { tag: "md5only", process: "cmgd:MARK_COMPLETE", status: "COMPLETED" },
    });
    const run = (await (await get(`/api/runs/${batch.run_name}`)).json()) as any;
    expect(run.job_status_counts.completed).toBe(1);
  });
});
