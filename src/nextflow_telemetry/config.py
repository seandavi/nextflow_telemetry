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
    # OAuth + session — populated by deploy from GCP Secret Manager.
    # Empty defaults are valid in dev/test; /auth/login returns 503 until set.
    OAUTH_CLIENT_ID: str
    OAUTH_CLIENT_SECRET: str
    OAUTH_REDIRECT_URI: str
    SESSION_SECRET: str
    # Machine auth for nf-client dispatch endpoints. When empty, require_service
    # is permissive (logs a one-time warning) so daemons keep working during
    # the rollout window. Flip from empty → populated to enforce.
    DISPATCH_TOKEN: str

settings = Settings(
    SQLALCHEMY_URI=_normalize_sqlalchemy_uri(
        os.environ.get("SQLALCHEMY_URI", "postgresql://postgres:postgres@localhost:5432/cmdg_dev")
    ),
    SKIP_DB_INIT=_as_bool(os.environ.get("TELEMETRY_SKIP_DB_INIT", "0")),
    CORS_ORIGINS=os.environ.get("CORS_ORIGINS", "*").split(","),
    OAUTH_CLIENT_ID=os.environ.get("OAUTH_CLIENT_ID", ""),
    OAUTH_CLIENT_SECRET=os.environ.get("OAUTH_CLIENT_SECRET", ""),
    OAUTH_REDIRECT_URI=os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback"),
    SESSION_SECRET=os.environ.get("SESSION_SECRET", "dev-insecure-do-not-use-in-prod"),
    DISPATCH_TOKEN=os.environ.get("DISPATCH_TOKEN", ""),
)
