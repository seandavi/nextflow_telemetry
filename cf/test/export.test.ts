/**
 * POST /admin/export (#376): a consistent ControlDO export to R2 plus the event
 * cursor, against the real Worker + Durable Objects (miniflare, no mocks).
 */
import { env, runInDurableObject, SELF } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";
import type { ControlDO } from "../src/control-do";
import { authExempt } from "../src/index";
import type { SinkDO } from "../src/sink-do";
import * as S from "../src/schemas";

const TABLES = ["samples", "collections", "collection_samples", "workflows", "jobs", "job_counts", "runs",
  "dead_letter", "daemons", "run_task_counts", "run_failed_tasks"];

async function post(path: string, body: unknown) {
  return SELF.fetch(`https://x${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function gunzipObject(key: string): Promise<string> {
  const obj = await env.STORE.get(key);
  expect(obj, key).not.toBeNull();
  return new Response(obj!.body.pipeThrough(new DecompressionStream("gzip"))).text();
}

const lines = (text: string) => text.split("\n").filter(Boolean);
const controlStub = () => env.CONTROL.get(env.CONTROL.idFromName("v1"));
const sinkStub = () => env.SINK.get(env.SINK.idFromName("v1"));

beforeAll(async () => {
  await post("/api/admin/reset", {});
  await post("/api/workflows", {
    workflow_id: "cmgd", version: "1.0.0", repository_url: "https://example.org/cmgd", revision: "main",
    max_retries: 1, collections: ["c1"],
  });
  await post("/api/samples", { sample_id: "sampleA", ncbi_accession: "SRR000001", collection: "c1" });
  await post("/api/samples", { sample_id: "sampleA", ncbi_accession: "SRR000001", collection: "c2" });
  await post("/api/samples", { sample_id: "sampleB", ncbi_accession: "SRR000002", collection: "c1" });
  await post("/api/samples", { sample_id: "sampleC", ncbi_accession: "SRR000003", collection: "c1" });
  await post("/api/admin/reconcile-jobs", {});
  const batch = (await (await post("/api/dispatch/batch", { limit: 1 })).json()) as any;
  await post("/api/dispatch/submitted", { run_name: batch.run_name });
  const weblog = (event: string, extra: Record<string, unknown> = {}) =>
    post("/telemetry", { runId: "nf-1", runName: batch.run_name, event, utcTime: new Date().toISOString(), ...extra });
  await weblog("started");
  await weblog("process_completed", { trace: { tag: batch.jobs[0].sample_id, process: "cmgd:FASTP", status: "COMPLETED" } });
  await SELF.fetch("https://x/api/daemons/heartbeat", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ agent_id: "login07:cmgd", hostname: "login07", workflow_id: "cmgd", mode: "slurm", batch_size: 10 }),
  });
});

describe("POST /admin/export", () => {
  it("writes every table, the manifest and the event cursor", async () => {
    const res = await post("/api/admin/export", {});
    expect(res.status).toBe(200);
    const manifest = S.ExportManifest.parse(await res.json());
    expect(Object.keys(manifest.tables).sort()).toEqual([...TABLES].sort());

    const stored = JSON.parse(await (await env.STORE.get(`exports/${manifest.export_id}/manifest.json`))!.text());
    expect(stored).toEqual(manifest);

    await runInDurableObject(controlStub(), async (_i: ControlDO, state) => {
      for (const t of TABLES) {
        const truth = state.storage.sql.exec(`select * from ${t} order by rowid`).toArray();
        const entry = manifest.tables[t];
        expect(entry.rows, t).toBe(truth.length);
        const back: unknown[] = [];
        for (const key of entry.objects) back.push(...lines(await gunzipObject(key)).map((l) => JSON.parse(l)));
        expect(back, t).toEqual(truth);
      }
    });
    expect(manifest.tables.samples.rows).toBeGreaterThan(0);
    expect(manifest.sequences.jobs).toBeGreaterThan(0);

    const cursor = manifest.events_cursor!;
    expect(cursor.stream).toBe("telemetry/events");
    expect(lines(await gunzipObject(cursor.object_key))).toHaveLength(cursor.line_offset);
    const events = lines(await gunzipObject(cursor.object_key)).map((l) => JSON.parse(l).event);
    expect(events).toContain("process_completed");
  });

  it("pages by rowid in chunks", async () => {
    const snap = await runInDurableObject(controlStub(), (inst: ControlDO) => inst.exportSnapshot("exports/test-chunk", 2));
    const jobs = snap.tables.jobs;
    expect(jobs.objects).toHaveLength(Math.ceil(jobs.rows / 2));
    expect(jobs.rows).toBeGreaterThan(2);
    const ids: number[] = [];
    for (const key of jobs.objects) ids.push(...lines(await gunzipObject(key)).map((l) => JSON.parse(l).id));
    expect(ids).toEqual([...ids].sort((a, b) => a - b));
    expect(ids).toHaveLength(jobs.rows);
  });

  it("is not auth-exempt", () => {
    expect(authExempt("POST", "/api/admin/export")).toBe(false);
  });
});

describe("SinkDO.flush", () => {
  it("returns the last object when the buffer is empty and keys increase", async () => {
    const first = await runInDurableObject(sinkStub(), (inst: SinkDO) => inst.flush());
    const again = await runInDurableObject(sinkStub(), (inst: SinkDO) => inst.flush());
    expect(again).toEqual(first);

    const keys: string[] = [first!.key];
    for (let i = 0; i < 4; i++) {
      await post("/telemetry", { runId: "nf-1", runName: "flush-probe", event: "started", utcTime: new Date().toISOString() });
      const f = await runInDurableObject(sinkStub(), (inst: SinkDO) => inst.flush());
      keys.push(f!.key);
    }
    for (let i = 1; i < keys.length; i++) expect(keys[i] > keys[i - 1], `${keys[i - 1]} < ${keys[i]}`).toBe(true);
  });
});
