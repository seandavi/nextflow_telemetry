# STATUS — nf_telemetry v2 (Cloudflare control plane)

_Card updated 2026-09-21 (previous: 2026-08-18). Update it rather than starting a
fresh one, so `What changed` keeps working._

v2 has now been driven end to end by the real client on a real clock: loading,
dispatch, the wrapper, every failure profile, and both timers. Nothing on a
cluster points at it yet, and the next step is the first real batch from Alpine.

## Current state

| | | anchor |
|---|---|---|
| ✓ | Worker deployed; 16 tests green; `authExempt` and `resetAllowed` unit-tested | `cf/test/lifecycle.test.ts` |
| ✓ | Loaded through unmodified `nf-client add-samples` / `add-cmd`; idempotent; cohorts appear | [#183](https://github.com/seandavi/nextflow_telemetry/issues/183) closed |
| ✓ | Local e2e: 94-run happy path, wrapper by hand, `fail-mark` / `fail-fetch` / `stochastic`, claim expiry at +5:00, liveness at +10:00 | [#181](https://github.com/seandavi/nextflow_telemetry/issues/181) closed |
| ✓ | `just v2-e2e` reproduces the loop from an empty catalog | `justfile`, `cf/README.md` |
| ✓ | ADR written; sequence + state diagrams | `docs/adr/0006`, `cf/docs/diagrams.md` |
| ✓ | Both clusters: `~/.nf_tel.env` path standard, `main` checkout, housekeeping done | `docs/hpc-layout.md` |
| → | Deployment holds the local test corpus (nf_testing, 102 samples) and one run parked in `submitted` behind the 48 h backstop | `/api/admin/stats` |
| ○ | No daemon running on either cluster; both configs still point at v1 | [#178](https://github.com/seandavi/nextflow_telemetry/issues/178) |
| ○ | 8 analytical endpoints 501; `/submissions`, `/curated`, OAuth not built | #175, #180, #176 |

## What changed since 2026-08-18

- Reset landed (#172) and became the dev loop.
- Two bugs found by the local loop, both fixed: v2 rejected every wrapper event
  with 401 (v1 never required auth there); `nf-client` published its bearer
  token in daemon heartbeats on the open `/daemons` listing. That token should
  be rotated before it goes into a cluster config.
- `nf-client` must run from its own venv; root venv click 8.3 breaks typer 0.12.
- Branch pushed; PR #182 open.

## Key decisions

Recorded in [ADR 0006](../docs/adr/0006-cloudflare-control-plane.md). In one
line each: one ControlDO, DO alarms instead of Workflows and sweepers, NDJSON on
R2 instead of Pipelines, reprocess instead of migrating jobs, `job_counts` by
trigger, wrapper and weblog routes open.

## Your attention

1. **Rotate the v2 operator token** (GSM + `wrangler secret put API_TOKEN`).
2. **Take #178.** Prerequisites are listed on the issue: reinstall nf-client on
   the login node from `$NF_TEL_REPO`, add `token:` to the cluster yaml,
   `just v2-reset`, register `cmgd_nextflow 2.2.1` and one study.
3. **Decide #175 / #176 / #180.** None blocks #178; all block the dashboard.

## Important references

- [#170](https://github.com/seandavi/nextflow_telemetry/issues/170) — the map
- `cf/README.md` — deviations, dev loop, endpoint status, cost shape
- `cf/docs/diagrams.md` — ERD, topology, run sequence, timers, state machines
- `docs/adr/0006-cloudflare-control-plane.md` — the decision
- `docs/hpc-layout.md` — cluster paths and storage facts
