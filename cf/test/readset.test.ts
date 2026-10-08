/**
 * Readset ids (ADR-0007): the ADR's golden vectors and the seqcol spec
 * examples it cites. nf_client's tests/test_readset.py pins the same values.
 */
import { describe, expect, it } from "vitest";
import { canonicalJson, readsetId, readsetIdForRuns, sha512t24u } from "../src/readset";

const ZELLER = ["ERR478958", "ERR478959", "ERR478960", "ERR478961", "ERR480454", "ERR480455", "ERR480456", "ERR480457"].map(
  (r) => `insdc.sra:${r}`,
);

describe("readset ids (ADR-0007)", () => {
  it("reproduces the seqcol v1.0.0 spec example digests", async () => {
    expect(await sha512t24u(canonicalJson(["chr1", "chr2", "chr3"]))).toBe("g04lKdxiYtG3dOGeUC5AdKEifw65G0Wp");
    const level1 = { sequences: "rD29ZKmEqwwHRXjiQ36p6UMZQ5hemmsb", names: "g04lKdxiYtG3dOGeUC5AdKEifw65G0Wp" };
    expect(await sha512t24u(canonicalJson(level1))).toBe("sjNNwm4zov3Dl0FRWbRTcZwzqrTQKIqL");
  });

  it("reproduces every golden vector, units digest and readset id", async () => {
    const vectors: [string[], string, string][] = [
      [["insdc.sra:SRR000001"], "YT21j4gzsIn8wOjEyeLa3MLDtgDplqBj", "RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF"],
      [ZELLER, "dPkNQbShezkLh6trclk7YEYbYPskOOfP", "RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj"],
      [[5, 0, 7, 2, 1, 6, 3, 4, 0].map((i) => ZELLER[i]), "dPkNQbShezkLh6trclk7YEYbYPskOOfP", "RS.l29A5uBFtCKLgc-EPvUhj0hD6Q02Z7qj"],
      [["insdc.sra:SRR1", "insdc.sra:ERR2", "insdc.sra:DRR3"], "SiHrRpm6pYqQOnfGN-6yg_ZssJWLjjGP", "RS.uFRI93utvCk3llTO2fQPqARuz-e6Lj_-"],
    ];
    for (const [units, unitsDigest, id] of vectors) {
      expect(await sha512t24u(canonicalJson([...new Set(units)].sort()))).toBe(unitsDigest);
      expect(await readsetId(units)).toBe(id);
    }
  });

  it("rejects empty sets and units outside the schema", async () => {
    await expect(readsetId([])).rejects.toThrow("no units");
    await expect(readsetId(["SRR1"])).rejects.toThrow("invalid readset unit");
    await expect(readsetId(["insdc.sra:SRS1"])).rejects.toThrow("invalid readset unit");
  });

  it("derives a sample's id from its canonical run list, or null for a non-run entry", async () => {
    expect(await readsetIdForRuns("SRR000001")).toBe("RS.29BkNp8wxCWwuhVe3luQxYtv97BwdwjF");
    expect(await readsetIdForRuns("SRR1;ERR2;DRR3")).toBe("RS.uFRI93utvCk3llTO2fQPqARuz-e6Lj_-");
    expect(await readsetIdForRuns("n/a;SRR9")).toBeNull();
    expect(await readsetIdForRuns("")).toBeNull();
  });
});
