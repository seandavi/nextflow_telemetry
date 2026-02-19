import os
from dataclasses import dataclass


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    SQLALCHEMY_URI: str
    SKIP_DB_INIT: bool

settings = Settings(
    SQLALCHEMY_URI=os.environ.get('SQLALCHEMY_URI', 'postgresql://postgres:postgres@localhost:5432/cmgd_dev'),
    SKIP_DB_INIT=_as_bool(os.environ.get("TELEMETRY_SKIP_DB_INIT", "0")),
)
