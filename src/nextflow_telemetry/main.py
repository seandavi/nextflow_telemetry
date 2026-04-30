from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .config import settings
from .log import logger
from . import models
from .routers.process_metrics import create_process_metrics_router
from .routers.dispatch import create_dispatch_router
from .services.process_metrics import ProcessMetricsService
from .services.telemetry import TelemetryService

app = FastAPI(title="Nextflow Telemetry API")

engine = create_async_engine(settings.SQLALCHEMY_URI)

app.add_middleware(
    CORSMiddleware,
    # Security exception: wildcard CORS is currently required by deployment constraints.
    allow_origins=["*"],
)

process_metrics_service = ProcessMetricsService(engine=engine)
telemetry_service = TelemetryService(engine=engine)

app.include_router(create_process_metrics_router(process_metrics_service))
app.include_router(create_dispatch_router(engine))


@app.get(
    "/health",
    response_model=models.HealthResponse,
    responses={503: {"model": models.HealthErrorResponse}},
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


@app.post("/telemetry", response_model=models.Telemetry)
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
