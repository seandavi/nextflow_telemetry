#!/usr/bin/env python3
"""Draw the README's figures as SVG. Stdlib only.

    python3 docs/figures/make_figures.py   # writes <name>-light.svg and <name>-dark.svg

Helpers and colour roles follow bioc-infrastructure's diagrams/unified-search/make_figures.py.
Each figure is drawn for a light and a dark theme with a transparent background; the
README picks one with <picture> and prefers-color-scheme.

Colour roles: amber = external (clusters, upstream sources), green = our command-line
tools, blue = services and published outputs, violet = data stores.
"""
from pathlib import Path
from html import escape

OUT = Path(__file__).parent

PALETTES = {
    "light": dict(BG="#ffffff", TEXT="#14202e", MUTED="#4a5a6c", BLUE="#1f7fc4",
                  GREEN="#4f8f17", AMBER="#b87a00", CORAL="#cc4733", VIOLET="#6e4fd0"),
    "dark": dict(BG="#020617", TEXT="#e8eef6", MUTED="#a9b8ca", BLUE="#4aa8e8",
                 GREEN="#9fd05a", AMBER="#f2b441", CORAL="#ff7a66", VIOLET="#b49cff"),
}
SANS = "Inter, 'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace"
P = {}  # the active palette, set by use()


def use(name):
    P.clear()
    P.update(PALETTES[name])


def svg(w, h, body, title):
    marks = "".join(
        f'<marker id="ah-{r}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{P[r]}"/></marker>'
        for r in ("MUTED", "BLUE", "GREEN", "AMBER", "CORAL", "VIOLET")
    )
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-label="{escape(title)}">\n<title>{escape(title)}</title>\n'
            f"<defs>{marks}</defs>\n" + "".join(body) + "</svg>\n")


def text(x, y, s, size=18, role="TEXT", weight=400, anchor="start", mono=False):
    fam = MONO if mono else SANS
    return (f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" fill="{P[role]}" '
            f'font-weight="{weight}" text-anchor="{anchor}" xml:space="preserve">{escape(s)}</text>\n')


def box(x, y, w, h, title, lines=(), role="BLUE", title_size=20, line_size=15, anchor="start"):
    c = P[role]
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{c}1a" stroke="{c}" stroke-width="2"/>\n']
    tx = x + w / 2 if anchor == "middle" else x + 16
    out.append(text(tx, y + 30, title, title_size, "TEXT", 650, anchor))
    for i, ln in enumerate(lines):
        out.append(text(tx, y + 38 + (i + 1) * (line_size + 7), ln, line_size, "MUTED", 400, anchor, mono=True))
    return "".join(out)


def group(x, y, w, h, label, role):
    c = P[role]
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="none" stroke="{c}" '
            f'stroke-width="1.5" stroke-dasharray="4 6"/>\n'
            + text(x + 16, y - 10, label.upper(), 14, role, 700, mono=True))


def arrow(pts, role="MUTED", label=None, lpos=None, width=2.2, lsize=13):
    c = P[role]
    d = "M" + " L".join(f"{a},{b}" for a, b in pts)
    out = [f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{width}" marker-end="url(#ah-{role})"/>\n']
    if label:
        lx, ly = lpos if lpos else ((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2)
        lines = label.split("\n")
        wmax = max(len(s) for s in lines) * lsize * 0.62 + 16
        hh = len(lines) * (lsize + 5) + 8
        out.append(f'<rect x="{lx - wmax / 2}" y="{ly - hh / 2}" width="{wmax}" height="{hh}" rx="6" '
                   f'fill="{P["BG"]}" stroke="{c}" stroke-width="1"/>\n')
        for i, s in enumerate(lines):
            out.append(text(lx, ly - hh / 2 + (i + 1) * (lsize + 5), s, lsize, role, 500, "middle", mono=True))
    return "".join(out)


def pill(x, y, s, role, size=14):
    c = P[role]
    w = len(s) * size * 0.62 + 20
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{size + 12}" rx="{(size + 12) / 2}" '
            f'fill="{c}22" stroke="{c}"/>\n' + text(x + w / 2, y + size + 3, s, size, role, 600, "middle", mono=True))



LEGEND = (("external", "AMBER"), ("our CLIs", "GREEN"), ("services / published", "BLUE"), ("stores", "VIOLET"))


def legend(y):
    out, x = [text(30, y + 19, "Legend:", 14, "MUTED", 600, mono=True)], 110
    for label, role in LEGEND:
        out.append(pill(x, y, label, role))
        x += len(label) * 14 * 0.62 + 40
    return "".join(out)


def architecture():
    b = []
    b.append(group(20, 60, 380, 400, "HPC: Alpine, Anvil, Bridges-2", "AMBER"))
    b.append(box(40, 90, 340, 120, "nf-client daemon", ["login node, outbound only", "claims batches, sbatch"], "GREEN"))
    b.append(box(40, 300, 340, 130, "SLURM wrapper job", ["run-wrapper -> nextflow", "per-sample SLURM tasks"], "GREEN"))
    b.append(arrow([(210, 210), (210, 295)], "GREEN", "sbatch", (210, 252)))
    b.append(box(520, 90, 380, 150, "v2 control plane (cf/)", ["Worker + Durable Objects", "dispatch, runs, timers", "event sink, task logs"], "BLUE"))
    b.append(box(520, 330, 380, 130, "R2", ["cmgd-raw: pipeline outputs", "events, ledger, logs"], "VIOLET"))
    b.append(arrow([(380, 150), (515, 150)], "GREEN", "claim / report", (448, 125)))
    b.append(arrow([(380, 330), (515, 220)], "GREEN", "weblog,\nrun events", (448, 262)))
    b.append(arrow([(380, 410), (515, 410)], "GREEN", "publishDir", (448, 410)))
    b.append(arrow([(710, 240), (710, 325)], "BLUE"))
    b.append(box(1010, 90, 390, 150, "Catalog service", ["FastAPI + Postgres", "studies, samples, submissions", "curation, source catalog"], "BLUE"))
    b.append(box(1010, 330, 390, 130, "React dashboard", ["cmgd.cancerdatasci.org", "progress, failures, runs"], "BLUE"))
    b.append(arrow([(1005, 130), (905, 130)], "BLUE", "write-through", (955, 72)))
    b.append(arrow([(905, 210), (1005, 360)], "BLUE", "live state", (955, 285)))
    b.append(arrow([(1205, 240), (1205, 325)], "BLUE", "studies", (1205, 282)))
    b.append(box(520, 540, 380, 110, "nf-etl, nf-meta", ["onclappc02"], "GREEN"))
    b.append(box(1010, 540, 390, 110, "Shared DuckLake", ["lake.cmgd, lake.cmgd_meta", "releases: Figure 2"], "VIOLET"))
    b.append(arrow([(710, 460), (710, 535)], "VIOLET", "outputs", (710, 497)))
    b.append(arrow([(900, 595), (1005, 595)], "GREEN"))
    b.append(legend(680))
    return svg(1420, 720, b, "System architecture. nf-client daemons on the HPC login nodes claim work from the v2 "
               "Cloudflare control plane and submit SLURM wrapper jobs that run Nextflow; telemetry flows back to "
               "the Worker and outputs land in R2. The Postgres catalog service writes registrations through to v2. "
               "The dashboard reads both. nf-etl and nf-meta load outputs and metadata into the shared DuckLake.")


def data_flow():
    b = []
    b.append(group(20, 60, 340, 610, "Sources", "AMBER"))
    srcs = [("Pipeline outputs", ["r2:cmgd-raw/<wf>/<ver>/", "one prefix per sample"], "VIOLET"),
            ("cMD curation", ["waldronlab GitHub repo"], "AMBER"),
            ("Olm Microbiome Repo", ["Zenodo data packages"], "AMBER"),
            ("ZOE PREDICT, ENA", ["Zenodo; ENA filereport"], "AMBER"),
            ("ASAP-MAC, tracker", ["cataloged, not loaded"], "AMBER")]
    for i, (t, ls, r) in enumerate(srcs):
        b.append(box(40, 80 + i * 118, 300, 100, t, ls, r))
    b.append(box(440, 80, 320, 110, "nf-etl ingest", ["completed jobs from v2", "500-sample batches"], "GREEN"))
    b.append(box(440, 300, 320, 120, "Source catalog", ["Postgres descriptors", "snapshots: cmgd-private"], "VIOLET"))
    b.append(box(440, 510, 320, 120, "nf-meta load / link", ["values per source release", "sample IDs -> ENA"], "GREEN"))
    b.append(arrow([(340, 130), (435, 135)], "VIOLET"))
    b.append(arrow([(362, 360), (435, 360)], "AMBER", "capture\nall", (398, 330)))
    b.append(arrow([(362, 570), (435, 570)], "AMBER", "load\ncMD, Olm", (398, 538)))
    b.append(arrow([(600, 420), (600, 505)], "VIOLET", "snapshots", (600, 462)))
    b.append(box(840, 260, 300, 200, "Shared DuckLake", ["lake.cmgd.*", "  results", "lake.cmgd_meta.*", "  sample metadata"], "VIOLET"))
    b.append(arrow([(760, 135), (835, 300)], "GREEN"))
    b.append(arrow([(760, 570), (835, 420)], "GREEN"))
    b.append(group(1200, 60, 360, 610, "Publication routes", "BLUE"))
    pubs = [("Public releases", ["nf-etl publish --sync", "cmgd-public.cancerdatasci.org"]),
            ("Mirrors", ["nf-etl mirror --to zenodo|hf"]),
            ("Deliverables", ["nf-etl deliver, 90-day expiry", "outputs read from cmgd-raw"]),
            ("Gene-family files", ["straight from cmgd-raw", "cmgd-raw.cancerdatasci.org"])]
    ys = (80, 228, 356, 504)
    for (t, ls), y in zip(pubs, ys):
        b.append(box(1220, y, 320, 110 if len(ls) == 2 else 90, t, ls, "BLUE"))
    b.append(arrow([(1140, 320), (1215, 140)], "BLUE", "publish", (1170, 220)))
    b.append(arrow([(1380, 190), (1380, 223)], "BLUE"))
    b.append(arrow([(1140, 400), (1215, 410)], "BLUE", "metadata", (1175, 435)))
    b.append(legend(700))
    return svg(1580, 740, b, "Data flow and publication routes. nf-etl ingests finished pipeline outputs from "
               "cmgd-raw; metadata sources are captured into the source catalog and loaded by nf-meta. Both write "
               "to the shared DuckLake, which feeds public releases (mirrored to Zenodo or Hugging Face) and scoped "
               "deliverables (which read outputs from cmgd-raw and metadata from the lake). Gene-family files are served straight from cmgd-raw.")


FIGS = {"architecture": architecture, "data-flow": data_flow}

if __name__ == "__main__":
    for theme in PALETTES:
        use(theme)
        for name, fn in FIGS.items():
            s = fn()
            assert s.count("<svg") == 1 and "<title>" in s, name
            (OUT / f"{name}-{theme}.svg").write_text(s)
            print("wrote", f"{name}-{theme}.svg")
