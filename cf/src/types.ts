import type { ControlDO } from "./control-do";
import type { RunDO } from "./run-do";
import type { SinkDO } from "./sink-do";

export interface Env {
  CONTROL: DurableObjectNamespace<ControlDO>;
  RUN: DurableObjectNamespace<RunDO>;
  SINK: DurableObjectNamespace<SinkDO>;
  STORE: R2Bucket;

  CLAIM_TTL_MINUTES: string;
  SUBMIT_BACKSTOP_HOURS: string;
  LIVENESS_MINUTES: string;
  CORS_ORIGINS: string;
  /** When set, bearer auth is enforced on mutating routes. */
  API_TOKEN?: string;
}

/**
 * A DO stub's methods, typed as themselves-but-async.
 *
 * The runtime types express the same thing via `Rpc.Serializable`, but that
 * constraint expands forever the moment a `Record<string, any>` (i.e. any JSON
 * request body) reaches an argument. Since every payload here is plain JSON by
 * construction, mapping the class is both accurate and finite.
 */
type Async<T> = {
  [K in keyof T]: T[K] extends (...a: infer A) => infer R ? (...a: A) => Promise<Awaited<R>> : never;
};

/** There is one ControlDO and one SinkDO; RunDO is keyed by run_name. */
export const control = (env: Env) =>
  env.CONTROL.get(env.CONTROL.idFromName("v1")) as unknown as Async<ControlDO>;
export const sink = (env: Env) => env.SINK.get(env.SINK.idFromName("v1")) as unknown as Async<SinkDO>;
export const runDo = (env: Env, runName: string) =>
  env.RUN.get(env.RUN.idFromName(runName)) as unknown as Async<RunDO>;
