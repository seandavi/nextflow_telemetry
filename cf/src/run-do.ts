/**
 * RunDO — one per run_name. Two jobs, both about time:
 *
 *   1. Hold the deadline for whatever the run is currently waiting on, as a
 *      Durable Object alarm. This is what replaces v1's cron sweepers
 *      (`requeue-expired`, `expire-stale-runs`, `heartbeat-watchdog`): the
 *      expiry policy IS the timer, so nothing has to scan for stragglers.
 *
 *   2. Absorb heartbeats. These are the highest-frequency write in the system
 *      (one per run per 60s) and carry almost no information, so they must not
 *      land on ControlDO, which every claim serializes on. The DO re-arms its
 *      own alarm locally and pushes `last_heartbeat_at` upstream at most once
 *      every HEARTBEAT_PUSH_MS so the dashboard still has a fresh-enough value.
 *
 * The alarm never writes terminal state itself — it calls ControlDO, which
 * owns every status write and no-ops if the run is already closed. One close
 * path, reachable from the weblog, the timer, or an operator.
 */
import { DurableObject } from "cloudflare:workers";
import { control, type Env } from "./types";

type Phase = "claim" | "backstop" | "liveness";

/** Dashboard freshness for last_heartbeat_at; well inside the 15-min stale threshold. */
const HEARTBEAT_PUSH_MS = 5 * 60_000;

export class RunDO extends DurableObject<Env> {
  /** Arm (or re-arm) the deadline for the phase the run just entered. */
  async arm(runName: string, phase: Phase, ms: number): Promise<void> {
    await this.ctx.storage.put({ runName, phase });
    await this.ctx.storage.setAlarm(Date.now() + ms);
  }

  /** A wrapper heartbeat: push the liveness deadline out, throttle the upstream write. */
  async heartbeat(runName: string, livenessMs: number): Promise<void> {
    const lastPush = (await this.ctx.storage.get<number>("lastPush")) ?? 0;
    await this.ctx.storage.put({ runName, phase: "liveness" as Phase });
    await this.ctx.storage.setAlarm(Date.now() + livenessMs);
    if (Date.now() - lastPush > HEARTBEAT_PUSH_MS) {
      await this.ctx.storage.put("lastPush", Date.now());
      await control(this.env).recordHeartbeat(runName, new Date().toISOString());
    }
  }

  /** The run reached a terminal signal by other means — drop the timer and the state. */
  async finalize(): Promise<void> {
    await this.ctx.storage.deleteAll();
  }

  async alarm(): Promise<void> {
    const runName = await this.ctx.storage.get<string>("runName");
    const phase = await this.ctx.storage.get<Phase>("phase");
    if (!runName || !phase) return;

    const ctl = control(this.env);
    if (phase === "claim") {
      // Claim TTL: the executor never confirmed submission. Jobs go straight
      // back to pending without burning a retry — nothing was attempted.
      await ctl.expireClaim(runName);
    } else if (phase === "backstop") {
      await ctl.closeRun(runName, "failed", "no wrapper activity within submit backstop");
    } else {
      await ctl.closeRun(runName, "failed", "presumed dead: heartbeats stopped");
    }
    await this.ctx.storage.deleteAll();
  }
}
