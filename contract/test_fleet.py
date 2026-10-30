from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter

from contract import schemas
from contract.support import validate

pytestmark = pytest.mark.writes

DAEMONS = TypeAdapter(list[schemas.Daemon])


def _heartbeat(api: httpx.Client, agent_id: str) -> schemas.Daemon:
    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "hostname": "contract-host",
        "workflow_id": agent_id,
        "mode": "slurm",
        "batch_size": 1,
        "max_concurrent_runs": 1,
        "active_runs": 0,
        "status": "idle",
        "nf_client_version": "contract",
    }
    return validate(schemas.Daemon, api.put("daemons/heartbeat", json=payload))


def test_daemon_heartbeat_list_delete(api: httpx.Client, ns: str) -> None:
    agent_id = f"{ns}-host:wf"
    assert _heartbeat(api, agent_id).is_active

    assert agent_id in [d.agent_id for d in validate(DAEMONS, api.get("daemons/"))]
    active = validate(DAEMONS, api.get("daemons/", params={"active_only": "true"}))
    assert agent_id in [d.agent_id for d in active]

    assert validate(schemas.DeletedDaemon, api.delete(f"daemons/{agent_id}")).deleted == agent_id
    validate(schemas.Detail, api.delete(f"daemons/{agent_id}"), 404)


def test_dispatchability_counts_active_daemon(api: httpx.Client, ns: str) -> None:
    agent_id = f"{ns}-host:dispatch"
    _heartbeat(api, agent_id)
    try:
        assert validate(schemas.Dispatchability, api.get("admin/dispatchability")).active_daemons >= 1
    finally:
        api.delete(f"daemons/{agent_id}")
