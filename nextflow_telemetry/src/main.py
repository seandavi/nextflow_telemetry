from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .log import logger
from . import models

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
)


@app.post("/telemetry")
async def telemetry(body: dict):
    logger.debug(body)
    tel = models.Telemetry(**body)
    logger.debug(tel)
    return body


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8091, reload=True)
