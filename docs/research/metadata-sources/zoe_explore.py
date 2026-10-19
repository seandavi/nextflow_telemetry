"""Exploration of the retained ZOE PREDICT open metadata (zenodo 15308000, v1).
Inputs: directory of extracted *.tsv (from the captured .bz2 snapshots). Prints findings."""
import csv, json, sys, urllib.request
from collections import Counter
from pathlib import Path
from statistics import median

d = Path(sys.argv[1])
ENA = {"P1": "PRJEB39223", "P2": "PRJEB75460", "P3_US21": "PRJEB75462", "P3_US22A": "PRJEB75463",
       "P3_UK22A": "PRJEB75464", "P3_UK23ART": "PRJEB75465"}

def ena_aliases(bp):
    u = ("https://www.ebi.ac.uk/ena/portal/api/filereport?accession=%s&result=read_run"
         "&fields=run_accession,sample_accession,secondary_sample_accession,sample_alias,sample_title&format=json" % bp)
    return json.load(urllib.request.urlopen(u, timeout=120))

all_ids = Counter()
for name, bp in ENA.items():
    rows = list(csv.DictReader((d / f"{name}.tsv").open(), delimiter="\t"))
    ids = [r["sampleID"] for r in rows]
    dup = [k for k, n in Counter(ids).items() if n > 1]
    all_ids.update(set(ids))
    ages = [float(r["age"]) for r in rows if r["age"] not in ("", "NA")]
    bmis = [float(r["bmi"]) for r in rows if r["bmi"] not in ("", "NA")]
    sexes = Counter(r["sex"] for r in rows)
    print(f"\n== {name} ({bp}): {len(rows)} rows, cols={list(rows[0])}")
    print(f"   duplicate sampleIDs: {len(dup)}; id examples: {ids[:2]}")
    print(f"   sex: {dict(sexes)}")
    print(f"   age n={len(ages)} missing={len(rows)-len(ages)} range={min(ages):.0f}-{max(ages):.0f} median={median(ages):.0f}")
    print(f"   bmi n={len(bmis)} missing={len(rows)-len(bmis)} range={min(bmis):.1f}-{max(bmis):.1f} (implausible <12 or >70: {sum(b<12 or b>70 for b in bmis)})")
    ena = ena_aliases(bp)
    for key in ("sample_alias", "sample_title"):
        vals = {r[key] for r in ena if r.get(key)}
        hit = len(set(ids) & vals)
        print(f"   ENA {key}: {len(vals)} distinct, overlap with sampleID: {hit}")
    best = max(("sample_alias", "sample_title"), key=lambda k: len(set(ids) & {r[k] for r in ena if r.get(k)}))
    by = {}
    for r in ena:
        by.setdefault(r[best], set()).add(r["secondary_sample_accession"])
    multi = sum(len(v) > 1 for v in by.values())
    runs_per = Counter(r[best] for r in ena)
    print(f"   joined on {best}: ZOE ids missing in ENA={len(set(ids)-set(by))}, ENA ids missing in ZOE={len(set(by)-set(ids))}, "
          f"ids spanning >1 SRS={multi}, ids with >1 run={sum(n>1 for n in runs_per.values())}")
print("\nsampleIDs that appear in more than one cohort file:", sum(n > 1 for n in all_ids.values()))
