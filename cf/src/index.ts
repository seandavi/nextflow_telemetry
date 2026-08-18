/**
 * API Worker — the v1 wire protocol, unchanged.
 *
 * Every path, method, status code and payload shape below is what
 * `nextflow_telemetry` v1 served, because the things calling them (nf-client,
 * the SLURM run wrapper, Nextflow's `-with-weblog` reporter, the dashboard)
 * are the expensive things to change. Only what happens behind the endpoint is
 * different: ControlDO instead of Postgres, DO alarms instead of cron sweepers,
 * R2 instead of blob columns.
 *
 * v1 mounted most routers under /api and kept /telemetry and /health at the
 * root, so the whole router is mounted at both prefixes here.
 */
import { Hono } from "hono";
import { cors } from "hono/cors";
import { ControlDO } from "./control-do";
import { RunDO } from "./run-do";
import { SinkDO, type TelemetryEnvelope } from "./sink-do";
import { control, runDo, sink, type Env } from "./types";
import { gzip } from "./util";

export { ControlDO, RunDO, SinkDO };

const NEXTFLOW_LOG_MAX = 16 * 1024 * 1024;
const WRAPPER_LOG_MAX = 4 * 1024 * 1024;
const TASK_LOG_MAX = 5 * 1024 * 1024;
const VALID_LOG_TYPES = new Set(["command_sh", "command_out", "command_err"]);

/** Everything this service writes to R2. Reset clears these and nothing else. */
const R2_PREFIXES = [
  "ledger/",
  "telemetry/",
  "archive/",
  "snapshots/",
  "nextflow-logs/",
  "task-logs/",
];

/** Fails closed: only the exact string "true" enables the reset route. */
export function resetAllowed(env: Pick<Env, "ALLOW_RESET">): boolean {
  return env.ALLOW_RESET === "true";
}

const api = new Hono<{ Bindings: Env }>();

// ====================================================================
// dispatch
// ====================================================================

api.post("/dispatch/batch", async (c) => {
  const body = await json(c);
  const limit = clamp(num(body.limit, 50), 1, 500);
  const workflowIds = body.workflow_id == null ? null : ([] as string[]).concat(body.workflow_id);

  const batch = await control(c.env).claimBatch(limit, workflowIds, body.workflow_version ?? null);
  if (!batch) return c.body(null, 204);

  // The claim TTL is a timer on the run itself, not a row for a sweeper to
  // find later. Unconfirmed claims expire on their own.
  await runDo(c.env, batch.run_name).arm(batch.run_name, "claim", mins(c.env.CLAIM_TTL_MINUTES, 5));
  return c.json(batch);
});

api.post("/dispatch/submitted", async (c) => {
  const body = await json(c);
  const runName = String(body.run_name ?? "");
  const ok = await control(c.env).markSubmitted(runName, body.executor_job_id ?? null);
  if (!ok) return c.json({ detail: `No claimed run with name '${runName}' found` }, 404);

  // Queue wait is unbounded in practice (maintenance reservations park jobs for
  // the better part of a day), so this deadline is a backstop, not a policy.
  await runDo(c.env, runName).arm(runName, "backstop", hours(c.env.SUBMIT_BACKSTOP_HOURS, 48));
  return c.json({ run_name: runName, status: "submitted" });
});

api.post("/dispatch/requeue-expired", (c) => {
  c.header("Deprecation", "true");
  c.header("Link", '</docs/v2.md#claim-ttl>; rel="deprecation"');
  return c.json({ requeued_runs: 0 });
});

// ====================================================================
// telemetry (Nextflow weblog) — intentionally unauthenticated: the weblog
// reporter has no way to send a bearer token.
// ====================================================================

api.post("/telemetry", async (c) => {
  const body = await json(c);
  stripZoneIds(body?.metadata);

  const runName = String(body.runName ?? body.run_name ?? "");
  const event = String(body.event ?? "");
  const trace = body.trace && typeof body.trace === "object" ? body.trace : null;
  // The pipeline tags every process with a bare "${meta.sample}"; the
  // historical "sample_id:run_name" form is tolerated by taking the head.
  const sampleId = trace?.tag ? String(trace.tag).split(":")[0] : null;
  const ctl = control(c.env);

  if (event === "started") {
    await ctl.markRunning(runName, body.runId ?? body.run_id ?? null);
  } else if (
    event === "process_completed" &&
    sampleId &&
    String(trace?.process ?? "").endsWith("MARK_COMPLETE") &&
    trace?.status === "COMPLETED"
  ) {
    // Marked complete the moment the sentinel lands, not at run close, so
    // dashboards move in real time.
    await ctl.completeSample(runName, sampleId);
  } else if (event === "completed") {
    await ctl.closeRun(runName, "completed", null);
    await runDo(c.env, runName).finalize();
  }

  await sink(c.env).write({
    run_name: runName,
    event,
    utc_time: body.utcTime ?? body.timestamp ?? null,
    source: "weblog",
    run_id: body.runId ?? null,
    sample_id: sampleId,
    process: trace?.process ?? null,
    payload: body,
  } satisfies TelemetryEnvelope);

  return c.json(body);
});

// ====================================================================
// run lifecycle events (wrapper / pipeline hooks / daemon)
// ====================================================================

api.post("/runs/:run_name/event", async (c) => {
  const runName = c.req.param("run_name");
  const form = await c.req.formData().catch(() => null);
  if (!form) return c.json({ detail: "expected multipart form data" }, 422);

  let ev: any;
  try {
    ev = JSON.parse(String(form.get("event") ?? ""));
  } catch (e) {
    return c.json({ detail: `event field is not valid JSON: ${e}` }, 422);
  }
  if (!ev?.type) return c.json({ detail: "event.type is required" }, 422);

  const nextflowLog: unknown = form.get("nextflow_log");
  const wrapperLog: unknown = form.get("wrapper_output_log");
  const attachments = [nextflowLog, wrapperLog].filter((f): f is File => f instanceof File);

  if (attachments.length && ev.type !== "wrapper_exited") {
    return c.json(
      { detail: `log attachments are only valid on wrapper_exited events; received type=${ev.type}.` },
      422,
    );
  }
  const ctl = control(c.env);
  if (attachments.length && (await ctl.getRunStatus(runName)) === null) {
    return c.json({ detail: `No run '${runName}'; refusing to store attachments as orphan.` }, 404);
  }
  if (nextflowLog instanceof File && nextflowLog.size > NEXTFLOW_LOG_MAX) {
    return c.json({ detail: `nextflow_log exceeds ${NEXTFLOW_LOG_MAX} byte limit.` }, 413);
  }
  if (wrapperLog instanceof File && wrapperLog.size > WRAPPER_LOG_MAX) {
    return c.json({ detail: `wrapper_output_log exceeds ${WRAPPER_LOG_MAX} byte limit.` }, 413);
  }

  const now = new Date().toISOString();
  let logUploaded = false;
  let wrapperUploaded = false;
  if (nextflowLog instanceof File) {
    await c.env.STORE.put(`nextflow-logs/${runName}/nextflow.log`, await nextflowLog.arrayBuffer());
    await ctl.markLogUploaded(runName, now);
    logUploaded = true;
  }
  if (wrapperLog instanceof File) {
    await c.env.STORE.put(`nextflow-logs/${runName}/wrapper_output.log`, await wrapperLog.arrayBuffer());
    wrapperUploaded = true;
  }

  await ctl.applyRunEvent(runName, ev, now);

  if (ev.type === "heartbeat" || ev.type === "wrapper_started") {
    // Any sign of life pushes the liveness deadline out. Nothing else needs to
    // happen: the absence of this call is what eventually closes the run.
    await runDo(c.env, runName).heartbeat(runName, mins(c.env.LIVENESS_MINUTES, 10));
  } else if (ev.type === "wrapper_exited") {
    // The driver is gone and we know its exit status — no reason to wait out
    // the liveness window. closeRun is idempotent, so a `completed` weblog
    // that already closed this run wins and this is a no-op.
    await ctl.closeRun(
      runName,
      ev.exit_code === 0 ? "completed" : "failed",
      ev.exit_code === 0 ? null : `wrapper exited ${ev.exit_code}`,
    );
    await runDo(c.env, runName).finalize();
  }

  await sink(c.env).write({
    run_name: runName,
    event: `run_${ev.type}`,
    utc_time: ev.utc_time ?? null,
    source: "run_event",
    payload: ev,
  } satisfies TelemetryEnvelope);

  return c.json(
    {
      run_name: runName,
      type: ev.type,
      nextflow_log_uploaded: logUploaded,
      wrapper_output_log_uploaded: wrapperUploaded,
    },
    201,
  );
});

api.get("/runs", async (c) => {
  const limit = clamp(num(c.req.query("limit"), 50), 1, 500);
  const offset = Math.max(0, num(c.req.query("offset"), 0));
  const res = await control(c.env).listRuns({
    status: c.req.query("status") ?? null,
    workflow_id: c.req.query("workflow_id") ?? null,
    limit,
    offset,
  });
  return c.json({ ...res, limit, offset });
});

api.get("/runs/:run_name", async (c) => {
  const runName = c.req.param("run_name");
  const row = (await control(c.env).getRun(runName)) as Record<string, unknown> | null;
  if (!row) return c.json({ detail: `No workflow run with name '${runName}'` }, 404);
  const logs = await c.env.STORE.list({ prefix: `nextflow-logs/${runName}/` });
  return c.json({
    ...row,
    nextflow_log_available: logs.objects.some((o) => o.key.endsWith("/nextflow.log")),
    wrapper_output_log_available: logs.objects.some((o) => o.key.endsWith("/wrapper_output.log")),
  });
});

// ====================================================================
// samples
// ====================================================================

api.post("/samples", async (c) => {
  const body = await json(c);
  if (!body.sample_id || !body.ncbi_accession) {
    return c.json({ detail: "sample_id and ncbi_accession are required" }, 422);
  }
  return c.json(await control(c.env).registerSample(body), 201);
});

api.get("/samples", async (c) => {
  const limit = clamp(num(c.req.query("limit"), 100), 1, 1000);
  const offset = Math.max(0, num(c.req.query("offset"), 0));
  const res = await control(c.env).listSamples({
    limit,
    offset,
    search: c.req.query("search") ?? null,
    collection: c.req.query("collection") ?? null,
  });
  return c.json({ ...res, limit, offset });
});

api.get("/samples/facets/collections", async (c) => c.json(await control(c.env).collectionFacets()));

api.get("/samples/by-srr/:srr", async (c) => {
  const row = await control(c.env).getSampleBySrr(c.req.param("srr"));
  return row ? c.json(row) : c.json({ detail: `No sample found with SRR '${c.req.param("srr")}'` }, 404);
});

api.get("/samples/by-biosample/:biosample_id", async (c) =>
  c.json(await control(c.env).getSamplesByBiosample(c.req.param("biosample_id"))),
);

api.get("/samples/:sample_id", async (c) => {
  const row = await control(c.env).getSample(c.req.param("sample_id"));
  return row ? c.json(row) : c.json({ detail: `Sample '${c.req.param("sample_id")}' not found` }, 404);
});

// ====================================================================
// workflows
// ====================================================================

api.post("/workflows", async (c) => {
  const body = await json(c);
  for (const k of ["workflow_id", "version", "repository_url", "revision"]) {
    if (!body[k]) return c.json({ detail: `${k} is required` }, 422);
  }
  return c.json(await control(c.env).registerWorkflow(body), 201);
});

api.get("/workflows", async (c) => c.json(await control(c.env).listWorkflows(c.req.query("status") ?? null)));

api.get("/workflows/:pk{[0-9]+}", async (c) => {
  const row = await control(c.env).getWorkflow(Number(c.req.param("pk")));
  return row ? c.json(row) : notFoundWorkflow(c);
});

api.patch("/workflows/:pk{[0-9]+}/status", async (c) => {
  const { status } = await json(c);
  if (!["active", "paused", "retired"].includes(status)) {
    return c.json({ detail: "status must be one of: active, paused, retired" }, 422);
  }
  const row = await control(c.env).updateWorkflowStatus(Number(c.req.param("pk")), status);
  return row ? c.json(row) : notFoundWorkflow(c);
});

api.patch("/workflows/:pk{[0-9]+}/revision", async (c) => {
  const { revision } = await json(c);
  if (!revision) return c.json({ detail: "revision is required" }, 422);
  const row = await control(c.env).updateWorkflowRevision(Number(c.req.param("pk")), revision);
  return row ? c.json(row) : notFoundWorkflow(c);
});

api.get("/workflows/:pk{[0-9]+}/job-summary", async (c) => {
  const row = await control(c.env).jobSummary(Number(c.req.param("pk")));
  return row ? c.json(row) : notFoundWorkflow(c);
});

// ====================================================================
// task logs — blobs live in R2, never in the control plane
// ====================================================================

api.post("/task-logs", async (c) => {
  const form = await c.req.formData().catch(() => null);
  if (!form) return c.json({ detail: "expected multipart form data" }, 422);
  const runName = String(form.get("run_name") ?? "");
  const logType = String(form.get("log_type") ?? "");
  const content: unknown = form.get("content");
  if (!VALID_LOG_TYPES.has(logType)) {
    return c.json({ detail: `log_type must be one of: ${[...VALID_LOG_TYPES].sort()}` }, 422);
  }
  if (!(content instanceof File)) return c.json({ detail: "content file is required" }, 422);
  if (content.size > TASK_LOG_MAX) return c.json({ detail: "Content exceeds size limit." }, 413);

  // Normalise to Nextflow's short work-dir hash (ab/cdef12) so it matches the
  // hash carried in trace events; the afterScript derives it from the full path.
  const taskHash = shortHash(String(form.get("task_hash") ?? ""));
  const text = await content.text();
  const uploadedAt = new Date().toISOString();
  const key = `task-logs/${runName}/${taskHash}/${logType}`;
  await c.env.STORE.put(key, text, { customMetadata: { uploaded_at: uploadedAt } });

  return c.json(
    { id: keyId(key), run_name: runName, task_hash: taskHash, log_type: logType, content: text, uploaded_at: uploadedAt },
    201,
  );
});

api.get("/task-logs/:run_name/:task_hash{.+}", async (c) => {
  const runName = c.req.param("run_name");
  const taskHash = c.req.param("task_hash");
  const listed = await c.env.STORE.list({ prefix: `task-logs/${runName}/${taskHash}/` });
  const logs = [];
  for (const o of listed.objects.sort((a, b) => a.key.localeCompare(b.key))) {
    const obj = await c.env.STORE.get(o.key);
    if (!obj) continue;
    logs.push({
      id: keyId(o.key),
      run_name: runName,
      task_hash: taskHash,
      log_type: o.key.split("/").pop(),
      content: await obj.text(),
      uploaded_at: obj.customMetadata?.uploaded_at ?? obj.uploaded.toISOString(),
    });
  }
  return c.json({ run_name: runName, task_hash: taskHash, logs });
});

// ====================================================================
// daemons
// ====================================================================

api.put("/daemons/heartbeat", async (c) => c.json(await control(c.env).daemonHeartbeat(await json(c))));

api.get("/daemons", async (c) =>
  c.json(await control(c.env).listDaemons(c.req.query("active_only") === "true")),
);
api.get("/daemons/", async (c) =>
  c.json(await control(c.env).listDaemons(c.req.query("active_only") === "true")),
);

api.delete("/daemons/:agent_id{.+}", async (c) => {
  const id = c.req.param("agent_id");
  const ok = await control(c.env).deleteDaemon(id);
  return ok ? c.json({ deleted: id }) : c.json({ detail: `Agent '${id}' not found` }, 404);
});

// ====================================================================
// cohorts (collections)
// ====================================================================

api.get("/cohorts", async (c) => c.json(await control(c.env).listCohorts()));
api.get("/cohorts/leaderboard", async (c) => c.json(await control(c.env).leaderboard()));

api.get("/cohorts/:id/summary", async (c) => {
  const row = await control(c.env).cohortSummary(c.req.param("id"), {
    workflow_id: c.req.query("workflow_id") ?? null,
    workflow_version: c.req.query("workflow_version") ?? null,
    all_workflows: c.req.query("all_workflows") === "true",
  });
  return row ? c.json(row) : c.json({ detail: `Cohort '${c.req.param("id")}' not found.` }, 404);
});

api.get("/cohorts/:id/failures", (c) => historicalTier(c));

// ====================================================================
// metrics
// ====================================================================

api.get("/metrics/processes/running", async (c) => {
  const [live, active] = await Promise.all([
    sink(c.env).runningProcesses(),
    control(c.env).activeRunCount(),
  ]);
  return c.json({ generated_at_utc: new Date().toISOString(), active_nf_runs: active, ...live });
});

// The analytical endpoints need per-task history, which now lives as NDJSON on
// R2 rather than in a queryable table. Explicit 501 beats a silently empty
// response — see cf/README.md "historical tier".
for (const p of ["summary", "retries", "resources-by-attempt", "failures", "failure-signatures", "tasks", "timeline"]) {
  api.get(`/metrics/processes/${p}`, (c) => historicalTier(c));
}

// ====================================================================
// admin
// ====================================================================

api.post("/admin/reconcile-jobs", async (c) => c.json({ jobs_created: await control(c.env).reconcileJobs() }));

api.post("/admin/reset-running", async (c) => {
  const pk = num(c.req.query("workflow_pk"), NaN);
  if (Number.isNaN(pk)) return c.json({ detail: "workflow_pk is required" }, 422);
  return c.json({ reset: await control(c.env).resetJobsToPending(pk, ["running", "failed"]) });
});

api.post("/admin/close-run", async (c) => {
  const runName = c.req.query("run_name") ?? (await json(c)).run_name;
  if (!runName) return c.json({ detail: "run_name is required" }, 422);
  const res = await control(c.env).closeRun(runName, "completed", null);
  if (!res) return c.json({ detail: `No workflow run with name '${runName}'` }, 404);
  await runDo(c.env, runName).finalize();
  return c.json({ run_name: runName, already_closed: res.already_closed, swept: res.swept });
});

// Both of these were cron-driven sweeps in v1. Expiry is a per-run timer now,
// so they have nothing to do — kept so existing crontabs and scripts don't 404.
api.post("/admin/expire-stale-runs", (c) => {
  c.header("Deprecation", "true");
  return c.json({ stale_runs_closed: 0, jobs_swept: 0 });
});

api.post("/admin/heartbeat-watchdog", (c) => {
  c.header("Deprecation", "true");
  return c.json({
    checked_at: new Date().toISOString(),
    stale_after_minutes: 0,
    stale_runs_failed: 0,
    jobs_swept: 0,
    runs: [],
  });
});

api.post("/admin/requeue-dead-letter", async (c) =>
  c.json({ requeued: await control(c.env).requeueDeadLetter() }),
);

// Housekeeping the daily cron also does; exposed so an operator who just
// retired a version does not have to wait for it.
api.post("/admin/archive-retired", async (c) => {
  const pk = c.req.query("workflow_pk");
  return c.json(await control(c.env).archiveRetiredJobs(pk ? Number(pk) : undefined));
});

/**
 * Wipe every trace of state: all three Durable Object classes and the R2
 * prefixes. The teardown half of the dev loop — reset, re-migrate, test again.
 *
 * Guarded by an env flag as well as the bearer token, because the token is
 * shared with every other write route and a fat-fingered path should not be
 * able to empty the control plane. `resetAllowed` fails closed: anything other
 * than the exact string "true" refuses.
 */
api.post("/admin/reset", async (c) => {
  if (!resetAllowed(c.env)) {
    return c.json({ detail: "Reset is disabled here. Set ALLOW_RESET=true to enable it." }, 403);
  }
  const ctl = control(c.env);

  // Run names first: a RunDO is addressed by run_name, so once the rows are
  // gone there is nothing left to tell us which timers to cancel.
  const runs = await ctl.runNames();
  for (const name of runs) await runDo(c.env, name).finalize();

  const cleared = await ctl.reset();
  await sink(c.env).reset();

  let objects = 0;
  for (const prefix of R2_PREFIXES) {
    let cursor: string | undefined;
    do {
      const listed = await c.env.STORE.list({ prefix, cursor, limit: 1000 });
      if (listed.objects.length) {
        await c.env.STORE.delete(listed.objects.map((o) => o.key));
        objects += listed.objects.length;
      }
      cursor = listed.truncated ? listed.cursor : undefined;
    } while (cursor);
  }

  return c.json({ cleared, runs_finalized: runs.length, r2_objects_deleted: objects });
});

api.get("/admin/dispatchability", async (c) => c.json(await control(c.env).dispatchability()));
api.get("/admin/stats", async (c) => c.json(await control(c.env).stats()));

api.get("/health", (c) => c.json({ message: "App Started", status: "Healthy", database: "Connected" }));

// ====================================================================
// app assembly
// ====================================================================

const app = new Hono<{ Bindings: Env }>();

app.use("*", async (c, next) =>
  cors({ origin: (c.env.CORS_ORIGINS ?? "*").split(",").map((s) => s.trim()), credentials: true })(c, next),
);

app.use("*", async (c, next) => {
  const token = c.env.API_TOKEN;
  const path = new URL(c.req.url).pathname;
  // Reads stay open (the dashboard is unauthenticated in v1) and /telemetry
  // must stay open because Nextflow's weblog reporter cannot send headers.
  if (!token || c.req.method === "GET" || c.req.method === "OPTIONS" || path.endsWith("/telemetry")) {
    return next();
  }
  const auth = c.req.header("authorization") ?? "";
  if (!/^bearer /i.test(auth) || !constantTimeEqual(auth.slice(7).trim(), token)) {
    return c.json({ detail: "Bearer token required" }, 401);
  }
  return next();
});

app.route("/api", api);
app.route("/", api);

export default {
  fetch: app.fetch,

  /**
   * Daily snapshot of the whole control plane to R2. Every DO here is
   * rebuildable from it, which is the point — the spec's anti-entropy diff
   * assumed two independent state stores to reconcile; there is only one.
   */
  async scheduled(_event: ScheduledController, env: Env): Promise<void> {
    const snap = await control(env).snapshot();
    const day = new Date().toISOString().slice(0, 10);
    await env.STORE.put(`snapshots/${day}.json.gz`, await gzip(JSON.stringify(snap)));
    // Flush whatever is still buffered so a quiet day still lands its events.
    await sink(env).flush();
    // Then reclaim: retired versions' jobs move to R2, bounding the live table
    // at samples × active versions. Runs after the snapshot so the day's
    // backup still contains everything.
    await control(env).archiveRetiredJobs();
  },
} satisfies ExportedHandler<Env>;

// ====================================================================
// helpers
// ====================================================================

async function json(c: any): Promise<any> {
  return (await c.req.json().catch(() => ({}))) ?? {};
}

function num(v: unknown, d: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, Math.trunc(n)));
}

const mins = (v: string | undefined, d: number) => num(v, d) * 60_000;
const hours = (v: string | undefined, d: number) => num(v, d) * 3_600_000;

function notFoundWorkflow(c: any) {
  return c.json({ detail: `Workflow ${c.req.param("pk")} not found` }, 404);
}

function historicalTier(c: any) {
  return c.json(
    {
      detail:
        "Historical metrics are served from the telemetry event archive on R2, " +
        "which has no query tier yet. See cf/README.md.",
    },
    501,
  );
}

/** Nextflow's own short form: two-char dir + six hex chars. */
function shortHash(taskHash: string): string {
  const [dir, rest] = taskHash.split("/", 2);
  return rest && rest.length > 6 ? `${dir}/${rest.slice(0, 6)}` : taskHash;
}

/**
 * v1's task_logs rows had integer ids that the dashboard uses only as React
 * keys. ponytail: derive one from the object key instead of keeping a counter
 * whose only job is to be unique.
 */
function keyId(key: string): number {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (Math.imul(31, h) + key.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Nextflow sometimes ships the entire tz database inside a weblog event. */
function stripZoneIds(metadata: any): void {
  for (const k of ["start", "complete"]) {
    try {
      delete metadata.workflow[k].offset.availableZoneIds;
    } catch {
      /* absent — fine */
    }
  }
}
