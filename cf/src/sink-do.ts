/**
 * SinkDO — the event stream. Every weblog event and every run-lifecycle event
 * lands here, is buffered, and is flushed to R2 as gzipped NDJSON partitioned
 * by UTC date. That is the historical record; ControlDO holds only live state.
 *
 * The envelope is stable (run_name, event, utc_time, received_at, source,
 * payload) and the raw Nextflow JSON rides along as a string — schema-on-read,
 * exactly like v1's JSONB column, and immune to weblog drift between Nextflow
 * versions.
 *
 * It also keeps the in-flight process counters, because it is already the one
 * object that sees every process event — the live tier of
 * /metrics/processes/running reads them straight out of SQLite.
 *
 * ponytail: NDJSON, not Parquet. DuckDB reads
 * `read_json_auto('r2://nf-telemetry/telemetry/events/**]/*.ndjson.gz')`
 * directly, so the historical tier can be built without a Pipelines
 * dependency. Compact to Parquet when query time over the raw files starts to
 * hurt — the partition layout is already the one Parquet would use.
 */
import { DurableObject } from "cloudflare:workers";
import type { Env } from "./types";
import { gzip } from "./util";

/** Flush when either bound is hit; whichever comes first. */
const FLUSH_ROWS = 500;
const FLUSH_MS = 60_000;

export interface TelemetryEnvelope {
  run_name: string;
  event: string;
  utc_time: string | null;
  source: "weblog" | "run_event";
  run_id?: string | null;
  sample_id?: string | null;
  process?: string | null;
  payload: unknown;
}

export class SinkDO extends DurableObject<Env> {
  private sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`
      create table if not exists buf (id integer primary key autoincrement, line text not null);
      create table if not exists proc (
        process text primary key,
        submitted integer not null default 0,
        started integer not null default 0,
        completed integer not null default 0
      );
    `);
  }

  async write(ev: TelemetryEnvelope): Promise<void> {
    this.sql.exec(
      `insert into buf (line) values (?)`,
      JSON.stringify({ ...ev, received_at: new Date().toISOString() }),
    );
    if (ev.process) this.countProcess(ev.event, ev.process);

    const n = this.sql.exec(`select count(*) as n from buf`).one().n as number;
    if (n >= FLUSH_ROWS) {
      await this.flush();
    } else if ((await this.ctx.storage.getAlarm()) === null) {
      await this.ctx.storage.setAlarm(Date.now() + FLUSH_MS);
    }
  }

  private countProcess(event: string, process: string) {
    const col =
      event === "process_submitted"
        ? "submitted"
        : event === "process_started"
          ? "started"
          : event === "process_completed"
            ? "completed"
            : null;
    if (!col) return;
    this.sql.exec(
      `insert into proc (process, ${col}) values (?, 1)
       on conflict(process) do update set ${col} = ${col} + 1`,
      process,
    );
  }

  /**
   * Live tier for GET /metrics/processes/running.
   *
   * ponytail: cumulative counters differenced, not a scan of open tasks. A
   * dropped event leaks a phantom in-flight task forever; that is the known
   * ceiling. Recompute from the R2 events if the numbers ever drift enough to
   * matter.
   */
  runningProcesses() {
    const rows = this.sql.exec(`select * from proc order by process`).toArray() as any[];
    const by_process = rows
      .map((r) => ({
        process: r.process as string,
        running: Math.max(0, (r.started as number) - (r.completed as number)),
        queued: Math.max(0, (r.submitted as number) - (r.started as number)),
      }))
      .filter((r) => r.running > 0 || r.queued > 0);
    return {
      total_running: by_process.reduce((a, r) => a + r.running, 0),
      total_queued: by_process.reduce((a, r) => a + r.queued, 0),
      by_process,
    };
  }

  /**
   * Drop buffered events and the process counters.
   *
   * The counters especially: they are cumulative deltas, so a `process_started`
   * whose `process_completed` never arrives leaves a phantom in-flight task
   * forever. Test traffic guarantees that, which is why reset clears them.
   */
  async reset(): Promise<void> {
    this.sql.exec(`delete from buf`);
    this.sql.exec(`delete from proc`);
    await this.ctx.storage.deleteAlarm();
  }

  async alarm(): Promise<void> {
    await this.flush();
  }

  async flush(): Promise<void> {
    const rows = this.sql.exec(`select id, line from buf order by id`).toArray() as any[];
    if (!rows.length) return;
    const body = rows.map((r) => r.line).join("\n") + "\n";

    const now = new Date();
    const dt = now.toISOString().slice(0, 10);
    const key = `telemetry/events/dt=${dt}/${now.toISOString().slice(11, 19).replace(/:/g, "")}-${crypto
      .randomUUID()
      .slice(0, 8)}.ndjson.gz`;

    await this.env.STORE.put(key, await gzip(body));

    // Delete only what was written: rows added while the R2 put was in flight
    // keep their place in the next batch.
    this.sql.exec(`delete from buf where id <= ?`, rows[rows.length - 1].id);
    await this.ctx.storage.deleteAlarm();
  }
}
