import { env, runInDurableObject } from "cloudflare:test";
import { expect, it } from "vitest";
import type { ControlDO } from "../src/control-do";

// DO SQLite caps a statement at 100 bound parameters; requeue must not bind ids.
it("requeues more than 100 dead-lettered jobs", async () => {
  const stub = env.CONTROL.get(env.CONTROL.idFromName("requeue-many"));
  const n = await runInDurableObject(stub, (instance: ControlDO, state) => {
    const sql = state.storage.sql;
    for (let i = 0; i < 150; i++) {
      sql.exec(
        `insert into jobs (sample_id, workflow_pk, workflow_id, workflow_version, status, created_at)
         values (?, 1, 'wf', '1', 'dead_letter', '2026-01-01')`, `s${i}`);
      sql.exec(`insert into dead_letter (job_id, created_at) values (last_insert_rowid(), '2026-01-01')`);
    }
    return instance.requeueDeadLetter();
  });
  expect(n).toBe(150);
});
