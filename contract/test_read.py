import httpx
import pytest
from pydantic import TypeAdapter

from contract import schemas
from contract.support import validate

HISTORICAL_ROUTES = [
    "metrics/processes/summary",
    "metrics/processes/retries",
    "metrics/processes/resources-by-attempt",
    "metrics/processes/hosts",
    "metrics/processes/failures",
    "metrics/processes/failure-signatures",
    "metrics/processes/tasks",
    "metrics/processes/timeline",
]


def test_health(api: httpx.Client) -> None:
    validate(schemas.Health, api.get("health"))


def test_stats(api: httpx.Client) -> None:
    stats = validate(schemas.Stats, api.get("admin/stats"))
    assert stats.samples >= 0 and stats.workflows >= 0


def test_workflows(api: httpx.Client) -> None:
    adapter = TypeAdapter(list[schemas.Workflow])
    workflows = validate(adapter, api.get("workflows"))
    validate(adapter, api.get("workflows", params={"status": "active"}))
    if workflows:
        pk = workflows[0].id
        validate(schemas.Workflow, api.get(f"workflows/{pk}"))
        validate(schemas.JobSummary, api.get(f"workflows/{pk}/job-summary"))
        jobs = validate(schemas.JobList, api.get(f"workflows/{pk}/jobs", params={"limit": 5}))
        assert jobs.limit == 5 and jobs.after == 0
    validate(schemas.Detail, api.get("workflows/999999999"), 404)


def test_samples(api: httpx.Client) -> None:
    page = validate(schemas.SampleList, api.get("samples", params={"limit": 5, "offset": 0}))
    assert page.limit == 5 and page.offset == 0
    assert page.total >= len(page.items)
    validate(schemas.CollectionFacets, api.get("samples/facets/collections"))
    if not page.items:
        pytest.skip("target holds no samples")
    first = page.items[0]
    validate(schemas.Sample, api.get(f"samples/{first.sample_id}"))
    first_run = (first.ncbi_accession or "").split(";")[0]
    if first_run:
        validate(schemas.Sample, api.get(f"samples/by-srr/{first_run}"))
    if first.collections:
        params = {"collection": first.collections[0], "limit": 1000, "offset": 0}
        validate(schemas.SampleList, api.get("samples", params=params))


def test_cohorts(api: httpx.Client, ns: str) -> None:
    adapter = TypeAdapter(list[schemas.Cohort])
    cohorts = validate(adapter, api.get("cohorts"))
    validate(adapter, api.get("cohorts/leaderboard"))
    assert api.get(f"cohorts/{ns}-nosuch/summary").status_code == 404
    if not cohorts:
        pytest.skip("target holds no cohorts")
    validate(schemas.CohortSummary, api.get(f"cohorts/{cohorts[0].collection_id}/summary"))


def test_runs(api: httpx.Client, ns: str) -> None:
    validate(schemas.RunList, api.get("runs"))
    page = validate(schemas.RunList, api.get("runs", params={"limit": 5, "offset": 0}))
    assert page.limit == 5 and page.offset == 0
    assert api.get(f"runs/{ns}-nosuch").status_code == 404
    if not page.runs:
        pytest.skip("target holds no runs")
    validate(schemas.RunDetail, api.get(f"runs/{page.runs[0].run_name}"))


def test_daemons(api: httpx.Client) -> None:
    adapter = TypeAdapter(list[schemas.Daemon])
    validate(adapter, api.get("daemons"))
    validate(adapter, api.get("daemons/"))
    active = validate(adapter, api.get("daemons/", params={"active_only": "true"}))
    assert all(d.is_active for d in active)


def test_dispatchability(api: httpx.Client) -> None:
    validate(schemas.Dispatchability, api.get("admin/dispatchability"))


def test_running_processes(api: httpx.Client) -> None:
    validate(schemas.RunningProcesses, api.get("metrics/processes/running"))


@pytest.mark.parametrize("route", [*HISTORICAL_ROUTES, "cohorts/COHORT/failures?process=x"])
def test_historical_tier(api: httpx.Client, ns: str, route: str) -> None:
    if "COHORT" in route:
        cohorts = api.get("cohorts").json()
        route = route.replace("COHORT", cohorts[0]["collection_id"] if cohorts else ns)
    resp = api.get(route)
    assert resp.status_code in {200, 501}, f"GET {route}: {resp.status_code} {resp.text[:500]}"
    resp.json()
