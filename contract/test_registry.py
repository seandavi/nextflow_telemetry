import httpx
import pytest
from pydantic import TypeAdapter

from contract import schemas
from contract.support import (
    claim,
    eventually,
    job_summary,
    mark_complete,
    md5_sample_id,
    new_srr,
    reconcile,
    register_sample,
    register_workflow,
    submitted,
    validate,
    weblog,
)

pytestmark = pytest.mark.writes


def test_sample_upsert_is_idempotent(api: httpx.Client, ns: str) -> None:
    c, c2 = f"{ns}-upsert", f"{ns}-upsert2"
    runs = new_srr()
    first = register_sample(api, c, runs)
    again = register_sample(api, c, runs)
    assert (again["id"], again["sample_id"]) == (first["id"], first["sample_id"])

    both = register_sample(api, c2, runs)
    assert sorted(both["collections"]) == sorted([c, c2])

    page = validate(schemas.SampleList, api.get("samples", params={"collection": c}))
    assert page.total == 1
    facets = validate(schemas.CollectionFacets, api.get("samples/facets/collections"))
    assert {"collection": c, "count": 1} in [f.model_dump() for f in facets.collections]


def test_sample_lookups(api: httpx.Client, ns: str) -> None:
    c, biosample = f"{ns}-lookups", f"{ns}-SAMN1"
    runs = new_srr()
    sample = register_sample(api, c, runs, biosample_id=biosample)
    md5, readset_id = md5_sample_id(runs), sample["readset_id"]
    assert sample["sample_id"] == md5

    for path in (f"samples/{md5}", f"samples/{readset_id}", f"samples/by-srr/{runs}"):
        assert validate(schemas.Sample, api.get(path)).sample_id == md5, path
    by_biosample = validate(TypeAdapter(list[schemas.Sample]), api.get(f"samples/by-biosample/{biosample}"))
    assert [s.sample_id for s in by_biosample] == [md5]

    found = validate(schemas.SampleList, api.get("samples", params={"search": readset_id[3:12]}))
    assert md5 in [s.sample_id for s in found.items]
    validate(schemas.Detail, api.get("samples/by-srr/SRR000000000"), 404)


def test_sample_requires_ids(api: httpx.Client) -> None:
    validate(schemas.Detail, api.post("samples", json={"sample_id": "x"}), 422)


def test_workflow_registration_is_a_bundle(api: httpx.Client, ns: str) -> None:
    wf, c = f"{ns}-bundle", f"{ns}-bundle"
    params = {"humann_bundle": "x", "skip_humann": False}
    first = register_workflow(api, wf, params=params, collections=[c])
    assert first["params"] == params and first["collections"] == [c]

    reordered = register_workflow(api, wf, params={"skip_humann": False, "humann_bundle": "x"}, collections=[c])
    assert reordered["id"] == first["id"]

    unscoped_again = register_workflow(api, wf, params=params)
    assert unscoped_again["collections"] == [c]

    clash = api.post(
        "workflows",
        json={**_workflow_body(wf), "params": {**params, "skip_humann": True}},
    )
    validate(schemas.Detail, clash, 409)
    assert validate(schemas.Workflow, api.get(f"workflows/{first['id']}")).params == params

    for bad in ({"params": {"nested": {"a": 1}}}, {"params": ["x"]}, {"collections": "c"}, {"collections": []}):
        resp = api.post("workflows", json={**_workflow_body(wf), "version": "bad", **bad})
        validate(schemas.Detail, resp, 422)


def _workflow_body(workflow_id: str) -> dict[str, object]:
    return {
        "workflow_id": workflow_id,
        "version": "1.0.0",
        "repository_url": "https://example.invalid/contract",
        "revision": "main",
        "max_retries": 1,
    }


def test_workflow_status_and_revision(api: httpx.Client, ns: str) -> None:
    c = f"{ns}-status"
    pk = register_workflow(api, c, collections=[c])["id"]

    for status in ("paused", "active"):
        patched = validate(schemas.Workflow, api.patch(f"workflows/{pk}/status", json={"status": status}))
        assert patched.status == status
        if status == "paused":
            paused = validate(TypeAdapter(list[schemas.Workflow]), api.get("workflows", params={"status": "paused"}))
            assert pk in [w.id for w in paused]
    validate(schemas.Detail, api.patch(f"workflows/{pk}/status", json={"status": "bogus"}), 422)

    revised = validate(schemas.Workflow, api.patch(f"workflows/{pk}/revision", json={"revision": "abc123"}))
    assert revised.revision == "abc123"
    validate(schemas.Detail, api.patch("workflows/999999999/status", json={"status": "active"}), 404)


def test_scoped_reconcile(api: httpx.Client, ns: str) -> None:
    c1, c2 = f"{ns}-scope1", f"{ns}-scope2"
    pk = register_workflow(api, f"{ns}-scope", collections=[c1])["id"]
    register_sample(api, c1)
    register_sample(api, c2)

    assert reconcile(api) >= 1
    summary = job_summary(api, pk)
    assert (summary["total"], summary["pending"]) == (1, 1)

    reconcile(api)
    assert job_summary(api, pk) == summary


def test_readset_keyed_claim(api: httpx.Client, ns: str) -> None:
    c = f"{ns}-readset"
    wf = register_workflow(api, c, collections=[c])
    assert wf["sample_key"] == "readset_id"
    a, b = new_srr(), new_srr()
    first = register_sample(api, c, f"{a};{b}")
    second = register_sample(api, c, f"{b},{a}")
    assert first["sample_id"] != second["sample_id"]
    assert first["readset_id"] == second["readset_id"]

    reconcile(api)
    assert job_summary(api, wf["id"])["total"] == 1
    batch = claim(api, wf["workflow_id"])
    assert batch is not None
    key = batch["jobs"][0]["sample_id"]
    assert key == first["readset_id"]
    assert key.startswith("RS.")
    assert validate(schemas.Sample, api.get(f"samples/{first['sample_id']}")).readset_id == key


def test_jobs_keyset_paging(api: httpx.Client, edge: httpx.Client, ns: str, lag: float) -> None:
    c = f"{ns}-paging"
    wf = register_workflow(api, c, collections=[c])
    pk = wf["id"]
    samples = [register_sample(api, c) for _ in range(3)]
    reconcile(api)

    page = validate(schemas.JobList, api.get(f"workflows/{pk}/jobs", params={"limit": 2}))
    ids = [j.job_id for j in page.items]
    assert len(ids) == 2 and ids == sorted(ids)
    rest = validate(schemas.JobList, api.get(f"workflows/{pk}/jobs", params={"after": ids[-1], "limit": 2}))
    assert len(rest.items) == 1 and rest.items[0].job_id > ids[-1]
    validate(schemas.Detail, api.get("workflows/999999999/jobs"), 404)

    batch = claim(api, wf["workflow_id"], limit=1)
    assert batch is not None
    submitted(api, batch["run_name"], [j["sample_id"] for j in batch["jobs"]])
    weblog(edge, batch["run_name"], "started")
    mark_complete(edge, batch["run_name"], batch["jobs"][0]["sample_id"])

    def completed() -> None:
        done = validate(schemas.JobList, api.get(f"workflows/{pk}/jobs", params={"status": "completed"}))
        assert len(done.items) == 1
        item = done.items[0]
        assert item.sample_key.startswith("RS.") and item.readset_id == item.sample_key
        assert item.sample_id in {s["sample_id"] for s in samples}
        assert item.collections == [c]
        assert item.completed_at

    eventually(completed, lag)
