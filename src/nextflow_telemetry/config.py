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
    # Bearer token for every control-plane write (ADR-0013 §6): dispatch,
    # registration, workflows, daemons, admin. The event routes are exempt. Set to
    # the v2 API token (GSM cdsci-nf-telemetry-v2-api-token). When empty,
    # require_service is permissive (logs a one-time warning).
    DISPATCH_TOKEN: str
    # Operator/CI auth for mutating catalog endpoints (e.g. POST /submissions).
    # Separate from DISPATCH_TOKEN so operator/CI credentials and the daemon's
    # dispatch credential can be revoked independently. Empty ⇒ token auth is
    # disabled and those endpoints require a logged-in contributor/admin session.
    OPERATOR_TOKEN: str
    # Cookie domain — set to ".cancerdatasci.org" in prod so the cookie is
    # sent by the SPA at cmgd.cancerdatasci.org to the API at
    # nf-telemetry.cancerdatasci.org (same eTLD+1, so SameSite=Lax works).
    # Empty in local dev makes the cookie host-only.
    SESSION_COOKIE_DOMAIN: str
    # Where to send the user after a successful OAuth callback. Typically
    # the frontend origin in prod; "/" works in dev when the SPA is
    # proxied through the same origin.
    FRONTEND_URL: str
    # Historical metrics tier: DuckDB over v2's NDJSON event archive.
    # EVENTS_ARCHIVE_URL is the directory holding the dt=YYYY-MM-DD partitions;
    # a local path works for tests. The R2_* keys (cdsci-r2-* in GCP SM) are
    # needed only for r2:// URLs.
    EVENTS_ARCHIVE_URL: str
    EVENTS_ARCHIVE_REFRESH_SECONDS: float
    R2_ACCOUNT_ID: str
    R2_ACCESS_KEY_ID: str
    R2_SECRET_ACCESS_KEY: str
    # v2 control-plane API (the Worker): run → workflow attribution and
    # collection membership for the historical tier.
    V2_API_URL: str
    # Bearer token for v2 writes (GSM cdsci-nf-telemetry-v2-api-token). Empty
    # disables the submission write-through to v2 POST /samples.
    V2_API_TOKEN: str
    # Control-plane run deadlines (#373); the values match cf/wrangler.jsonc. Floats
    # so a contract target can use fractional minutes. M4's sweep reads these.
    CLAIM_TTL_MINUTES: float
    SUBMIT_BACKSTOP_HOURS: float
    LIVENESS_MINUTES: float
    # Where task logs and run logs live: r2://<bucket>, or a local directory path.
    # No default: the service refuses to start without it, so a dev or contract
    # run can never write into the production bucket the Worker owns.
    BLOB_STORE_URL: str

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
    OPERATOR_TOKEN=os.environ.get("OPERATOR_TOKEN", ""),
    SESSION_COOKIE_DOMAIN=os.environ.get("SESSION_COOKIE_DOMAIN", ""),
    FRONTEND_URL=os.environ.get("FRONTEND_URL", "/"),
    EVENTS_ARCHIVE_URL=os.environ.get("EVENTS_ARCHIVE_URL", "r2://nf-telemetry/telemetry/events"),
    EVENTS_ARCHIVE_REFRESH_SECONDS=float(os.environ.get("EVENTS_ARCHIVE_REFRESH_SECONDS", "300")),
    R2_ACCOUNT_ID=os.environ.get("R2_ACCOUNT_ID", ""),
    R2_ACCESS_KEY_ID=os.environ.get("R2_ACCESS_KEY_ID", ""),
    R2_SECRET_ACCESS_KEY=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
    V2_API_URL=os.environ.get("V2_API_URL", "https://nf-telemetry.seandavi.workers.dev/api"),
    V2_API_TOKEN=os.environ.get("V2_API_TOKEN", ""),
    CLAIM_TTL_MINUTES=float(os.environ.get("CLAIM_TTL_MINUTES", "5")),
    SUBMIT_BACKSTOP_HOURS=float(os.environ.get("SUBMIT_BACKSTOP_HOURS", "168")),
    LIVENESS_MINUTES=float(os.environ.get("LIVENESS_MINUTES", "10")),
    BLOB_STORE_URL=os.environ.get("BLOB_STORE_URL", ""),
)
