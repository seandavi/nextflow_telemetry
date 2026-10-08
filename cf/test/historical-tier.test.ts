/**
 * The eight historical-tier routes proxy GETs to the catalog service when
 * CATALOG_URL is set, and keep answering 501 otherwise or when it fails.
 */
import { createExecutionContext, env, waitOnExecutionContext } from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";

async function call(path: string, catalogUrl?: string) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(new Request(`https://x${path}`), { ...env, CATALOG_URL: catalogUrl }, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

describe("historical tier proxy", () => {
  afterEach(() => vi.restoreAllMocks());

  it("answers 501 without calling out when CATALOG_URL is unset", async () => {
    const spy = vi.spyOn(globalThis, "fetch");
    const res = await call("/api/metrics/processes/failures");
    expect(res.status).toBe(501);
    expect(spy).not.toHaveBeenCalled();
  });

  it("forwards path and query string to the catalog and relays its answer", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () => Response.json({ rows: [], window_days: 7 }));
    for (const path of [
      "/api/metrics/processes/failures?window_days=7&min_samples=1",
      "/metrics/processes/failures?window_days=7&min_samples=1",
    ]) {
      const res = await call(path, "https://catalog.test/api/");
      expect(res.status).toBe(200);
      expect(await res.json()).toEqual({ rows: [], window_days: 7 });
    }
    expect(spy.mock.calls.map((c) => String(c[0]))).toEqual([
      "https://catalog.test/api/metrics/processes/failures?window_days=7&min_samples=1",
      "https://catalog.test/api/metrics/processes/failures?window_days=7&min_samples=1",
    ]);
  });

  it("relays the catalog's 4xx validation errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ detail: "bad" }, { status: 400 }));
    const res = await call("/api/cohorts/ZellerG_2014/failures?process=kneaddata", "https://catalog.test/api");
    expect(res.status).toBe(400);
  });

  it.each([
    ["404", () => Promise.resolve(new Response("Not Found", { status: 404 }))],
    ["502", () => Promise.resolve(new Response("bad gateway", { status: 502 }))],
    ["unreachable", () => Promise.reject(new TypeError("connect failed"))],
  ])("falls back to 501 when the catalog answers %s", async (_name, impl) => {
    vi.spyOn(globalThis, "fetch").mockImplementation(impl);
    const res = await call("/api/metrics/processes/summary", "https://catalog.test/api");
    expect(res.status).toBe(501);
  });
});
