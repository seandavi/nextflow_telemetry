/**
 * The contract is served by the Worker and covers every route the router has.
 * Coverage is checked against the live router, not a hand-kept list, so adding
 * a route without documenting it fails here.
 */
import { SELF } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { api } from "../src/index";

const DEPRECATED = ["/dispatch/requeue-expired", "/admin/expire-stale-runs", "/admin/heartbeat-watchdog"];

/** Hono route path → OpenAPI path: `:pk{[0-9]+}` → `{pk}`, `:task_hash{.+}` → `{task_hash}`. */
const toOpenAPI = (p: string) => p.replace(/:([A-Za-z_]+)(\{[^}]*\})?/g, "{$1}");

describe("openapi", () => {
  it("serves a 3.1 document with a bearer scheme", async () => {
    const res = await SELF.fetch("https://x/openapi.json");
    expect(res.status).toBe(200);
    const doc = (await res.json()) as any;
    expect(doc.openapi).toMatch(/^3\.1/);
    expect(doc.components.securitySchemes.bearer.scheme).toBe("bearer");
    expect(doc.info.title).toBe("nf_telemetry v2");
  });

  it("documents every route in the router", async () => {
    const doc = (await (await SELF.fetch("https://x/openapi.json")).json()) as any;
    const missing: string[] = [];
    for (const r of api.routes) {
      if (r.method === "ALL" || r.path === "/openapi.json" || r.path === "/docs") continue;
      const op = doc.paths[toOpenAPI(r.path)]?.[r.method.toLowerCase()];
      if (!op) missing.push(`${r.method} ${r.path}`);
      else expect(op.summary, `${r.method} ${r.path} has no summary`).toBeTruthy();
    }
    expect(missing).toEqual([]);
  });

  it("flags the no-op sweeper routes as deprecated", async () => {
    const doc = (await (await SELF.fetch("https://x/openapi.json")).json()) as any;
    for (const p of DEPRECATED) expect(doc.paths[p].post.deprecated, p).toBe(true);
    expect(doc.paths["/dispatch/batch"].post.deprecated).toBeUndefined();
  });

  it("renders the docs page without auth", async () => {
    const res = await SELF.fetch("https://x/docs");
    expect(res.status).toBe(200);
    expect(await res.text()).toContain("openapi.json");
  });
});
