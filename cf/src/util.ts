/** Gzip a string for R2. Everything this service archives is text, and R2 charges by stored byte. */
export async function gzip(body: string): Promise<ArrayBuffer> {
  const stream = new Blob([body]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Response(stream).arrayBuffer();
}

/** One NDJSON line per row. The archive format DuckDB reads without ceremony. */
export function ndjson(rows: unknown[]): string {
  return rows.map((r) => JSON.stringify(r)).join("\n") + "\n";
}

/**
 * UUIDv7 — time-ordered, so run names sort by claim time in any listing.
 * v1 minted these with Python's uuid7; keeping the format means run_name
 * looks identical on the wire and in logs.
 */
export function uuidv7(): string {
  const ts = Date.now();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[0] = (ts / 2 ** 40) & 0xff;
  b[1] = (ts / 2 ** 32) & 0xff;
  b[2] = (ts / 2 ** 24) & 0xff;
  b[3] = (ts / 2 ** 16) & 0xff;
  b[4] = (ts / 2 ** 8) & 0xff;
  b[5] = ts & 0xff;
  b[6] = 0x70 | (b[6] & 0x0f); // version 7
  b[8] = 0x80 | (b[8] & 0x3f); // variant
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
