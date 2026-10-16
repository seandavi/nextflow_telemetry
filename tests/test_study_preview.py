"""Test the add-study preview comment formatter (scripts/format_study_preview.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "scripts" / "format_study_preview.py"
_spec = importlib.util.spec_from_file_location("format_study_preview", _path)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_render_includes_composition_and_warning():
    receipt = {
        "samples_found": 4, "samples_added": 3, "samples_existing": 1,
        "library_composition": {
            "library_strategy": {"WGS": 3, "AMPLICON": 1},
            "library_selection": {"RANDOM": 3, "PCR": 1},
            "library_source": {"METAGENOMIC": 4},
            "instrument_platform": {"ILLUMINA": 4},
        },
        "warnings": ["1 of 4 run(s) look like amplicon/16S (…)"],
    }
    out = mod.render("PRJEB1", receipt)
    assert "PRJEB1" in out
    assert "found **4** samples" in out
    assert "WGS (3), AMPLICON (1)" in out
    assert "⚠️" in out and "amplicon" in out.lower()
    assert "approved" in out


def test_render_no_composition_no_warnings():
    out = mod.render("PRJEB2", {"samples_found": 2, "samples_added": 2, "samples_existing": 0})
    assert "found **2** samples" in out
    assert "⚠️" not in out
    assert "Library composition" not in out


def test_render_estimate_and_queued_backlog():
    receipt = {
        "samples_found": 2, "samples_added": 2, "samples_existing": 0,
        "estimate": {"samples": 2, "gbp": 5.1, "median_gbp": 2.55, "core_h": 48,
                     "anvil_su": 35, "peak_scratch_gb_per_sample": 50},
    }
    out = mod.render("PRJEB3", receipt, queued=10)
    assert "**48 core-h**" in out and "**35 Anvil SU**" in out and "5.1 Gbp" in out
    assert "10 unfinished jobs ≈ 240 core-h" in out
    assert "Already queued" not in mod.render("PRJEB3", receipt)
