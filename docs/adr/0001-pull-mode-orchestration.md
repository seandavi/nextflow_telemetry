# 0001. Pull-mode HPC orchestration

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** Sean Davis

## Context

The backend dispatches Nextflow work to multiple HPC clusters (Alpine, Anvil,
and future sites) that we do not administer. Two integration shapes are
possible:

- **Push** — an orchestrator on our host (`onclappc02`) SSHes out to each
  cluster's login node to submit and monitor jobs.
- **Pull** — a long-running `nf-client` daemon on each cluster's login node
  initiates outbound HTTPS to the API, claims batches, and reports status.

Constraints in tension:

- The campus firewall **blocks outbound TCP/22 from `onclappc02`** (SYNs are
  silently dropped), so push-mode SSH does not work without a firewall
  exception. ICMP and HTTPS egress are clean.
- The available SSH workaround (ProxyJump through a PHI/HIPAA host) inverts the
  trust boundary — using a more-sensitive host as an egress proxy for
  less-sensitive research workloads is a security smell.
- ACCESS clusters allow outbound HTTPS from login nodes and tolerate a small
  long-lived poller there.

## Decision

We will use **pull-mode**: an `nf-client` daemon runs on each cluster's login
node and initiates outbound HTTPS to the API (`/dispatch/batch`,
`/dispatch/submitted`, `/runs/.../event`, `/daemons/heartbeat`). The server is
never required to initiate a connection into a cluster. Push-mode remains
documented in `docs/orchestrator-architecture.md` as a deferred option, gated on
a firewall exception being granted.

## Alternatives considered

- **Push-mode (orchestrator SSHes out)** — rejected as the default: blocked by
  the outbound-22 firewall rule, and the ProxyJump workaround is a trust-boundary
  violation. Reconsider only if a scoped firewall exception is granted.
- **A managed workflow service per site** — far more integration surface than a
  single config-driven daemon; not justified for the current set of clusters.

## Consequences

- Works across heterogeneous network postures: the daemon only needs outbound
  HTTPS, which every site already permits. Firewall-friendly by construction.
- The cluster-side daemon is now a critical, mostly-invisible component. Its
  health must be observable (heartbeats; see [0003](0003-dispatchability-detection.md))
  and it must tolerate API outages without dying (nf-client #106).
- Per-cluster credentials/config live on the cluster, not centrally.
- Adding a cluster means standing up a daemon there, not opening inbound access.

## References

- `docs/orchestrator-architecture.md` (push-mode deferred design + prerequisites).
- `packages/nf_client/` (the daemon).
- Network constraint: outbound TCP/22 blocked from `onclappc02`
  (same family as the UDP/53 block in monode `NETWORK_CONSTRAINTS.md`).
