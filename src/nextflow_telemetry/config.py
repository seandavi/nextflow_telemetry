import os
from dataclasses import dataclass


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}

def _normalize_sqlalchemy_uri(uri: str) -> str:
    # Ensure async SQLAlchemy uses asyncpg when URI is provided without explicit driver.
    if uri.startswith("postgresql://"):
        return uri.replace("postgresql://", "postgresql+asyncpg://", 1)
    return uri


@dataclass
class Settings:
    SQLALCHEMY_URI: str
    SKIP_DB_INIT: bool
    CORS_ORIGINS: list[str]

settings = Settings(
    SQLALCHEMY_URI=_normalize_sqlalchemy_uri(
        os.environ.get("SQLALCHEMY_URI", "postgresql://postgres:postgres@localhost:5432/cmdg_dev")
    ),
    SKIP_DB_INIT=_as_bool(os.environ.get("TELEMETRY_SKIP_DB_INIT", "0")),
    CORS_ORIGINS=os.environ.get("CORS_ORIGINS", "*").split(","),
)
