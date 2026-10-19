"""The pinned cMD dictionary, its generated LinkML schema, and the fields we emit stay in step (#340)."""
from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from nextflow_telemetry.meta import zoe

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("cmd_schema", ROOT / "scripts" / "cmd_schema.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

# Fields we emit on purpose that cMD does not define. Each needs a reason.
LOCAL_FIELDS = {"health_condition": "coarse case/control derived from cMD disease (ADR-0012)"}


def test_pinned_csv_matches_pin():
    mod.check_pin()


def test_regenerating_is_a_no_op():
    assert mod.render() == mod.YAML_PATH.read_text(encoding="utf-8"), \
        "cmd.yaml is stale: run `uv run python scripts/cmd_schema.py`"


def test_schema_shape():
    import yaml
    schema = yaml.safe_load(mod.YAML_PATH.read_text())
    enums = schema["enums"]
    dynamic = {k for k, v in enums.items() if "reachable_from" in v or "include" in v}
    assert len(dynamic) == 11
    assert enums["SexEnum"]["permissible_values"]["Male"] == {"meaning": "NCIT:C20197"}
    assert schema["slots"]["sample_id"]["identifier"] is True
    assert schema["slots"]["disease"]["multivalued"] is True


def test_label_and_id_counts_must_agree():
    row = {"col.name": "x", "corpus.type": "static_enum", "allowedvalues": "A|B",
           "static.enum": "NCIT:C1", "dynamic.enum": "NA", "dynamic.enum.property": "NA"}
    with pytest.raises(ValueError, match="2 labels vs 1"):
        mod._enum(row)


def test_unknown_prefix_is_an_error():
    with pytest.raises(ValueError, match="unknown CURIE prefix"):
        mod._curies("EFO:0000001")


def test_linkml_lint_passes():
    p = subprocess.run(["linkml", "lint", "-c", str(mod.SCHEMA_DIR / "linkml-lint.yaml"), str(mod.YAML_PATH)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.mark.parametrize("gen", ["gen-pydantic", "gen-json-schema"])
def test_generators_succeed(gen):
    p = subprocess.run([gen, str(mod.YAML_PATH)], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout, p.stderr


def test_emitted_fields_exist_in_schema():
    with mod.CSV_PATH.open(newline="") as f:
        names = {r["col.name"] for r in csv.DictReader(f)}
    with (ROOT / "src" / "nextflow_telemetry" / "meta" / "field_map.csv").open(newline="") as f:
        emitted = {r["field"] for r in csv.DictReader(f)} | set(zoe.UNITS)
    assert emitted - names == set(LOCAL_FIELDS)


def test_pin_records_catalog_snapshot():
    pin = json.loads((mod.SCHEMA_DIR / "pin.json").read_text())
    assert pin["catalog_snapshot"]["source"] == "cmd-curation"
    assert pin["catalog_snapshot"]["version"] == pin["commit"]
