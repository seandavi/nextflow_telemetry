#!/usr/bin/env python3
"""Copy the v1 catalog (Postgres) into the v2 control plane (Cloudflare).

Catalog only — samples with their collection membership, and workflow versions
that are not retired. Event history (`telemetry`, `task_executions`,
`task_logs`) is deliberately left behind: v2 keeps that class of data on R2, and
the v1 rows span 2026-05-12 to 2026-07-09 only.

Whether `jobs` come too is a separate call, because they are not history — they
record which samples are already processed. Pass --jobs to bring them.

Reads Postgres through `docker exec pg_main psql` (the database only listens on
127.0.0.1 of the container host) and writes through the v2 HTTP API, so the
wire protocol validates every row on the way in. Idempotent: every write is an
upsert keyed on the same content address v1 used, so re-running converges.

  cf/scripts/migrate_from_v1.py --dry-run
  cf/scripts/migrate_from_v1.py
  cf/scripts/migrate_from_v1.py --jobs
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://nf-telemetry.seandavi.workers.dev"
PG_CONTAINER = "pg_main"
PG_DB = "nf_telemetry"
PG_USER = "nf_telemetry"
API_SECRET = "cdsci-nf-telemetry-v2-api-token"
GCP_PROJECT = "cdsci-infra"
# The API is a single Worker fronting one Durable Object; the object serialises
# writes anyway, so more than a handful of connections buys nothing.
WORKERS = 8


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def secret(name: str) -> str:
    return sh(["gcloud", "secrets", "versions", "access", "latest",
               f"--secret={name}", f"--project={GCP_PROJECT}"])


def pg_password() -> str:
    env = sh(["docker", "inspect", "nf_telemetry_api",
              "--format", "{{range .Config.Env}}{{println .}}{{end}}"])
    for line in env.splitlines():
        if "SQLALCHEMY_URI" in line:
            return line.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    sys.exit("could not read the v1 database password from the API container")


def query(sql: str, password: str) -> list[dict]:
    """Run SQL in the v1 database and return rows, one JSON object per row."""
    out = sh(["docker", "exec", "-e", f"PGPASSWORD={password}", PG_CONTAINER,
              "psql", "-U", PG_USER, "-d", PG_DB, "-tAc", sql])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def post(path: str, body: dict, token: str) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "Authorization": f"Bearer {token}",
            # Cloudflare's managed bot rules reject the default Python-urllib
            # agent with a 403 (error 1010) before the request reaches the
            # Worker. Any ordinary agent string gets through.
            "User-Agent": "nf-telemetry-migrate/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # network flake — reported, never fatal mid-run
        return 0, str(e)


SAMPLES_SQL = """
select json_build_object(
  'sample_id',      s.sample_id,
  'ncbi_accession', s.ncbi_accession,
  'biosample_id',   s.biosample_id,
  'metadata',       coalesce(s.metadata_, '{}'::jsonb),
  'collections',    coalesce((select json_agg(cs.collection_id)
                                from collection_samples cs
                               where cs.sample_id = s.sample_id), '[]'::json)
)::text
  from samples s
 where s.ncbi_accession is not null and s.ncbi_accession <> ''
 order by s.id
"""

WORKFLOWS_SQL = """
select json_build_object(
  'workflow_id',      w.workflow_id,
  'version',          w.version,
  'repository_url',   w.repository_url,
  'revision',         w.revision,
  'manifest_version', w.manifest_version,
  'max_retries',      w.max_retries,
  'description',      w.description,
  'status',           w.status
)::text
  from workflows w
 where w.status <> 'retired'
 order by w.id
"""

# Jobs are only meaningful for the versions we carried over, and only the
# terminal ones are worth moving: anything else reconcile recreates as pending.
JOBS_SQL = """
select json_build_object(
  'sample_id',        j.sample_id,
  'workflow_id',      j.workflow_id,
  'workflow_version', j.workflow_version,
  'status',           j.status,
  'retry_count',      j.retry_count,
  'completed_at',     j.completed_at,
  'failed_at',        j.failed_at,
  'failure_reason',   j.failure_reason
)::text
  from jobs j join workflows w on w.id = j.workflow_pk
 where w.status <> 'retired' and j.status in ('completed', 'failed')
 order by j.id
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report what would move, write nothing")
    ap.add_argument("--jobs", action="store_true",
                    help="also carry terminal jobs, so completed samples are not reprocessed")
    args = ap.parse_args()

    pw = pg_password()
    samples = query(SAMPLES_SQL, pw)
    workflows = query(WORKFLOWS_SQL, pw)
    jobs = query(JOBS_SQL, pw) if args.jobs else []

    print(f"samples   : {len(samples)}")
    print(f"workflows : {len(workflows)}  " +
          ", ".join(f"{w['workflow_id']}@{w['version']} ({w['status']})" for w in workflows))
    if args.jobs:
        done = sum(1 for j in jobs if j["status"] == "completed")
        print(f"jobs      : {len(jobs)} terminal ({done} completed, {len(jobs) - done} failed)")
    if args.dry_run:
        print("\ndry run — nothing written")
        return

    token = secret(API_SECRET)
    failures: list[str] = []

    # Workflows first: a sample's jobs cannot exist before its workflow does.
    for w in workflows:
        status = w.pop("status")
        code, err = post("/api/workflows", w, token)
        if code != 201:
            failures.append(f"workflow {w['workflow_id']}@{w['version']}: {code} {err}")
        elif status != "active":
            print(f"  note: {w['workflow_id']}@{w['version']} was '{status}' in v1 — "
                  f"registered active, PATCH it back if that matters")

    def send_sample(s: dict) -> None:
        collections = s.pop("collections") or [None]
        # One POST per membership: the upsert is keyed on sample_id, and each
        # call adds one collection, so N calls converge on N memberships.
        for c in collections:
            body = {k: v for k, v in s.items() if v is not None}
            if c:
                body["collection"] = c
            code, err = post("/api/samples", body, token)
            if code != 201:
                failures.append(f"sample {s['sample_id']}: {code} {err}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(send_sample, samples))
    print(f"samples written ({len(failures)} failures so far)")

    if jobs:
        # Reconcile first so a pending job exists for every (sample, version),
        # then replay the terminal ones onto it.
        code, _ = post("/api/admin/reconcile-jobs", {}, token)
        print(f"reconcile : {code}")
        code, err = post("/api/admin/import-jobs", {"jobs": jobs}, token)
        if code != 200:
            failures.append(f"import-jobs: {code} {err}")
        else:
            print(f"jobs      : {len(jobs)} imported")

    for f in failures[:20]:
        print("FAIL", f, file=sys.stderr)
    print(f"\ndone — {len(failures)} failures")


if __name__ == "__main__":
    main()
