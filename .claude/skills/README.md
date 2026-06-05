# Skills

Checked-in Claude Code skills for the cmgd Nextflow telemetry system. Each is a
folder with a `SKILL.md` (model-triggered by its `description`) and optional
`scripts/`. They encode the hard-won, non-obvious operational knowledge of this
system — the stuff that isn't derivable from the code.

| Skill | Use it when |
|---|---|
| `telemetry-api` | Querying the backend (job-summary, runs, dispatchability, reconcile). Shared base for the others. |
| `cmgd-triage` | A run failed / is stuck / has no logs; DLQ; "lots of failures". Classification → root cause → right log. |
| `cmgd-release` | Cutting + rolling out a new pipeline version (manifest/tag/registry lockstep, retire, reconcile). |
| `alpine-daemon` | Deploy/restart/health-check the nf-client daemon on an HPC login node. |
| `cmgd-config` | "Where is X configured / how do I change it?" — the config-location index. |
| `gcs-verify` | Confirm + locate pipeline outputs in GCS (`rclone gs1:`). |
| `adr-author` | Recording a behavior-affecting architecture decision (`docs/adr/`). |

Skills reference each other by name (e.g. `cmgd-triage` builds on `telemetry-api`).
Keep the **Gotchas** sections current — they are the highest-value part.
