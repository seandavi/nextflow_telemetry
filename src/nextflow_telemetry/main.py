from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .config import settings
from .log import logger
from . import models
from .routers.admin import create_admin_router
from .routers.cohorts import create_cohorts_router
from .routers.curated import create_curated_router
from .routers.daemons import create_daemons_router
from .routers.dispatch import create_dispatch_router
from .routers.process_metrics import create_process_metrics_router
from .routers.runs import create_runs_router
from .routers.samples import create_samples_router
from .routers.task_logs import create_task_logs_router
from .routers.workflows import create_workflows_router
from .services.process_metrics import ProcessMetricsService
from .services.telemetry import TelemetryService

app = FastAPI(
    title="Nextflow Telemetry API",
    description=(
        "Central orchestration and observability service for Nextflow bioinformatics pipelines. "
        "Maintains a catalog of **samples** and a registry of **workflow** definitions, "
        "then manages the full lifecycle of **jobs** (one job = one sample × one workflow version): "
        "creation via reconciliation, dispatch to an executor, real-time progress via Nextflow weblog "
        "events, and automatic retry with dead-letter queuing on failure. "
        "\n\n"
        "**Typical operator flow:**\n"
        "1. Register workflows (`POST /workflows`) and samples (`POST /samples`).\n"
        "2. Reconcile to create pending jobs (`POST /admin/reconcile-jobs`).\n"
        "3. A client daemon claims a batch (`POST /dispatch/batch`), submits it to Nextflow, "
        "and confirms submission (`POST /dispatch/submitted`).\n"
        "4. Nextflow posts weblog events to `POST /telemetry`; the server updates job state in real time.\n"
        "5. On run completion the server sweeps unfinished jobs: retrying within budget or "
        "routing to the dead-letter queue."
    ),
    version="0.1.0",
)

engine = create_async_engine(settings.SQLALCHEMY_URI)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_log_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    # /health is hit every 30s by the Docker healthcheck — logging it at
    # INFO drowns out real signal, so it goes to DEBUG. Everything else
    # logs at INFO; unhandled exceptions log at ERROR with the traceback
    # before re-raising so FastAPI's normal error handling still runs.
    started = time.perf_counter()
    response: Response | None = None
    error: BaseException | None = None
    try:
        response = await call_next(request)
        return response
    except BaseException as exc:
        error = exc
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        path = request.url.path
        status = response.status_code if response is not None else 500
        if path == "/health":
            level = logging.DEBUG
        elif error is not None:
            level = logging.ERROR
        else:
            level = logging.INFO
        extra: dict[str, object] = {
            "method": request.method,
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
            "client": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        }
        if error is not None:
            extra["error"] = str(error)
        logger.log(level, "http.request", extra=extra, exc_info=error if error else None)

process_metrics_service = ProcessMetricsService(engine=engine)
telemetry_service = TelemetryService(engine=engine)

app.include_router(create_process_metrics_router(process_metrics_service), prefix="/api")
app.include_router(create_dispatch_router(engine), prefix="/api")
app.include_router(create_samples_router(engine), prefix="/api")
app.include_router(create_workflows_router(engine), prefix="/api")
app.include_router(create_admin_router(engine), prefix="/api")
app.include_router(create_task_logs_router(engine), prefix="/api")
app.include_router(create_daemons_router(engine), prefix="/api")
app.include_router(create_curated_router(engine), prefix="/api")
app.include_router(create_runs_router(engine), prefix="/api")
app.include_router(create_cohorts_router(engine), prefix="/api")


@app.get(
    "/health",
    response_model=models.HealthResponse,
    responses={503: {"model": models.HealthErrorResponse}},
    summary="Health check",
    description=(
        "Returns 200 when the API process is running and the database is reachable. "
        "Returns 503 with a JSON body describing the failure when the database cannot be reached. "
        "Safe to poll from load-balancer health checks."
    ),
    tags=["system"],
)
async def healthcheck():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"message": "App Started", "status": "Healthy", "database": "Connected"}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={"message": "App Started", "status": "Unhealthy", "database": f"Error: {str(e)}"},
        )


@app.post(
    "/telemetry",
    response_model=models.Telemetry,
    summary="Ingest a Nextflow weblog event",
    description=(
        "Receives a single event from Nextflow's `-with-weblog` reporter and persists it to the "
        "raw `telemetry` table. Depending on `event` type the server also updates higher-level "
        "state: `started` → marks the workflow run as running; `process_completed` on the "
        "`MARK_COMPLETE` sentinel process → marks the individual sample job as completed; "
        "`completed` → closes the run and sweeps any unfinished jobs (retry or dead-letter). "
        "Returns the parsed event so Nextflow can confirm receipt."
    ),
    tags=["telemetry"],
)
async def telemetry(body: models.Telemetry):
    # Strip large non-serialisable timezone blobs Nextflow sometimes includes
    if isinstance(body.metadata, dict):
        try:
            del body.metadata["workflow"]["start"]["offset"]["availableZoneIds"]
        except (KeyError, TypeError):
            pass
        try:
            del body.metadata["workflow"]["complete"]["offset"]["availableZoneIds"]
        except (KeyError, TypeError):
            pass

    logger.debug(body)
    await telemetry_service.ingest(body)
    return body


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
