"""Build add-samples TSVs for the 2026-10-30 registration batch.

Reads only local copies of catalog snapshots / ENA listings; writes tsv/<collection>.tsv
and conflicts.json. Run with python3 -I from $TMPDIR.
"""
import csv, glob, json, os, re
from collections import defaultdict

csv.field_size_limit(10**9)
RUN = re.compile(r"[SED]RR\d+")
T = os.environ["TMPDIR"]
v2 = json.load(open(f"{T}/v2_samples.json"))
run_to_sample = {}
for s in v2:
    for r in (s["ncbi_accession"] or "").split(";"):
        if r:
            run_to_sample[r] = s["sample_id"]

def runs_of(acc):
    toks = {x.strip() for x in acc.replace(",", ";").split(";")}
    return sorted(x for x in toks if RUN.fullmatch(x))  # ponytail: drops placeholders like "Not applicable"

batches = {}       # collection -> list of (ncbi_accession, biosample)
conflicts = []

# group 1: cMD curated studies (catalog snapshot cmd-curation @ 6828fb9)
for d in sorted(glob.glob(f"{T}/snaps/cmd/*/inst/curated/*/")):
    st = os.path.basename(d.rstrip("/"))
    rows = []
    for f in glob.glob(d + "*.tsv"):
        for r in csv.DictReader(open(f, errors="replace"), delimiter="\t"):
            runs = runs_of(r.get("ncbi_accession") or "")
            if runs:
                rows.append((";".join(runs), ""))
    rows = sorted(set(rows))
    if not rows or all(all(x in run_to_sample for x in a.split(";")) for a, _ in rows):
        continue  # no accessions, or fully registered
    batches[st] = rows

cmd_runs = {x for rows in batches.values() for a, _ in rows for x in a.split(";")}

# group 3 (v1-only, not in cMD) + ZOE P2/P3: ENA listings, WGS runs grouped per BioSample
ena = {
    "PRJNA1099939": f"{T}/ena2/PRJNA1099939.json",
    "PRJEB53401": f"{T}/ena2/PRJEB53401.json",
    "PRJEB75460": f"{T}/zoe/p2.json",
    "PRJEB75462": f"{T}/zoe/p3-us21.json",
    "PRJEB75463": f"{T}/zoe/p3-us22a.json",
    "PRJEB75464": f"{T}/zoe/p3-uk22a.json",
    "PRJEB75465": f"{T}/zoe/p3-uk23art.json",
}
for coll, path in ena.items():
    by_bs = defaultdict(set)
    for r in json.load(open(path)):
        if r["library_strategy"] == "WGS":
            by_bs[r["sample_accession"]].add(r["run_accession"])
    overlap = {x for rs in by_bs.values() for x in rs} & cmd_runs
    if overlap:
        conflicts.append({"collection": coll, "runs_in_cmd": len(overlap)})
        continue
    batches[coll] = sorted((";".join(sorted(rs)), bs) for bs, rs in by_bs.items())

# a run already registered under a different run grouping would make a second sample
seen = {}
for coll, rows in batches.items():
    for a, _ in rows:
        runs = a.split(";")
        prior = {run_to_sample[x] for x in runs if x in run_to_sample}
        if prior and len(prior) == 1 and all(x in run_to_sample for x in runs) and \
                sum(1 for v in run_to_sample.values() if v in prior) == len(runs):
            continue  # identical sample already registered: membership only
        if prior:
            conflicts.append({"collection": coll, "ncbi_accession": a, "reason": "runs registered under another grouping"})
        for x in runs:
            if x in seen and seen[x] != (coll, a):
                conflicts.append({"collection": coll, "ncbi_accession": a, "reason": f"run {x} also in {seen[x][0]}"})
            seen[x] = (coll, a)

os.makedirs(f"{T}/tsv", exist_ok=True)
for coll, rows in batches.items():
    with open(f"{T}/tsv/{coll}.tsv", "w") as f:
        f.write("ncbi_accession\tstudy_name\tbiosample\n")
        for a, bs in rows:
            f.write(f"{a}\t{coll}\t{bs}\n")
json.dump(conflicts, open(f"{T}/conflicts.json", "w"), indent=1)
print(len(batches), "collections,", sum(map(len, batches.values())), "rows,", len(conflicts), "conflicts")
