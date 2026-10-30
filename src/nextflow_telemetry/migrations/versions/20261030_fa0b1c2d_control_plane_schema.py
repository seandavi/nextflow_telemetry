"""Control-plane schema (ADR-0013, #371); v1 tables renamed to v1_*.

Revision ID: fa0b1c2d
Revises: e9fa0b1c

Runs online only: the rename reads pg_catalog, so `alembic upgrade --sql` is not
supported. No data is copied; the new tables start empty (M6 imports v2 state).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "fa0b1c2d"
down_revision = "e9fa0b1c"
branch_labels = None
depends_on = None

V1_TABLES = ("telemetry", "task_executions", "samples", "collections",
             "collection_samples", "workflows", "jobs", "workflow_runs",
             "task_logs", "dead_letter", "daemon_agents")

NEW_TABLES_REVERSE = (
    "curation_decisions", "ingest_cursor", "run_failed_tasks", "run_task_counts",
    "daemons", "dead_letter", "jobs", "runs", "workflows", "collection_samples",
    "collections", "readset_runs", "samples", "study_aliases", "studies",
)

NO_DEADLINE = "num_nonnulls(claim_expires_at, submit_deadline, liveness_deadline) = 0"


def _owned_relations(table: str) -> list[tuple[str, str]]:
    """(kind, name) for every index and owned sequence of `table` in the current schema.
    kind is "INDEX" or "SEQUENCE", ready for ALTER <kind> … RENAME."""
    rows = op.get_bind().execute(sa.text("""
        select 'INDEX', indexname from pg_indexes
         where schemaname = current_schema() and tablename = :t
        union all
        select 'SEQUENCE', s.relname from pg_class s
          join pg_depend d on d.objid = s.oid
         where d.classid = 'pg_class'::regclass and d.refclassid = 'pg_class'::regclass
           and d.refobjid = to_regclass(:t) and s.relkind = 'S' and d.deptype in ('a', 'i')
    """), {"t": table})
    return [(kind, name) for kind, name in rows]


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _now(name: str) -> sa.Column:
    return _ts(name, nullable=False, server_default=sa.text("now()"))


def upgrade() -> None:
    # Step 1: rename v1. Indexes and sequences share the schema's relation namespace,
    # so they are renamed too or the new tables could not create samples_pkey etc.
    for t in V1_TABLES:
        op.rename_table(t, f"v1_{t}")
        for kind, name in _owned_relations(f"v1_{t}"):
            op.execute(f'ALTER {kind} "{name}" RENAME TO "v1_{name}"')

    # Step 2: new tables.
    op.create_table(
        "studies",
        sa.Column("study_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("name", name="uq_studies_name"),
    )
    op.create_table(
        "study_aliases",
        sa.Column("study_id", sa.Integer(), sa.ForeignKey("studies.study_id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheme", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("scheme", "value", "study_id"),
        sa.CheckConstraint(
            "scheme in ('cmd_study','bioproject','insdc_study','asap_code','tracker_name','doi','pmid')",
            name="ck_study_aliases_scheme",
        ),
    )
    op.create_index("ix_study_aliases_study_id", "study_aliases", ["study_id"])

    op.create_table(
        "samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sample_id", sa.String(), nullable=False),
        sa.Column("readset_id", sa.String(), nullable=True),
        sa.Column("ncbi_accession", sa.Text(), nullable=True),
        sa.Column("biosample_id", sa.String(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("sample_id", name="uq_samples_sample_id"),
    )
    op.create_index("ix_samples_readset_id", "samples", ["readset_id"])
    op.create_index("ix_samples_biosample_id", "samples", ["biosample_id"])

    op.create_table(
        "readset_runs",
        sa.Column("run_accession", sa.String(), primary_key=True),
        sa.Column("readset_id", sa.String(), nullable=False),
        sa.Column("biosample", sa.String(), nullable=True),
        sa.Column("sra_sample", sa.String(), nullable=True),
        sa.Column("experiment", sa.String(), nullable=True),
        sa.Column("bioproject", sa.String(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint("run_accession ~ '^[SED]RR[0-9]+$'", name="ck_readset_runs_run_accession"),
    )
    op.create_index("ix_readset_runs_readset_id", "readset_runs", ["readset_id"])

    op.create_table(
        "collections",
        sa.Column("collection_id", sa.String(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("study_id", sa.Integer(), sa.ForeignKey("studies.study_id"), nullable=True),
        _now("created_at"),
        _now("updated_at"),
    )
    op.create_index("ix_collections_study_id", "collections", ["study_id"])

    op.create_table(
        "collection_samples",
        sa.Column("collection_id", sa.String(), sa.ForeignKey("collections.collection_id"), primary_key=True),
        sa.Column("sample_id", sa.String(), sa.ForeignKey("samples.sample_id"), primary_key=True),
        _now("created_at"),
    )
    op.create_index("ix_collection_samples_sample_id", "collection_samples", ["sample_id"])

    op.create_table(
        "workflows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("repository_url", sa.String(), nullable=False),
        sa.Column("revision", sa.String(), nullable=False),
        sa.Column("manifest_version", sa.String(), nullable=True),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("collections", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("sample_key", sa.String(), nullable=False, server_default="readset_id"),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("workflow_id", "version", name="uq_workflows_workflow_id_version"),
        sa.CheckConstraint("status in ('active','paused','retired')", name="ck_workflows_status"),
        sa.CheckConstraint("sample_key in ('sample_id','readset_id')", name="ck_workflows_sample_key"),
        sa.CheckConstraint("collections is null or cardinality(collections) > 0", name="ck_workflows_collections"),
        sa.CheckConstraint("max_retries >= 0", name="ck_workflows_max_retries"),
    )
    # "params fixed per version" (ADR-0010): callers see IntegrityError (check_violation).
    op.execute("""
        CREATE FUNCTION workflows_bundle_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.params IS DISTINCT FROM OLD.params OR NEW.sample_key IS DISTINCT FROM OLD.sample_key THEN
            RAISE EXCEPTION 'workflow % %: params and sample_key are fixed per version (ADR-0010)',
              OLD.workflow_id, OLD.version USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_workflows_bundle_immutable BEFORE UPDATE OF params, sample_key ON workflows
          FOR EACH ROW EXECUTE FUNCTION workflows_bundle_immutable()
    """)

    op.create_table(
        "runs",
        sa.Column("run_name", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("workflow_version", sa.String(), nullable=True),
        sa.Column("workflow_pk", sa.Integer(), sa.ForeignKey("workflows.id"), nullable=True),
        sa.Column("revision", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("executor_job_id", sa.String(), nullable=True),
        _ts("claimed_at"),
        _ts("submitted_at"),
        _ts("started_at"),
        _ts("completed_at"),
        _ts("last_heartbeat_at"),
        sa.Column("wait_seconds", sa.Integer(), nullable=True),
        sa.Column("wrapper_exit_code", sa.Integer(), nullable=True),
        sa.Column("last_known_slurm_state", sa.String(), nullable=True),
        sa.Column("slurm_reason", sa.Text(), nullable=True),
        _ts("nextflow_log_uploaded_at"),
        _ts("claim_expires_at"),
        _ts("submit_deadline"),
        _ts("liveness_deadline"),
        sa.CheckConstraint(
            "status in ('claimed','submitted','running','completed','failed','expired')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint(
            "num_nonnulls(claim_expires_at, submit_deadline, liveness_deadline) <= 1",
            name="ck_runs_one_deadline",
        ),
        sa.CheckConstraint(
            f"status not in ('completed','failed','expired') or (completed_at is not null and {NO_DEADLINE})",
            name="ck_runs_terminal_closed",
        ),
    )
    op.create_index("ix_runs_status_claimed_at", "runs", ["status", "claimed_at"])
    for col in ("claim_expires_at", "submit_deadline", "liveness_deadline"):
        op.create_index(f"ix_runs_{col}", "runs", [col], postgresql_where=sa.text(f"{col} IS NOT NULL"))

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sample_id", sa.String(), sa.ForeignKey("samples.sample_id"), nullable=False),
        sa.Column("workflow_pk", sa.Integer(), sa.ForeignKey("workflows.id"), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("workflow_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("run_name", sa.String(), sa.ForeignKey("runs.run_name"), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        _now("created_at"),
        _ts("completed_at"),
        _ts("failed_at"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("sample_id", "workflow_pk", name="uq_jobs_sample_id_workflow_pk"),
        sa.CheckConstraint(
            "status in ('pending','claimed','submitted','running','completed','failed')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint("status <> 'completed' or completed_at is not null", name="ck_jobs_completed_at"),
        sa.CheckConstraint("status <> 'failed' or failed_at is not null", name="ck_jobs_failed_at"),
        sa.CheckConstraint("(status = 'pending') = (run_name is null)", name="ck_jobs_run_name"),
        sa.CheckConstraint("retry_count >= 0", name="ck_jobs_retry_count"),
    )
    op.create_index(
        "ix_jobs_pending_claim", "jobs",
        ["workflow_pk", sa.text("priority DESC"), "created_at", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_jobs_workflow_pk_status", "jobs", ["workflow_pk", "status"])
    op.create_index("ix_jobs_run_name", "jobs", ["run_name"])

    op.create_table(
        "dead_letter",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("run_name", sa.String(), sa.ForeignKey("runs.run_name"), nullable=True),
        sa.Column("sample_id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("workflow_version", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        _now("created_at"),
        _ts("resolved_at"),
        sa.UniqueConstraint("job_id", name="uq_dead_letter_job_id"),
    )

    op.create_table(
        "daemons",
        sa.Column("agent_id", sa.String(), primary_key=True),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("profile", sa.String(), nullable=True),
        sa.Column("nf_client_version", sa.String(), nullable=True),
        sa.Column("config_yaml", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("batch_size", sa.Integer(), nullable=True),
        sa.Column("max_concurrent_runs", sa.Integer(), nullable=True),
        sa.Column("active_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="idle"),
        _ts("last_seen_at", nullable=False),
        _ts("started_at", nullable=False),
        sa.CheckConstraint("status in ('idle','running','stopped')", name="ck_daemons_status"),
    )
    op.create_index("ix_daemons_last_seen_at", "daemons", ["last_seen_at"])

    op.create_table(
        "run_task_counts",
        sa.Column("run_name", sa.String(), sa.ForeignKey("runs.run_name"), primary_key=True),
        sa.Column("status", sa.String(), primary_key=True),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.CheckConstraint("n >= 0", name="ck_run_task_counts_n"),
    )

    op.create_table(
        "run_failed_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_name", sa.String(), sa.ForeignKey("runs.run_name"), nullable=False),
        sa.Column("process", sa.String(), nullable=False),
        sa.Column("sample_id", sa.String(), nullable=True),
        sa.Column("exit_code", sa.String(), nullable=True),
        sa.Column("task_hash", sa.String(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("error_action", sa.String(), nullable=True),
        _ts("utc_time", nullable=False),
    )
    op.create_index("ix_run_failed_tasks_run_name", "run_failed_tasks", ["run_name"])

    op.create_table(
        "ingest_cursor",
        sa.Column("stream", sa.String(), primary_key=True),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("line_offset", sa.BigInteger(), nullable=False, server_default="0"),
        _now("updated_at"),
        sa.CheckConstraint("line_offset >= 0", name="ck_ingest_cursor_line_offset"),
    )

    op.create_table(
        "curation_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        _now("at"),
        sa.CheckConstraint("subject_type in ('study','readset','run')", name="ck_curation_decisions_subject_type"),
        sa.CheckConstraint("decision in ('include','exclude')", name="ck_curation_decisions_decision"),
    )
    op.create_index("ix_curation_decisions_subject", "curation_decisions", ["subject_type", "subject_id", "at"])

    # Step 3: submission provenance.
    op.add_column("submissions", sa.Column("source_snapshot_source_id", sa.String(80), nullable=True))
    op.add_column("submissions", sa.Column("source_snapshot_asset_id", sa.String(80), nullable=True))
    op.add_column("submissions", sa.Column("source_snapshot_version", sa.String(300), nullable=True))
    op.create_foreign_key(
        "fk_submissions_source_snapshot", "submissions", "metadata_source_snapshots",
        ["source_snapshot_source_id", "source_snapshot_asset_id", "source_snapshot_version"],
        ["source_id", "asset_id", "version"],
    )
    op.create_check_constraint(
        "ck_submissions_source_snapshot", "submissions",
        "num_nulls(source_snapshot_source_id, source_snapshot_asset_id, source_snapshot_version) in (0, 3)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_submissions_source_snapshot", "submissions", type_="check")
    op.drop_constraint("fk_submissions_source_snapshot", "submissions", type_="foreignkey")
    for col in ("source_snapshot_version", "source_snapshot_asset_id", "source_snapshot_source_id"):
        op.drop_column("submissions", col)

    op.execute("DROP TRIGGER trg_workflows_bundle_immutable ON workflows")
    op.execute("DROP FUNCTION workflows_bundle_immutable()")
    for t in NEW_TABLES_REVERSE:
        op.drop_table(t)

    for t in V1_TABLES:
        for kind, name in _owned_relations(f"v1_{t}"):
            if name.startswith("v1_"):
                op.execute(f'ALTER {kind} "{name}" RENAME TO "{name[3:]}"')
        op.rename_table(f"v1_{t}", t)
