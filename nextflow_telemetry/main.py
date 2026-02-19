from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .log import logger
from . import models
from .config import settings

app = FastAPI()

from sqlalchemy import DateTime, create_engine
from sqlalchemy import Table, Column, Integer, String, MetaData, insert, select
from sqlalchemy.dialects.postgresql import JSONB

engine = create_engine(settings.SQLALCHEMY_URI)

metadata = MetaData()

telemetry_tbl = Table(
    'telemetry', 
    metadata,
    Column('id', Integer, primary_key=True),
    Column('run_id', String),
    Column('run_name', String),
    Column('event', String),
    Column('timestamp', DateTime),
    Column('metadata', JSONB),
    Column('trace', JSONB),
)

if not settings.SKIP_DB_INIT:
    metadata.create_all(engine)


app.add_middleware(
    CORSMiddleware,
    # Security exception: wildcard CORS is currently required by deployment constraints.
    # Treat this as an accepted risk and tighten origins when feasible.
    allow_origins=["*"],
)

# health check
@app.get("/health")
async def healthcheck():
    try:
        with engine.connect() as conn:
            conn.execute(select(1))  # Simple DB check
        return {"message": "App Started", "status": "Healthy", "database": "Connected"}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={"message": "App Started", "status": "Unhealthy", "database": f"Error: {str(e)}"},
        )


@app.post("/telemetry")
async def telemetry(body: dict):
    #logger.debug(body)
    try:
        del(body['metadata']['workflow']['start']['offset']['availableZoneIds'])
    except (KeyError, TypeError):
        pass
    try:
        del(body['metadata']['workflow']['complete']['offset']['availableZoneIds'])
    except (KeyError, TypeError):
        pass
    tel = models.Telemetry(**body)
    logger.debug(tel)
    with engine.begin() as conn:
        conn.execute(insert(telemetry_tbl).values(
            run_id=tel.run_id,
            run_name=tel.run_name,
            event=tel.event,
            timestamp=tel.timestamp,
            metadata=tel.metadata,
            trace=tel.trace
        ))
    return body


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8091, reload=True)
