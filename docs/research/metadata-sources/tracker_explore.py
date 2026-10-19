"""Aggregate profile of the retained study-tracker export (restricted source: prints
counts only, never rows or URLs). Usage: python -I tracker_explore.py tracker.csv"""
import csv, re, sys
from collections import Counter

rows = list(csv.DictReader(open(sys.argv[1], newline="")))
print(len(rows), "rows; columns:", len(rows[0]))
print("blank study cells:", sum(not r["study"].strip() for r in rows))
print("summary rows:", sum(bool(re.search(r"total|overall", r["study"], re.I)) for r in rows))
print("study names used on >1 row:", sum(n > 1 for k, n in Counter(r["study"] for r in rows).items() if k))
for col in ("Host species", "Bodysite", "HasInfants", "Sharing data agreement"):
    print(col, dict(Counter(r[col] or "∅" for r in rows).most_common(6)))
pat = re.compile(r"^(PRJ[EDN][A-Z]?\d+|[SED]RP\d+)$")
def kind(b):
    if b in ("", "NA"): return "empty/NA"
    return "INSDC" if all(pat.match(x) for x in re.split(r"[,/ ]+", b) if x) else "other (EGA, annotated, prose)"
print("accession field:", dict(Counter(kind(r["Bioproject"]) for r in rows)))
print("sum of numeric Samples incl. summary rows:", sum(int(r["Samples"]) for r in rows if r["Samples"].isdigit()))
