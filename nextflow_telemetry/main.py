from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import DateTime, Column, ForeignKey, Integer, MetaData, String, Table, insert, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql import func

from .log import logger
from . import models
from .config import settings
from .routers.process_metrics import create_process_metrics_router
from .routers.jobs import create_jobs_router
from .routers.pipelines import create_pipelines_router
from .routers.samples import create_samples_router
from .services.jobs import JobsService
from .services.pipelines import PipelinesService
from .services.process_metrics import ProcessMetricsService
from .services.samples import SamplesService

app = FastAPI()

engine = create_async_engine(settings.SQLALCHEMY_URI)

metadata = MetaData()

telemetry_tbl = Table(
    "telemetry",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", String),
    Column("run_name", String),
    Column("event", String),
    Column("utc_time", DateTime(timezone=True)),
    Column("metadata_", JSONB),
    Column("trace", JSONB),
)

samples_tbl = Table(
    "samples",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", String, unique=True, nullable=False),
    Column("srr_accessions", JSONB),
    Column("metadata_", JSONB),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

pipelines_tbl = Table(
    "pipelines",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("pipeline_id", String, unique=True, nullable=False),
    Column("repository", String),
    Column("branch", String, server_default="main"),
    Column("description", String),
    Column("default_params", JSONB),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

jobs_tbl = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", Integer, ForeignKey("samples.id"), nullable=False),
    Column("pipeline_id", Integer, ForeignKey("pipelines.id"), nullable=False),
    Column("status", String, nullable=False, server_default="pending"),
    Column("submitted_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

@app.on_event("startup")
async def maybe_init_db() -> None:
    if settings.SKIP_DB_INIT:
        return
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


app.add_middleware(
    CORSMiddleware,
    # Security exception: wildcard CORS is currently required by deployment constraints.
    # Treat this as an accepted risk and tighten origins when feasible.
    allow_origins=["*"],
)

process_metrics_service = ProcessMetricsService(engine=engine)
app.include_router(create_process_metrics_router(process_metrics_service))

samples_service = SamplesService(engine=engine)
app.include_router(create_samples_router(samples_service))

pipelines_service = PipelinesService(engine=engine)
app.include_router(create_pipelines_router(pipelines_service))

jobs_service = JobsService(engine=engine)
app.include_router(create_jobs_router(jobs_service))

# health check
@app.get("/health", response_model=models.HealthResponse, responses={503: {"model": models.HealthErrorResponse}})
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
    # logger.debug(body)
    if isinstance(body.metadata, dict):
        try:
            del (body.metadata["workflow"]["start"]["offset"]["availableZoneIds"])
        except (KeyError, TypeError):
            pass
        try:
            del (body.metadata["workflow"]["complete"]["offset"]["availableZoneIds"])
        except (KeyError, TypeError):
            pass
    logger.debug(body)
    async with engine.begin() as conn:
        await conn.execute(
            insert(telemetry_tbl).values(
                run_id=body.run_id,
                run_name=body.run_name,
                event=body.event,
                utc_time=body.timestamp,
                metadata_=body.metadata,
                trace=body.trace,
            )
        )
    return body


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8091, reload=True)
