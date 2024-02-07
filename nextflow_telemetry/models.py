from pydantic import BaseModel, Field
from typing import Any, Optional
import datetime


class NextFlowVersion(BaseModel):
    """Nextflow version model"""

    version: str
    build: int
    timestamp: datetime.datetime


class Trace(BaseModel):
    """Trace model"""

    task_id: str
    hash: str
    process: str
    name: str
    status: str


class Workflow(BaseModel):
    # start: datetime.datetime
    project_dir: str = Field(..., alias="projectDir")
    complete: Optional[Any]
    profile: Optional[str]
    homeDir: Optional[str]
    workDir: Optional[str]
    container: Optional[Any]
    commitId: Optional[str]
    errorMessage: Optional[str]
    repository: Optional[str]
    containerEngine: Optional[str]
    scriptFile: Optional[str]
    userName: Optional[str]
    launchDir: Optional[str]
    configFiles: Optional[list[str]]
    sessionId: Optional[str]
    errorReport: Optional[str]
    scriptId: Optional[str]
    revision: Optional[str]
    commandLine: Optional[str]
    nextflow: Optional[NextFlowVersion]


class Metadata(BaseModel):
    """Metadata model"""

    params: Optional[dict[str, Any]] = None
    workflow: Optional[Workflow]


class Telemetry(BaseModel):
    """Telemetry model"""

    run_id: str = Field(..., alias="runId")
    run_name: str = Field(..., alias="runName")
    event: str
    timestamp: datetime.datetime = Field(..., alias="utcTime")
    metadata: Optional[Any] # Optional[Metadata]
    trace: Optional[Any] # Optional[Trace]
