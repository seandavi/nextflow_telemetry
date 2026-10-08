/**
 * Readset ids (ADR-0007): `RS.` + the refget Sequence Collections v1.0.0
 * digest of `{ units: [...] }`, where units is the deduplicated, code-point
 * sorted set of `insdc.sra:<run>` CURIEs. Pinned by the ADR's golden vectors
 * in test/readset.test.ts; nf_client/readset.py is the Python twin.
 *
 * Async because Web Crypto is; callers compute before they touch SQL, so the
 * ControlDO write that follows stays a single synchronous block.
 */

const UNIT = /^insdc\.sra:[SED]RR\d+$/;
const RUN = /^[SED]RR\d+$/;

/** seqcol footnote F3: base64url of the first 24 bytes of SHA-512. */
export async function sha512t24u(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-512", new TextEncoder().encode(text));
  return btoa(String.fromCharCode(...new Uint8Array(digest, 0, 24)))
    .replaceAll("+", "-")
    .replaceAll("/", "_");
}

/**
 * RFC 8785 canonical JSON. JCS serializes strings and numbers exactly as
 * JSON.stringify does, so the only extra rules are no whitespace and object
 * keys sorted by UTF-16 code unit (the default `sort()` order).
 */
export function canonicalJson(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonicalJson).join(",")}]`;
  if (v !== null && typeof v === "object") {
    const keys = Object.keys(v).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson((v as Record<string, unknown>)[k])}`).join(",")}}`;
  }
  return JSON.stringify(v);
}

/** The readset id of a set of units. Throws on an empty set or a unit outside the schema. */
export async function readsetId(units: string[]): Promise<string> {
  const u = [...new Set(units)].sort();
  if (!u.length) throw new Error("readset has no units");
  for (const x of u) if (!UNIT.test(x)) throw new Error(`invalid readset unit: ${JSON.stringify(x)}`);
  return "RS." + (await sha512t24u(canonicalJson({ units: await sha512t24u(canonicalJson(u)) })));
}

/**
 * Readset id for a sample's canonical `;`-joined run list, or null when any
 * entry is not an INSDC run accession: such a sample has no readset and gets
 * no jobs under a readset-keyed registration.
 */
export async function readsetIdForRuns(ncbiAccession: string): Promise<string | null> {
  const runs = ncbiAccession.split(";").filter(Boolean);
  if (!runs.length || !runs.every((r) => RUN.test(r))) return null;
  return readsetId(runs.map((r) => `insdc.sra:${r}`));
}
