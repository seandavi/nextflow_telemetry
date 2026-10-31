import { describe, expect, it } from "vitest";
import { serialized } from "../src/util";

// Stands in for DurableObjectState: the real one resets the object when the
// callback throws, so serialized must never let it throw (#401).
function fakeCtx() {
  const seen = { threwInside: false };
  return {
    seen,
    async blockConcurrencyWhile<U>(fn: () => Promise<U>): Promise<U> {
      try {
        return await fn();
      } catch (e) {
        seen.threwInside = true;
        throw e;
      }
    },
  };
}

describe("serialized", () => {
  it("returns the callback's value", async () => {
    const ctx = fakeCtx();
    await expect(serialized(ctx, async () => 42)).resolves.toBe(42);
  });

  it("rethrows outside the lock, so the object is not reset", async () => {
    const ctx = fakeCtx();
    const boom = new Error("R2 put failed");
    await expect(serialized(ctx, async () => { throw boom; })).rejects.toBe(boom);
    expect(ctx.seen.threwInside).toBe(false);
  });
});
