/**
 * MODE=sink-only (#375, ADR-0013 M5): the three event routes archive through SinkDO (and R2 for
 * attachments) without ever creating a ControlDO or RunDO stub, against the real Worker
 * (miniflare, no mocks). Removing CONTROL and RUN from the env makes any stub creation a 500.
 */
import { createExecutionContext, env, runInDurableObject, SELF, waitOnExecutionContext } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";
import worker, { sinkOnly } from "../src/index";
import type { SinkDO } from "../src/sink-do";
import type { Env } from "../src/types";

const SINK_ONLY = { MODE: "sink-only", CONTROL: undefined, RUN: undefined };

async function post(path: string, body: unknown) {
  return SELF.fetch(`https://x${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function withEnv(overrides: Record<string, unknown>, path: string, init: RequestInit) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(new Request(`https://x${path}`, init), { ...env, ...overrides } as unknown as Env, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

function runEventForm(event: Record<string, unknown>, nextflowLog?: string) {
  const fd = new FormData();
  fd.set("event", JSON.stringify({ utc_time: new Date().toISOString(), ...event }));
  if (nextflowLog) fd.set("nextflow_log", new File([nextflowLog], "nextflow.log"));
  return { method: "POST", body: fd };
}

const sinkStub = () => env.SINK.get(env.SINK.idFromName("v1"));
const flush = () => runInDurableObject(sinkStub(), (i: SinkDO) => i.flush());

/** Flush the buffer and return the envelopes of the object that flush wrote. */
async function flushed(): Promise<any[]> {
  const out = await flush();
  const obj = await env.STORE.get(out!.key);
  const text = await new Response(obj!.body.pipeThrough(new DecompressionStream("gzip"))).text();
  return text.split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

let knownRun: string;

beforeAll(async () => {
  await post("/api/admin/reset", {});
  await post("/api/workflows", {
    workflow_id: "cmgd", version: "1.0.0", repository_url: "https://example.org/cmgd", revision: "main",
    max_retries: 1, collections: ["c1"],
  });
  await post("/api/samples", { sample_id: "sampleA", ncbi_accession: "SRR000001", collection: "c1" });
  await post("/api/admin/reconcile-jobs", {});
  const batch = (await (await post("/api/dispatch/batch", { limit: 1 })).json()) as any;
  await post("/api/dispatch/submitted", { run_name: batch.run_name });
  knownRun = batch.run_name;
});

describe("sink-only event routes", () => {
  it("answer as in full mode and write only to SinkDO and R2", async () => {
    await flush();

    const weblog = { runName: "r-unknown", event: "started", runId: "nf-1" };
    const w = await withEnv(SINK_ONLY, "/telemetry", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(weblog),
    });
    expect(w.status).toBe(200);
    expect(await w.json()).toEqual(weblog);

    const hb = await withEnv(SINK_ONLY, "/api/runs/r-unknown/event", runEventForm({ type: "heartbeat" }));
    expect(hb.status).toBe(201);
    expect(await hb.json()).toEqual({
      run_name: "r-unknown", type: "heartbeat", nextflow_log_uploaded: false, wrapper_output_log_uploaded: false,
    });

    const exited = await withEnv(SINK_ONLY, "/api/runs/r-unknown/event", runEventForm({ type: "wrapper_exited", exit_code: 0 }, "log text"));
    expect(exited.status).toBe(201);
    expect(((await exited.json()) as any).nextflow_log_uploaded).toBe(true);

    const fd = new FormData();
    fd.set("run_name", "r-unknown");
    fd.set("task_hash", "ab/cdef12");
    fd.set("log_type", "command_out");
    fd.set("content", new File(["x"], "content"));
    expect((await withEnv(SINK_ONLY, "/api/task-logs", { method: "POST", body: fd })).status).toBe(201);

    const lines = await flushed();
    expect(lines.map((l) => [l.source, l.event, l.run_name])).toEqual([
      ["weblog", "started", "r-unknown"],
      ["run_event", "run_heartbeat", "r-unknown"],
      ["run_event", "run_wrapper_exited", "r-unknown"],
    ]);
    expect(lines.map((l) => "nextflow_log_uploaded" in l)).toEqual([false, false, true]);
    expect(lines[2].nextflow_log_uploaded).toBe(true);
    expect(lines.every((l) => typeof l.received_at === "string")).toBe(true);
    expect(await (await env.STORE.get("nextflow-logs/r-unknown/nextflow.log"))!.text()).toBe("log text");
    expect(await env.STORE.get("task-logs/r-unknown/ab/cdef12/command_out")).not.toBeNull();
  });

  it("still validate: attachments only on wrapper_exited", async () => {
    const res = await withEnv(SINK_ONLY, "/api/runs/r-unknown/event", runEventForm({ type: "heartbeat" }, "x"));
    expect(res.status).toBe(422);
  });
});

describe("full mode", () => {
  it("marks nextflow_log_uploaded in the envelope only when a log was stored", async () => {
    await flush();
    expect((await SELF.fetch(`https://x/api/runs/${knownRun}/event`, runEventForm({ type: "heartbeat" }))).status).toBe(201);
    expect((await SELF.fetch(`https://x/api/runs/${knownRun}/event`, runEventForm({ type: "wrapper_exited", exit_code: 0 }, "log"))).status).toBe(201);

    const lines = await flushed();
    expect(lines.map((l) => l.event)).toEqual(["run_heartbeat", "run_wrapper_exited"]);
    expect("nextflow_log_uploaded" in lines[0]).toBe(false);
    expect(lines[1].nextflow_log_uploaded).toBe(true);
  });
});

describe("scheduled", () => {
  it("flushes SinkDO and nothing else in sink-only mode", async () => {
    await flush();
    await withEnv(SINK_ONLY, "/telemetry", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ runName: "r-sched", event: "started" }),
    });
    const ctx = createExecutionContext();
    await worker.scheduled({} as ScheduledController, { ...env, ...SINK_ONLY } as unknown as Env, ctx);
    await waitOnExecutionContext(ctx);

    // Nothing is left to flush, so this returns the object the cron wrote.
    const lines = await flushed();
    expect(lines.map((l) => l.run_name)).toEqual(["r-sched"]);
  });
});

describe("sinkOnly", () => {
  it("is true only for the exact string sink-only", () => {
    expect(sinkOnly({ MODE: "sink-only" })).toBe(true);
    for (const mode of ["full", undefined, "SINK-ONLY", "sink-only ", ""]) expect(sinkOnly({ MODE: mode }), String(mode)).toBe(false);
  });
});
