"""Integration tests for curated sample annotation endpoints.

All tests use the shared ``integration_client`` fixture (real Postgres via
testcontainers). The schema is created once per session via the ``create_schema``
autouse fixture in conftest.py, which calls ``metadata.create_all`` — this
picks up the two new tables added to db.py.
"""
from __future__ import annotations

import io
import uuid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_study_name() -> str:
    return f"TestStudy_{uuid.uuid4().hex[:8]}"


def _make_tsv(rows: list[dict], extra_cols: list[str] | None = None) -> bytes:
    """Build a minimal TSV with ncbi_accession + optional extra columns."""
    cols = ["ncbi_accession"] + (extra_cols or ["subject_id", "disease"])
    lines = ["\t".join(cols)]
    for row in rows:
        lines.append("\t".join(row.get(c, "") for c in cols))
    return "\n".join(lines).encode("utf-8")


def _import(client, study_name: str, tsv: bytes, **kwargs):
    """POST /curated/import with multipart form data."""
    return client.post(
        "/api/curated/import",
        data={"study_name": study_name, **kwargs},
        files={"file": ("test.tsv", io.BytesIO(tsv), "text/tab-separated-values")},
    )


# ---------------------------------------------------------------------------
# POST /curated/import — happy path
# ---------------------------------------------------------------------------

def test_import_happy_path(integration_client):
    client, _ = integration_client
    study_name = _make_study_name()

    tsv = _make_tsv([
        {"ncbi_accession": "SRR000001;SRR000002", "subject_id": "S1", "disease": "healthy"},
        {"ncbi_accession": "SRR000003",            "subject_id": "S2", "disease": "IBD"},
    ])
    resp = _import(client, study_name, tsv, pubmed_id="12345678")
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert data["rows_loaded"] == 2
    assert data["rows_dropped"] == 0
    assert data["dropped_rows"] == []


def test_import_creates_study(integration_client):
    client, _ = integration_client
    study_name = _make_study_name()

    tsv = _make_tsv([{"ncbi_accession": "SRR111111", "subject_id": "A"}])
    _import(client, study_name, tsv, doi="10.1234/test")

    resp = client.get(f"/api/curated/studies/{study_name}")
    assert resp.status_code == 200
    study = resp.json()
    assert study["study_name"] == study_name
    assert study["metadata"]["doi"] == "10.1234/test"


# ---------------------------------------------------------------------------
# Idempotent re-import
# ---------------------------------------------------------------------------

def test_import_idempotent(integration_client):
    client, _ = integration_client
    study_name = _make_study_name()

    tsv = _make_tsv([{"ncbi_accession": "SRR222222", "subject_id": "B", "disease": "healthy"}])
    resp1 = _import(client, study_name, tsv)
    resp2 = _import(client, study_name, tsv)

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Both calls report 1 row loaded (upsert)
    assert resp1.json()["rows_loaded"] == 1
    assert resp2.json()["rows_loaded"] == 1

    # Only one annotation row should exist
    resp = client.get(f"/api/curated/studies/{study_name}/samples")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Null / empty ncbi_accession rows are dropped
# ---------------------------------------------------------------------------

def test_import_drops_null_accession_rows(integration_client):
    client, _ = integration_client
    study_name = _make_study_name()

    tsv = _make_tsv([
        {"ncbi_accession": "SRR333333", "subject_id": "C", "disease": "healthy"},
        {"ncbi_accession": "",           "subject_id": "D", "disease": "IBD"},
        {"ncbi_accession": "SRR333334", "subject_id": "E", "disease": "healthy"},
    ])
    resp = _import(client, study_name, tsv)
    assert resp.status_code == 200

    data = resp.json()
    assert data["rows_loaded"] == 2
    assert data["rows_dropped"] == 1
    assert len(data["dropped_rows"]) == 1
    dropped = data["dropped_rows"][0]
    assert dropped["row_index"] == 1
    assert dropped["subject_id"] == "D"


# ---------------------------------------------------------------------------
# Case-insensitive ncbi_accession column detection
# ---------------------------------------------------------------------------

def test_import_case_insensitive_column(integration_client):
    """TSV with NCBI_ACCESSION (uppercase) should be accepted."""
    client, _ = integration_client
    study_name = _make_study_name()

    # Build a TSV with the column named in uppercase
    tsv = "NCBI_ACCESSION\tsubject_id\nSRR444444\tX\n".encode("utf-8")
    resp = _import(client, study_name, tsv)
    assert resp.status_code == 200
    assert resp.json()["rows_loaded"] == 1


def test_import_missing_accession_column_returns_422(integration_client):
    """TSV without any ncbi_accession-like column should return 422."""
    client, _ = integration_client
    study_name = _make_study_name()

    tsv = "subject_id\tdisease\nS1\thealthy\n".encode("utf-8")
    resp = _import(client, study_name, tsv)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /curated/studies
# ---------------------------------------------------------------------------

def test_list_studies(integration_client):
    client, _ = integration_client
    study_name = _make_study_name()

    tsv = _make_tsv([{"ncbi_accession": "SRR555555", "subject_id": "F"}])
    _import(client, study_name, tsv)

    resp = client.get("/api/curated/studies")
    assert resp.status_code == 200
    names = [s["study_name"] for s in resp.json()]
    assert study_name in names


# ---------------------------------------------------------------------------
# GET /curated/studies/{study_name}
# ---------------------------------------------------------------------------

def test_get_study_not_found(integration_client):
    client, _ = integration_client
    resp = client.get("/api/curated/studies/DOES_NOT_EXIST_STUDY_XYZ")
    assert resp.status_code == 404


def test_get_study_found(integration_client):
    client, _ = integration_client
    study_name = _make_study_name()

    tsv = _make_tsv([{"ncbi_accession": "SRR666666", "subject_id": "G"}])
    _import(client, study_name, tsv, pubmed_id="99999999")

    resp = client.get(f"/api/curated/studies/{study_name}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["study_name"] == study_name
    assert data["metadata"]["pubmed_id"] == "99999999"


# ---------------------------------------------------------------------------
# GET /curated/studies/{study_name}/samples
# ---------------------------------------------------------------------------

def test_list_study_samples(integration_client):
    client, _ = integration_client
    study_name = _make_study_name()

    tsv = _make_tsv([
        {"ncbi_accession": "SRR777771", "subject_id": "H1"},
        {"ncbi_accession": "SRR777772", "subject_id": "H2"},
        {"ncbi_accession": "SRR777773", "subject_id": "H3"},
    ])
    _import(client, study_name, tsv)

    resp = client.get(f"/api/curated/studies/{study_name}/samples")
    assert resp.status_code == 200
    annotations = resp.json()
    assert len(annotations) == 3
    study_names = {a["study_name"] for a in annotations}
    assert study_names == {study_name}


def test_list_study_samples_pagination(integration_client):
    client, _ = integration_client
    study_name = _make_study_name()

    tsv = _make_tsv([
        {"ncbi_accession": f"SRR88888{i}", "subject_id": f"P{i}"}
        for i in range(5)
    ])
    _import(client, study_name, tsv)

    resp = client.get(f"/api/curated/studies/{study_name}/samples?limit=2&offset=0")
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp2 = client.get(f"/api/curated/studies/{study_name}/samples?limit=2&offset=2")
    assert resp2.status_code == 200
    assert len(resp2.json()) == 2


# ---------------------------------------------------------------------------
# GET /curated/samples/{sample_id}
# ---------------------------------------------------------------------------

def test_get_sample_annotations(integration_client):
    """sample_id lookup should return annotations from all studies."""
    from nextflow_telemetry.utils import parse_srrs, srrs_to_sample_id

    client, _ = integration_client
    srr = f"SRR9{uuid.uuid4().hex[:7]}"
    expected_sample_id = srrs_to_sample_id(parse_srrs(srr))

    study_name_a = _make_study_name()
    study_name_b = _make_study_name()

    tsv_a = f"ncbi_accession\tsubject_id\n{srr}\tX\n".encode("utf-8")
    tsv_b = f"ncbi_accession\tsubject_id\n{srr}\tY\n".encode("utf-8")

    _import(client, study_name_a, tsv_a)
    _import(client, study_name_b, tsv_b)

    resp = client.get(f"/api/curated/samples/{expected_sample_id}")
    assert resp.status_code == 200
    annotations = resp.json()
    # Should find the annotation in both studies
    assert len(annotations) == 2
    returned_studies = {a["study_name"] for a in annotations}
    assert returned_studies == {study_name_a, study_name_b}


def test_get_sample_annotations_empty(integration_client):
    """Unknown sample_id should return empty list (not 404)."""
    client, _ = integration_client
    resp = client.get("/api/curated/samples/0000000000000000000000000000000000000000")
    assert resp.status_code == 200
    assert resp.json() == []
