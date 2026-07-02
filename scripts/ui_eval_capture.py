#!/usr/bin/env python3
"""Capture the telemetry dashboard for UI/UX + observability evaluation.

Walks every nav page (state-based SPA, so we click nav items — there are no
per-page URLs), and for each page saves:
  - a full-page screenshot (real headless chromium; obscura has no paint engine)
  - the rendered innerText
  - a structured probe: tables/rows, pagination controls, loading/empty/error
    signals, heading outline, ARIA landmarks, and any console errors.

Output goes to an --out dir (default /tmp/ui_eval). Re-runnable as the UI evolves.

Usage:
    uv run --with playwright python scripts/ui_eval_capture.py \
        --url https://cmgd.cancerdatasci.org --out /tmp/ui_eval
"""
from __future__ import annotations
import argparse, json, os, sys
from playwright.sync_api import sync_playwright

NAV = ["Overview", "Process Metrics", "Workflows", "Samples",
       "Cohorts", "Dispatch", "Runs", "Infrastructure"]

PROBE = r"""
() => {
  const q = s => [...document.querySelectorAll(s)];
  const txt = document.body.innerText || "";
  const has = re => re.test(txt);
  return {
    tables: q("table").length,
    rows: q("tbody tr").length,
    buttons: q("button").length,
    links: q("a[href]").length,
    inputs: q("input,select").length,
    // scale signals
    pagination: q("*").some(e => /next|prev|page\s*\d|rows per page|load more|show more/i.test(e.textContent||"") && e.children.length===0),
    scrollHeight: document.documentElement.scrollHeight,
    // state signals
    loading: has(/loading|skeleton|please wait/i),
    empty: has(/no (data|results|samples|runs|rows)|nothing to show|empty/i),
    error: has(/error|failed to (load|fetch)|something went wrong|4\d\d|5\d\d/i),
    // structure / a11y
    headings: q("h1,h2,h3").map(h => h.tagName + ":" + (h.textContent||"").trim()).slice(0,25),
    landmarks: q("[role],nav,main,aside,header,footer").map(e=>e.getAttribute("role")||e.tagName.toLowerCase())
                 .reduce((a,r)=>{a[r]=(a[r]||0)+1;return a;},{}),
    ariaLabels: q("[aria-label]").length,
    imgNoAlt: q("img:not([alt])").length,
    // completeness-oriented content signals (persona: study monitor)
    mentionsPercent: (txt.match(/\d+(\.\d+)?%/g)||[]).slice(0,20),
    mentionsStudyCohort: /stud(y|ies)|cohort|collection|project/i.test(txt),
    mentionsETA: /eta|estimat|time remaining|projected|throughput|per hour|\/hr|rate/i.test(txt),
    textLen: txt.length,
    textHead: txt.replace(/\n{2,}/g,"\n").slice(0,1200),
  };
}
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://cmgd.cancerdatasci.org")
    ap.add_argument("--out", default="/tmp/ui_eval")
    ap.add_argument("--viewport", default="1440x900")
    args = ap.parse_args()
    w, h = (int(x) for x in args.viewport.split("x"))
    os.makedirs(args.out, exist_ok=True)

    manifest = {"url": args.url, "viewport": args.viewport, "pages": []}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": w, "height": h})
        console = []
        pg.on("console", lambda m: console.append(f"{m.type}: {m.text[:200]}") if m.type in ("error","warning") else None)
        pg.on("pageerror", lambda e: console.append(f"pageerror: {str(e)[:200]}"))
        pg.goto(args.url, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(4000)

        for label in NAV:
            slug = label.lower().split()[0]
            console.clear()
            try:
                if label != "Overview":
                    pg.get_by_text(label, exact=False).first.click(timeout=8000)
                    pg.wait_for_timeout(3500)
                    try: pg.wait_for_load_state("networkidle", timeout=8000)
                    except Exception: pass
                shot = os.path.join(args.out, f"{slug}.png")
                pg.screenshot(path=shot, full_page=True)
                probe = pg.evaluate(PROBE)
                probe["console"] = list(console)
                probe["label"] = label
                probe["screenshot"] = shot
                manifest["pages"].append(probe)
                print(f"[ok] {label:16} tables={probe['tables']} rows={probe['rows']} "
                      f"%={len(probe['mentionsPercent'])} eta={probe['mentionsETA']} "
                      f"empty={probe['empty']} err={probe['error']} console={len(probe['console'])}")
            except Exception as e:
                manifest["pages"].append({"label": label, "capture_error": str(e)[:300]})
                print(f"[ERR] {label}: {str(e)[:160]}")
        b.close()

    with open(os.path.join(args.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"\nWrote {args.out}/manifest.json and {len([p for p in manifest['pages'] if 'screenshot' in p])} screenshots.")

if __name__ == "__main__":
    sys.exit(main())
