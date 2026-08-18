/**
 * ControlDO — the whole relational control plane in one Durable Object.
 *
 * v1 spread this across Postgres tables served by a stateless FastAPI app;
 * the locking choreography there (`FOR UPDATE SKIP LOCKED`, advisory locks)
 * existed only to serialize concurrent writers. A Durable Object is
 * single-threaded by construction and `ctx.storage.sql.exec` is synchronous,
 * so any method that does not `await` between statements is already atomic.
 * That is why there is no transaction handling anywhere below.
 *
 * This module is the ONE place that writes job/run `status` — the same rule
 * services/lifecycle.py enforced in v1, and for the same reason.
 *
 * ponytail: one DO for samples + workflows + jobs + runs + collections +
 * daemons. Claim traffic is a handful of daemons polling every ~30s, so the
 * single-writer ceiling is nowhere near. Shard into one DO per
 * (workflow_id, version) if claim throughput ever becomes the bottleneck —
 * the claim path already scopes every query to one workflow version.
 */
import { DurableObject } from "cloudflare:workers";
import { SCHEMA } from "./schema";
import type { Env } from "./types";
import { gzip, ndjson, uuidv7 } from "./util";

/** Run states that have already had their terminal write; closing again is a no-op. */
const RUN_TERMINAL = ["completed", "failed", "expired"];
/** Job states that are "in flight under a run" and therefore sweepable. */
const JOB_INFLIGHT = ["claimed", "submitted", "running"];

const SWEEP_REASON_DEFAULT = "run completed without MARK_COMPLETE";

/** Rows per archive object — bounds both the JSON in memory and the R2 body. */
const ARCHIVE_CHUNK = 5_000;

type Row = Record<string, any>;

export class ControlDO extends DurableObject<Env> {
  private sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(SCHEMA);
    this.backfillJobCounts();
  }

  /**
   * Populate job_counts from jobs when it is empty — on first deploy against an
   * existing corpus, and as self-repair if the counters are ever wiped. Costs
   * one row read per wake-up; the scan only happens when there is nothing to
   * scan against.
   */
  private backfillJobCounts(): void {
    if (this.sql.exec(`select 1 as x from job_counts limit 1`).toArray().length) return;
    this.sql.exec(
      `insert into job_counts (workflow_pk, status, n)
       select workflow_pk, status, count(*) from jobs group by workflow_pk, status`,
    );
  }

  private all(q: string, ...b: any[]): Row[] {
    return this.sql.exec(q, ...b).toArray() as Row[];
  }
  private first(q: string, ...b: any[]): Row | undefined {
    return this.all(q, ...b)[0];
  }
  /**
   * Execute a write and return the number of rows it actually affected.
   * `rowsWritten` counts index maintenance too (a 3-row insert into `jobs`
   * reports 16), so RETURNING is the only honest affected-row count.
   */
  private run(q: string, ...b: any[]): number {
    return this.sql.exec(`${q} returning 1`, ...b).toArray().length;
  }

  // ==================================================================
  // Samples
  // ==================================================================

  registerSample(req: {
    sample_id: string;
    ncbi_accession: string;
    biosample_id?: string | null;
    // Deliberately `unknown`, not `any`: a DO RPC signature containing `any`
    // sends TypeScript into an infinite serializable-type expansion.
    metadata?: Record<string, unknown> | null;
    collection?: string | null;
  }): Row {
    const now = iso();
    // Canonical form: sorted + deduplicated SRR set, matching v1's
    // parse_srrs/normalisation so sample identity stays content-addressed.
    const acc = normalizeSrrs(req.ncbi_accession);
    this.run(
      `insert into samples (sample_id, ncbi_accession, biosample_id, metadata, created_at, updated_at)
       values (?, ?, ?, ?, ?, ?)
       on conflict(sample_id) do update set
         ncbi_accession = excluded.ncbi_accession,
         biosample_id   = excluded.biosample_id,
         metadata       = excluded.metadata,
         updated_at     = excluded.updated_at`,
      req.sample_id,
      acc,
      req.biosample_id ?? null,
      JSON.stringify(req.metadata ?? {}),
      now,
      now,
    );
    if (req.collection) this.addToCollection(req.collection, req.sample_id, now);
    return this.getSample(req.sample_id)!;
  }

  /** One membership write seam (ADR-0005): collection rows are the truth, never a metadata key. */
  private addToCollection(collectionId: string, sampleId: string, now: string) {
    const source = collectionId.startsWith("PRJ")
      ? "bioproject"
      : /^[DES]RP\d+$/.test(collectionId)
        ? "sra_study"
        : "manual";
    this.run(
      `insert into collections (collection_id, source, created_at, updated_at) values (?, ?, ?, ?)
       on conflict(collection_id) do update set updated_at = excluded.updated_at`,
      collectionId,
      source,
      now,
      now,
    );
    this.run(
      `insert or ignore into collection_samples (collection_id, sample_id, created_at) values (?, ?, ?)`,
      collectionId,
      sampleId,
      now,
    );
  }

  getSample(sampleId: string): Row | null {
    const row = this.first(`select * from samples where sample_id = ?`, sampleId);
    return row ? this.hydrateSample(row) : null;
  }

  getSampleBySrr(srr: string): Row | null {
    // ncbi_accession is a ';'-joined canonical list; bracket the LIKE with
    // separators so SRR12 does not match SRR123.
    const row = this.first(
      `select * from samples where ';' || ncbi_accession || ';' like ? limit 1`,
      `%;${srr};%`,
    );
    return row ? this.hydrateSample(row) : null;
  }

  getSamplesByBiosample(biosampleId: string): Row[] {
    return this.all(
      `select * from samples where biosample_id = ? order by created_at desc`,
      biosampleId,
    ).map((r) => this.hydrateSample(r));
  }

  listSamples(opts: { limit: number; offset: number; search?: string | null; collection?: string | null }) {
    const where: string[] = [];
    const args: any[] = [];
    let from = "samples s";
    if (opts.collection) {
      from += " join collection_samples cs on cs.sample_id = s.sample_id";
      where.push("cs.collection_id = ?");
      args.push(opts.collection);
    }
    if (opts.search) {
      where.push("s.sample_id like ?");
      args.push(`%${opts.search}%`);
    }
    const w = where.length ? `where ${where.join(" and ")}` : "";
    const total = this.first(`select count(*) as n from ${from} ${w}`, ...args)!.n as number;
    const rows = this.all(
      `select s.* from ${from} ${w} order by s.id limit ? offset ?`,
      ...args,
      opts.limit,
      opts.offset,
    );
    return { items: rows.map((r) => this.hydrateSample(r)), total };
  }

  collectionFacets() {
    const total = this.first(`select count(*) as n from samples`)!.n as number;
    const collections = this.all(
      `select collection_id as collection, count(*) as count
         from collection_samples group by collection_id order by count desc`,
    );
    return { total, collections };
  }

  private hydrateSample(row: Row): Row {
    return {
      id: row.id,
      sample_id: row.sample_id,
      ncbi_accession: row.ncbi_accession,
      biosample_id: row.biosample_id,
      metadata: safeJson(row.metadata),
      collections: this.all(
        `select collection_id from collection_samples where sample_id = ? order by collection_id`,
        row.sample_id,
      ).map((r) => r.collection_id),
      created_at: row.created_at,
      updated_at: row.updated_at,
    };
  }

  // ==================================================================
  // Workflows
  // ==================================================================

  registerWorkflow(req: {
    workflow_id: string;
    version: string;
    repository_url: string;
    revision: string;
    manifest_version?: string | null;
    max_retries?: number;
    description?: string | null;
  }): Row {
    const now = iso();
    this.run(
      `insert into workflows (workflow_id, version, repository_url, revision, manifest_version,
                              max_retries, status, description, created_at, updated_at)
       values (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
       on conflict(workflow_id, version) do update set
         repository_url   = excluded.repository_url,
         revision         = excluded.revision,
         manifest_version = excluded.manifest_version,
         max_retries      = excluded.max_retries,
         description      = excluded.description,
         updated_at       = excluded.updated_at`,
      req.workflow_id,
      req.version,
      req.repository_url,
      req.revision,
      req.manifest_version ?? null,
      req.max_retries ?? 3,
      req.description ?? null,
      now,
      now,
    );
    return this.first(
      `select * from workflows where workflow_id = ? and version = ?`,
      req.workflow_id,
      req.version,
    )!;
  }

  listWorkflows(status?: string | null): Row[] {
    return status
      ? this.all(`select * from workflows where status = ? order by workflow_id, version`, status)
      : this.all(`select * from workflows order by workflow_id, version`);
  }

  getWorkflow(pk: number): Row | null {
    return this.first(`select * from workflows where id = ?`, pk) ?? null;
  }

  updateWorkflowStatus(pk: number, status: string): Row | null {
    this.run(`update workflows set status = ?, updated_at = ? where id = ?`, status, iso(), pk);
    // Retiring a version purges its still-pending jobs so they stop being
    // dispatchable; in-flight jobs are left alone to finish (v1 WorkflowService).
    if (status === "retired") {
      this.run(`delete from jobs where workflow_pk = ? and status = 'pending'`, pk);
    }
    return this.getWorkflow(pk);
  }

  updateWorkflowRevision(pk: number, revision: string): Row | null {
    this.run(`update workflows set revision = ?, updated_at = ? where id = ?`, revision, iso(), pk);
    return this.getWorkflow(pk);
  }

  jobSummary(pk: number): Row | null {
    const wf = this.getWorkflow(pk);
    if (!wf) return null;
    const counts: Row = {};
    for (const r of this.all(`select status, n from job_counts where workflow_pk = ?`, pk)) {
      counts[r.status] = r.n;
    }
    const dead_letter = this.first(
      `select count(*) as n from dead_letter dl join jobs j on dl.job_id = j.id where j.workflow_pk = ?`,
      pk,
    )!.n as number;
    const g = (k: string) => (counts[k] as number) ?? 0;
    const total = g("pending") + g("claimed") + g("submitted") + g("running") + g("completed") + g("failed");
    return {
      workflow_pk: pk,
      workflow_id: wf.workflow_id,
      version: wf.version,
      total,
      pending: g("pending"),
      claimed: g("claimed"),
      submitted: g("submitted"),
      running: g("running"),
      completed: g("completed"),
      failed: g("failed"),
      dead_letter,
      completion_pct: total > 0 ? round2((100 * g("completed")) / total) : 0,
    };
  }

  // ==================================================================
  // Jobs: birth, claim, transitions
  // ==================================================================

  /** Cross-product of samples × active workflows; idempotent. */
  reconcileJobs(): number {
    const now = iso();
    return this.run(
      `insert or ignore into jobs (sample_id, workflow_pk, workflow_id, workflow_version, status, retry_count, created_at)
       select s.sample_id, w.id, w.workflow_id, w.version, 'pending', 0, ?
         from samples s cross join workflows w
        where w.status = 'active'`,
      now,
    );
  }

  /**
   * Claim up to `limit` pending jobs for ONE workflow version and mint a run.
   *
   * Two-step pick-then-take is kept from v1 for its *ordering* semantics
   * (oldest pending job decides the workflow, one workflow version per
   * batch), not for its locking — there is nothing to lock here.
   */
  claimBatch(limit: number, workflowIds?: string[] | null, workflowVersion?: string | null) {
    const where = [`j.status = 'pending'`, `w.status = 'active'`];
    const args: any[] = [];
    if (workflowIds?.length) {
      where.push(`j.workflow_id in (${workflowIds.map(() => "?").join(",")})`);
      args.push(...workflowIds);
    }
    if (workflowVersion) {
      where.push(`j.workflow_version = ?`);
      args.push(workflowVersion);
    }
    const w = where.join(" and ");

    const pick = this.first(
      `select j.workflow_id, j.workflow_version from jobs j
         join workflows w on j.workflow_pk = w.id
        where ${w}
        order by j.workflow_id, j.workflow_version, j.created_at, j.id
        limit 1`,
      ...args,
    );
    if (!pick) return null;

    const rows = this.all(
      `select j.id, j.sample_id, j.workflow_pk, w.repository_url, w.revision
         from jobs j join workflows w on j.workflow_pk = w.id
        where ${w} and j.workflow_id = ? and j.workflow_version = ?
        order by j.created_at, j.id
        limit ?`,
      ...args,
      pick.workflow_id,
      pick.workflow_version,
      limit,
    );
    if (!rows.length) return null;

    const now = iso();
    const runName = "r" + uuidv7();
    this.run(
      `insert into runs (run_name, workflow_id, workflow_version, workflow_pk, revision, status, claimed_at)
       values (?, ?, ?, ?, ?, 'claimed', ?)`,
      runName,
      pick.workflow_id,
      pick.workflow_version,
      rows[0].workflow_pk,
      rows[0].revision,
      now,
    );
    this.run(
      `update jobs set run_name = ?, status = 'claimed'
        where status = 'pending' and id in (${rows.map(() => "?").join(",")})`,
      runName,
      ...rows.map((r) => r.id),
    );

    const samples = new Map(
      this.all(
        `select sample_id, ncbi_accession, metadata from samples where sample_id in (${rows
          .map(() => "?")
          .join(",")})`,
        ...rows.map((r) => r.sample_id),
      ).map((s) => [s.sample_id as string, s]),
    );

    return {
      run_name: runName,
      workflow_id: pick.workflow_id,
      workflow_version: pick.workflow_version,
      workflow_pk: rows[0].workflow_pk,
      repository_url: rows[0].repository_url,
      revision: rows[0].revision,
      jobs: rows.map((r) => ({
        sample_id: r.sample_id,
        ncbi_accession: samples.get(r.sample_id)?.ncbi_accession ?? null,
        metadata: safeJson(samples.get(r.sample_id)?.metadata),
      })),
    };
  }

  /** claimed -> submitted. Returns false when the run is unknown or already past `claimed`. */
  markSubmitted(runName: string, executorJobId?: string | null): boolean {
    const n = this.run(
      `update runs set status = 'submitted', submitted_at = ?, executor_job_id = ?
        where run_name = ? and status = 'claimed'`,
      iso(),
      executorJobId ?? null,
      runName,
    );
    if (!n) return false;
    this.run(
      `update jobs set status = 'submitted' where run_name = ? and status = 'claimed'`,
      runName,
    );
    return true;
  }

  /** Weblog `started`. Only from claimed/submitted, so a late duplicate cannot resurrect a closed run. */
  markRunning(runName: string, runId: string | null): void {
    this.run(
      `update runs set run_id = ?, status = 'running', started_at = ?
        where run_name = ? and status in ('claimed', 'submitted')`,
      runId,
      iso(),
      runName,
    );
    this.run(
      `update jobs set status = 'running' where run_name = ? and status in ('claimed', 'submitted')`,
      runName,
    );
  }

  /** MARK_COMPLETE sentinel. Never flips an already-terminal job. */
  completeSample(runName: string, sampleId: string): number {
    return this.run(
      `update jobs set status = 'completed', completed_at = ?
        where run_name = ? and sample_id = ? and status not in ('completed', 'failed')`,
      iso(),
      runName,
      sampleId,
    );
  }

  /**
   * Terminal write for a run: close it (if not already closed) and sweep any
   * job that never reported MARK_COMPLETE — retry within budget, else fail to
   * the dead-letter queue. Every close path (weblog `completed`, RunDO alarm,
   * admin) funnels here, so terminal state is written in exactly one place.
   *
   * Returns null when the run is unknown.
   */
  async closeRun(
    runName: string,
    terminal: "completed" | "failed" | "expired",
    reason?: string | null,
  ): Promise<{ already_closed: boolean; swept: number; outcome: Row } | null> {
    const row = this.first(`select * from runs where run_name = ?`, runName);
    if (!row) return null;
    const now = iso();
    const alreadyClosed = RUN_TERMINAL.includes(row.status);

    if (!alreadyClosed) {
      this.run(
        `update runs set status = ?, completed_at = ?, slurm_reason = coalesce(?, slurm_reason)
          where run_name = ?`,
        terminal,
        now,
        reason ?? null,
        runName,
      );
    }
    const swept = this.sweepIncomplete(runName, now, reason ?? SWEEP_REASON_DEFAULT);

    const outcome = {
      run_name: runName,
      status: alreadyClosed ? row.status : terminal,
      already_closed: alreadyClosed,
      reason: reason ?? null,
      closed_at: now,
      claimed_at: row.claimed_at,
      submitted_at: row.submitted_at,
      started_at: row.started_at,
      workflow_id: row.workflow_id,
      workflow_version: row.workflow_version,
      jobs: this.all(
        `select sample_id, status, retry_count, failure_reason from jobs where run_name = ?`,
        runName,
      ),
      swept,
    };
    // Ledger append is the last thing: all SQL above ran without an await, so
    // it was atomic. R2 is the durable history; the DO is live state.
    await this.appendLedger(runName, outcome);
    return { already_closed: alreadyClosed, swept, outcome };
  }

  /** Retry-within-budget or dead-letter, for every job still in flight under a run. */
  private sweepIncomplete(runName: string, now: string, reason: string): number {
    const rows = this.all(
      `select j.id, j.sample_id, j.workflow_id, j.workflow_version, j.retry_count, w.max_retries
         from jobs j join workflows w on j.workflow_pk = w.id
        where j.run_name = ? and j.status in (${JOB_INFLIGHT.map(() => "?").join(",")})`,
      runName,
      ...JOB_INFLIGHT,
    );
    for (const r of rows) {
      if ((r.retry_count as number) < (r.max_retries as number)) {
        this.run(
          `update jobs set retry_count = retry_count + 1, status = 'pending', run_name = null,
                           failed_at = null, failure_reason = null
            where id = ?`,
          r.id,
        );
      } else {
        this.run(
          `update jobs set retry_count = retry_count + 1, status = 'failed', failed_at = ?, failure_reason = ?
            where id = ?`,
          now,
          reason,
          r.id,
        );
        this.run(
          `insert or ignore into dead_letter (job_id, run_name, sample_id, workflow_id, workflow_version, reason, created_at)
           values (?, ?, ?, ?, ?, ?, ?)`,
          r.id,
          runName,
          r.sample_id,
          r.workflow_id,
          r.workflow_version,
          reason,
          now,
        );
      }
    }
    return rows.length;
  }

  /**
   * Claim TTL expiry — the RunDO alarm calls this. Replaces v1's
   * `POST /dispatch/requeue-expired` cron sweep.
   */
  expireClaim(runName: string): boolean {
    const n = this.run(
      `update runs set status = 'expired', completed_at = ?,
                       slurm_reason = 'claim expired without submitted confirmation'
        where run_name = ? and status = 'claimed'`,
      iso(),
      runName,
    );
    if (!n) return false;
    this.run(
      `update jobs set status = 'pending', run_name = null where run_name = ? and status = 'claimed'`,
      runName,
    );
    return true;
  }

  requeueDeadLetter(): number {
    const rows = this.all(`select id, job_id from dead_letter where resolved_at is null`);
    if (!rows.length) return 0;
    const ids = rows.map((r) => r.job_id);
    this.run(
      `update jobs set status = 'pending', retry_count = 0, run_name = null,
                       failed_at = null, failure_reason = null
        where id in (${ids.map(() => "?").join(",")})`,
      ...ids,
    );
    this.run(
      `update dead_letter set resolved_at = ? where id in (${rows.map(() => "?").join(",")})`,
      iso(),
      ...rows.map((r) => r.id),
    );
    return rows.length;
  }

  resetJobsToPending(workflowPk: number, fromStatuses: string[]): number {
    return this.run(
      `update jobs set status = 'pending', run_name = null, retry_count = 0,
                       failed_at = null, failure_reason = null
        where workflow_pk = ? and status in (${fromStatuses.map(() => "?").join(",")})`,
      workflowPk,
      ...fromStatuses,
    );
  }

  // ==================================================================
  // Runs
  // ==================================================================

  getRunStatus(runName: string): string | null {
    return (this.first(`select status from runs where run_name = ?`, runName)?.status as string) ?? null;
  }

  /** Summary-column updates driven by wrapper/daemon lifecycle events. */
  applyRunEvent(runName: string, ev: Row, receivedAt: string): void {
    switch (ev.type) {
      case "wrapper_started":
        // Defensive fallback: the wrapper can race ahead of POST /dispatch/submitted.
        this.markSubmitted(runName, null);
        break;
      case "pre_nextflow":
        if (ev.wait_seconds != null) {
          this.run(`update runs set wait_seconds = ? where run_name = ?`, ev.wait_seconds, runName);
        }
        break;
      case "wrapper_exited":
        this.run(`update runs set wrapper_exit_code = ? where run_name = ?`, ev.exit_code, runName);
        break;
      case "heartbeat":
        // Server receipt time, not the client clock — staleness must not be
        // answerable by the machine whose liveness is in question.
        this.run(`update runs set last_heartbeat_at = ? where run_name = ?`, receivedAt, runName);
        break;
      case "slurm_state":
        this.run(
          `update runs set last_known_slurm_state = ?, slurm_reason = ? where run_name = ?`,
          ev.state,
          ev.reason ?? null,
          runName,
        );
        break;
    }
  }

  recordHeartbeat(runName: string, at: string): void {
    this.run(`update runs set last_heartbeat_at = ? where run_name = ?`, at, runName);
  }

  markLogUploaded(runName: string, at: string): void {
    this.run(`update runs set nextflow_log_uploaded_at = ? where run_name = ?`, at, runName);
  }

  listRuns(opts: { status?: string | null; workflow_id?: string | null; limit: number; offset: number }) {
    const where: string[] = [];
    const args: any[] = [];
    if (opts.status) {
      where.push("status = ?");
      args.push(opts.status);
    }
    if (opts.workflow_id) {
      where.push("workflow_id = ?");
      args.push(opts.workflow_id);
    }
    const w = where.length ? `where ${where.join(" and ")}` : "";
    const total = this.first(`select count(*) as n from runs ${w}`, ...args)!.n as number;
    const rows = this.all(
      `select * from runs ${w} order by claimed_at desc limit ? offset ?`,
      ...args,
      opts.limit,
      opts.offset,
    );
    return { total, runs: rows.map((r) => ({ ...r, classification: classifyRun(r) })) };
  }

  getRun(runName: string): Row | null {
    const row = this.first(`select * from runs where run_name = ?`, runName);
    if (!row) return null;
    const jobCounts: Row = {};
    for (const r of this.all(
      `select status, count(*) as n from jobs where run_name = ? group by status`,
      runName,
    )) {
      jobCounts[r.status] = r.n;
    }
    return {
      ...row,
      classification: classifyRun(row),
      job_status_counts: jobCounts,
      // task_status_counts / failed_tasks came from the telemetry table in v1;
      // that history now lives in R2 (historical tier, not yet built).
      task_status_counts: {},
      failed_tasks: [],
    };
  }

  // ==================================================================
  // Daemons
  // ==================================================================

  daemonHeartbeat(body: Row): Row {
    const now = iso();
    this.run(
      `insert into daemons (agent_id, hostname, workflow_id, profile, nf_client_version, config_yaml,
                            mode, batch_size, max_concurrent_runs, active_runs, status, last_seen_at, started_at)
       values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       on conflict(agent_id) do update set
         hostname = excluded.hostname, workflow_id = excluded.workflow_id, profile = excluded.profile,
         nf_client_version = excluded.nf_client_version, config_yaml = excluded.config_yaml,
         mode = excluded.mode, batch_size = excluded.batch_size,
         max_concurrent_runs = excluded.max_concurrent_runs, active_runs = excluded.active_runs,
         status = excluded.status, last_seen_at = excluded.last_seen_at`,
      body.agent_id,
      body.hostname,
      body.workflow_id ?? null,
      body.profile ?? null,
      body.nf_client_version ?? null,
      body.config_yaml ?? null,
      body.mode,
      body.batch_size,
      body.max_concurrent_runs ?? null,
      body.active_runs ?? 0,
      body.status ?? "idle",
      now,
      now,
    );
    return withActive(this.first(`select * from daemons where agent_id = ?`, body.agent_id)!);
  }

  listDaemons(activeOnly: boolean): Row[] {
    const rows = this.all(`select * from daemons order by last_seen_at desc`).map(withActive);
    return activeOnly ? rows.filter((r) => r.is_active) : rows;
  }

  deleteDaemon(agentId: string): boolean {
    return this.run(`delete from daemons where agent_id = ?`, agentId) > 0;
  }

  // ==================================================================
  // Cohorts (collections)
  // ==================================================================

  listCohorts(): Row[] {
    return this.all(
      `select c.collection_id, c.source, c.label,
              (select count(*) from collection_samples cs where cs.collection_id = c.collection_id) as sample_count,
              c.created_at, c.updated_at
         from collections c order by c.created_at desc`,
    );
  }

  cohortExists(id: string): boolean {
    return !!this.first(`select 1 as x from collections where collection_id = ?`, id);
  }

  /** Per-cohort completion under the ACTIVE workflow versions (v1 default scope). */
  leaderboard(): Row[] {
    const rows = this.all(
      `select c.collection_id, c.source, c.label,
              (select count(*) from collection_samples cs where cs.collection_id = c.collection_id) as sample_count,
              (select count(distinct j.sample_id) from jobs j
                 join collection_samples cs on cs.sample_id = j.sample_id
                 join workflows w on w.id = j.workflow_pk
                where cs.collection_id = c.collection_id and w.status = 'active' and j.status = 'completed') as samples_completed,
              (select count(distinct j.sample_id) from jobs j
                 join collection_samples cs on cs.sample_id = j.sample_id
                 join workflows w on w.id = j.workflow_pk
                where cs.collection_id = c.collection_id and w.status = 'active' and j.status = 'failed') as samples_failed,
              (select count(distinct j.sample_id) from jobs j
                 join collection_samples cs on cs.sample_id = j.sample_id
                 join workflows w on w.id = j.workflow_pk
                where cs.collection_id = c.collection_id and w.status = 'active'
                  and j.status in ('claimed','submitted','running')) as samples_running,
              (select max(j.completed_at) from jobs j
                 join collection_samples cs on cs.sample_id = j.sample_id
                 join workflows w on w.id = j.workflow_pk
                where cs.collection_id = c.collection_id and w.status = 'active' and j.status = 'completed') as last_completed_at
         from collections c`,
    );
    return rows
      .map((r): Row => ({
        ...r,
        samples_remaining: (r.sample_count as number) - (r.samples_completed as number) - (r.samples_failed as number),
        completion_pct: r.sample_count ? round2((100 * (r.samples_completed as number)) / (r.sample_count as number)) : 0,
      }))
      .sort((a, b) => a.completion_pct - b.completion_pct || (b.sample_count as number) - (a.sample_count as number));
  }

  cohortSummary(id: string, opts: { workflow_id?: string | null; workflow_version?: string | null; all_workflows?: boolean }) {
    const c = this.first(`select * from collections where collection_id = ?`, id);
    if (!c) return null;
    const where = [`cs.collection_id = ?`];
    const args: any[] = [id];
    if (!opts.all_workflows && !opts.workflow_id) where.push(`w.status = 'active'`);
    if (opts.workflow_id) {
      where.push(`j.workflow_id = ?`);
      args.push(opts.workflow_id);
    }
    if (opts.workflow_version) {
      where.push(`j.workflow_version = ?`);
      args.push(opts.workflow_version);
    }
    const w = where.join(" and ");
    const counts: Row = {};
    for (const r of this.all(
      `select j.status, count(*) as n from jobs j
         join collection_samples cs on cs.sample_id = j.sample_id
         join workflows w on w.id = j.workflow_pk
        where ${w} group by j.status`,
      ...args,
    )) {
      counts[r.status] = r.n;
    }
    const sample_count = this.first(
      `select count(*) as n from collection_samples where collection_id = ?`,
      id,
    )!.n as number;
    const samples_completed = this.first(
      `select count(distinct j.sample_id) as n from jobs j
         join collection_samples cs on cs.sample_id = j.sample_id
         join workflows w on w.id = j.workflow_pk
        where ${w} and j.status = 'completed'`,
      ...args,
    )!.n as number;
    const g = (k: string) => (counts[k] as number) ?? 0;
    return {
      collection_id: id,
      source: c.source,
      label: c.label,
      workflow_id: opts.workflow_id ?? null,
      workflow_version: opts.workflow_version ?? null,
      sample_count,
      samples_completed,
      total_jobs: Object.values(counts).reduce((a: number, b: any) => a + b, 0),
      job_status_counts: {
        pending: g("pending"),
        claimed: g("claimed"),
        submitted: g("submitted"),
        running: g("running"),
        completed: g("completed"),
        failed: g("failed"),
      },
      completion_pct: sample_count ? round2((100 * samples_completed) / sample_count) : 0,
      // Needs per-task telemetry — historical tier, not the live tier.
      failure_by_process: [],
      generated_at_utc: iso(),
    };
  }

  // ==================================================================
  // Ops
  // ==================================================================

  stats(): Row {
    const byStatus = (rows: Row[]) => Object.fromEntries(rows.map((r) => [r.status, r.n]));
    return {
      samples: this.first(`select count(*) as n from samples`)!.n,
      workflows: this.first(`select count(*) as n from workflows`)!.n,
      // Both breakdowns come off the counter table: a handful of rows per
      // workflow version instead of a scan of every job.
      jobs_by_status: byStatus(
        this.all(`select status, sum(n) as n from job_counts where n > 0 group by status`),
      ),
      jobs_by_status_active: byStatus(
        this.all(
          `select c.status, sum(c.n) as n from job_counts c join workflows w on w.id = c.workflow_pk
            where w.status = 'active' and c.n > 0 group by c.status`,
        ),
      ),
      runs_by_status: byStatus(this.all(`select status, count(*) as n from runs group by status`)),
      dead_letter_unresolved: this.first(`select count(*) as n from dead_letter where resolved_at is null`)!.n,
      // The one number that decides whether this DO ever needs to be split.
      // A Durable Object's SQLite database is capped at 10 GB; jobs cost ~225
      // bytes/row measured, so the cap is ~44M job rows. Watch the percentage,
      // not the row count.
      storage: {
        bytes: this.sql.databaseSize,
        pct_of_limit: round2((100 * this.sql.databaseSize) / 10_737_418_240),
      },
    };
  }

  activeRunCount(): number {
    return this.first(`select count(*) as n from runs where status = 'running'`)!.n as number;
  }

  /** Pending work on active workflows that no live daemon is configured to claim. */
  dispatchability(): Row {
    const now = Date.now();
    const pending = this.all(
      `select c.workflow_pk, w.workflow_id, c.n as pending
         from job_counts c join workflows w on w.id = c.workflow_pk
        where c.status = 'pending' and w.status = 'active' and c.n > 0`,
    );
    const filters = this.all(`select workflow_id, last_seen_at from daemons`)
      .filter((d) => now - Date.parse(d.last_seen_at as string) < 2 * 60_000)
      .map((d) => {
        const wf = ((d.workflow_id as string) ?? "").trim();
        return wf ? new Set(wf.split(",").map((x) => x.trim()).filter(Boolean)) : null;
      });
    const stuck = pending
      .filter((p) => !filters.some((f) => f === null || f.has(p.workflow_id as string)))
      .map((p): Row => ({ ...p, reason: "no active daemon claims this workflow" }));
    return {
      checked_at: iso(),
      active_daemons: filters.length,
      stuck,
      stuck_pending_total: stuck.reduce((a, s) => a + (s.pending as number), 0),
    };
  }

  /** Runs whose RunDO timer may have been lost (belt-and-braces for the cron). */
  nonTerminalRuns(): Row[] {
    return this.all(
      `select run_name, status, claimed_at, submitted_at, started_at, last_heartbeat_at
         from runs where status not in (${RUN_TERMINAL.map(() => "?").join(",")})`,
      ...RUN_TERMINAL,
    );
  }

  /**
   * Archive and purge the jobs of retired workflow versions.
   *
   * `jobs` is the only table that grows as samples × workflow versions, so it
   * is the only one that can plausibly reach the 10 GB per-object ceiling
   * (~225 bytes/row measured, so ~44M rows). Once a version is retired its job
   * rows are pure ballast — the run ledger on R2 already holds the outcomes —
   * and removing them bounds the live table at samples × *active* versions.
   *
   * Retiring already purges `pending` jobs (that stops dispatch immediately);
   * this handles the rest, and deliberately runs from the daily cron rather
   * than from the retire transition so a PATCH never blocks on a large delete.
   */
  async archiveRetiredJobs(workflowPk?: number): Promise<Row> {
    const targets = workflowPk
      ? this.all(`select id from workflows where id = ? and status = 'retired'`, workflowPk)
      : this.all(`select id from workflows where status = 'retired'`);

    let workflows = 0;
    let archived = 0;
    for (const t of targets) {
      const pk = t.id as number;
      // Retiring does not cancel in-flight runs — they finish. Skip this
      // version while any job is still under a run; tomorrow's cron gets it.
      const inflight = this.first(
        `select coalesce(sum(n), 0) as n from job_counts
          where workflow_pk = ? and status in (${JOB_INFLIGHT.map(() => "?").join(",")})`,
        pk,
        ...JOB_INFLIGHT,
      )!.n as number;
      if (inflight > 0) continue;

      let n = 0;
      // Chunked so neither the JSON nor the R2 body is bounded by corpus size.
      // Between chunks control returns to the event loop, which is safe here
      // precisely because the version is retired: claim and reconcile both
      // filter on `active`, so no new job for this pk can appear mid-loop.
      for (;;) {
        const rows = this.all(`select * from jobs where workflow_pk = ? limit ?`, pk, ARCHIVE_CHUNK);
        if (!rows.length) break;
        const ids = rows.map((r) => r.id);
        const ph = ids.map(() => "?").join(",");
        const dlq = this.all(`select * from dead_letter where job_id in (${ph})`, ...ids);

        // R2 first: a failed put leaves the rows in place for the next run,
        // whereas a failed delete only costs a duplicate archive object.
        const key = `archive/jobs/${pk}/${iso().replace(/[:.]/g, "")}-${n}.ndjson.gz`;
        await this.env.STORE.put(key, await gzip(ndjson([...rows, ...dlq])));

        this.run(`delete from dead_letter where job_id in (${ph})`, ...ids);
        this.run(`delete from jobs where id in (${ph})`, ...ids);
        n += rows.length;
      }
      if (n) {
        // The delete trigger has already zeroed these; drop the empty rows.
        this.run(`delete from job_counts where workflow_pk = ?`, pk);
        workflows++;
        archived += n;
      }
    }
    return { workflows_archived: workflows, jobs_archived: archived };
  }

  /**
   * Empty every table. The teardown half of the dev loop — see reset() in the
   * Worker, which also clears R2 and the event sink.
   *
   * `storage.deleteAll()` would drop the SQL tables out from under the running
   * instance, leaving it broken until the constructor happened to re-run.
   * Deleting rows keeps the object usable and the schema intact.
   */
  reset(): Row {
    const tables = [
      "job_counts",
      "jobs",
      "dead_letter",
      "runs",
      "collection_samples",
      "collections",
      "samples",
      "workflows",
      "daemons",
    ];
    const cleared: Row = {};
    for (const t of tables) {
      cleared[t] = this.first(`select count(*) as n from ${t}`)!.n;
      this.sql.exec(`delete from ${t}`);
    }
    return cleared;
  }

  /** Whole-DO export for the daily R2 snapshot (spec §10 disaster recovery). */
  snapshot(): Row {
    const tables = [
      "samples",
      "collections",
      "collection_samples",
      "workflows",
      "jobs",
      "runs",
      "dead_letter",
      "daemons",
    ];
    return Object.fromEntries(tables.map((t) => [t, this.all(`select * from ${t}`)]));
  }

  private async appendLedger(runName: string, outcome: Row): Promise<void> {
    const d = new Date();
    const key = `ledger/${d.getUTCFullYear()}/${String(d.getUTCMonth() + 1).padStart(2, "0")}/${runName}.jsonl`;
    // ponytail: the key is the idempotency token — same run, same object. The
    // spec's content-hash naming buys nothing while one run closes once.
    await this.env.STORE.put(key, JSON.stringify(outcome) + "\n");
  }
}

// ====================================================================
// helpers
// ====================================================================

function iso(): string {
  return new Date().toISOString();
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

function safeJson(v: any): Row {
  if (!v) return {};
  try {
    return JSON.parse(v as string) ?? {};
  } catch {
    return {};
  }
}

/** Sorted, deduplicated, ';'-joined — v1's canonical SRR form. */
function normalizeSrrs(acc: string): string {
  return [...new Set(acc.split(/[;,\s]+/).map((s) => s.trim()).filter(Boolean))].sort().join(";");
}

const DAEMON_ACTIVE_MS = 2 * 60_000;
function withActive(row: Row): Row {
  return { ...row, is_active: Date.now() - Date.parse(row.last_seen_at as string) < DAEMON_ACTIVE_MS };
}

const RUN_STALE_MS = 15 * 60_000;

/**
 * Derived run state. Ported verbatim from v1 routers/runs.py `_classify_run`
 * — the two shapes worth naming are a driver that exited non-zero
 * (`wrapper-failed`) and a run that ended without ever uploading its
 * .nextflow.log (`ended-no-log`, the signature of a hard-killed allocation).
 */
export function classifyRun(row: Row): string {
  const wec = row.wrapper_exit_code;
  if (wec != null && wec !== 0) return "wrapper-failed";
  if (["claimed", "submitted", "running"].includes(row.status as string)) {
    const hb = row.last_heartbeat_at ?? row.submitted_at ?? row.claimed_at;
    if (hb && Date.now() - Date.parse(hb as string) > RUN_STALE_MS) return "stalled";
    return "active";
  }
  if (row.nextflow_log_uploaded_at == null) return "ended-no-log";
  return (row.status as string) ?? "unknown";
}
