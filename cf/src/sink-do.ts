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
import { gzip, serialized } from "./util";

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
  /** Set on run events whose nextflow_log attachment was stored; the ingester stamps nextflow_log_uploaded_at. */
  nextflow_log_uploaded?: true;
  payload: unknown;
}

/** The last NDJSON object written under telemetry/events/, and its line count. */
export interface FlushedObject {
  key: string;
  lines: number;
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
      create table if not exists flush_state (
        id    integer primary key check (id = 1),
        seq   integer not null,
        key   text not null,
        lines integer not null
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
    this.sql.exec(`delete from flush_state`); // its key names an object reset just deleted
    await this.ctx.storage.deleteAlarm();
  }

  async alarm(): Promise<void> {
    await this.flush();
  }

  /**
   * Serialized: an alarm flush and an explicit flush can't write the same rows
   * twice. The key suffix is a per-DO counter, so objects sort in write order
   * and "everything after key K" is an exact cursor (#376).
   */
  async flush(): Promise<FlushedObject | null> {
    return serialized(this.ctx, async () => {
      const rows = this.sql.exec(`select id, line from buf order by id`).toArray() as any[];
      const state = this.sql.exec(`select seq, key, lines from flush_state where id = 1`).toArray()[0] as
        | { seq: number; key: string; lines: number }
        | undefined;
      if (!rows.length) return state ? { key: state.key, lines: state.lines } : null;
      const body = rows.map((r) => r.line).join("\n") + "\n";

      const seq = (state?.seq ?? 0) + 1;
      const now = new Date();
      const dt = now.toISOString().slice(0, 10);
      const key = `telemetry/events/dt=${dt}/${now.toISOString().slice(11, 19).replace(/:/g, "")}-${seq
        .toString(16)
        .padStart(8, "0")}.ndjson.gz`;

      await this.env.STORE.put(key, await gzip(body));

      this.sql.exec(`delete from buf where id <= ?`, rows[rows.length - 1].id);
      this.sql.exec(
        `insert into flush_state (id, seq, key, lines) values (1, ?, ?, ?)
         on conflict(id) do update set seq = excluded.seq, key = excluded.key, lines = excluded.lines`,
        seq,
        key,
        rows.length,
      );
      await this.ctx.storage.deleteAlarm();
      return { key, lines: rows.length };
    });
  }
}
