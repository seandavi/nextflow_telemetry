"""FastAPI dependencies for auth.

Three flavors:

- ``get_current_user``  — optional; returns the logged-in user or None.
- ``require_role``      — factory; raises 401/403 unless the user has the role
                          (admin implies contributor).
- ``require_service``   — bearer-token gate for nf-client dispatch endpoints.
                          When ``DISPATCH_TOKEN`` is empty, allows all
                          (logs a one-time warning) so we can ship the check
                          before daemons have been updated.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from .config import settings
from .log import logger
from .services.auth import UserService


@dataclass
class CurrentUser:
    email: str
    role: str | None  # None when the email isn't in users_tbl (logged in but no role)


# Cache the "service auth disabled" warning so we log it exactly once at first hit.
_service_auth_warned = False


def _user_service_from_request(request: Request) -> UserService:
    """Pull the AsyncEngine off app.state and wrap a UserService around it."""
    engine: AsyncEngine = request.app.state.engine
    return UserService(engine=engine)


async def get_current_user(request: Request) -> CurrentUser | None:
    """Return the current user from the session cookie, or None if anonymous."""
    email = request.session.get("email") if hasattr(request, "session") else None
    if not email:
        return None
    role = await _user_service_from_request(request).get_role(email)
    return CurrentUser(email=email, role=role)


def require_role(required: str):
    """Dep factory. ``required`` is 'admin' or 'contributor'.

    Admin satisfies 'contributor'. Anonymous → 401. Insufficient role → 403.
    """
    if required not in {"admin", "contributor"}:
        raise ValueError(f"Unknown role: {required}")

    async def _dep(user: CurrentUser | None = Depends(get_current_user)) -> CurrentUser:
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if user.role == "admin":
            return user
        if required == "contributor" and user.role == "contributor":
            return user
        raise HTTPException(status_code=403, detail=f"Role '{required}' required")

    return _dep


async def require_service(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate for daemon endpoints.

    When ``settings.DISPATCH_TOKEN`` is empty we let the request through and log
    a one-time warning. This makes the API safe to deploy ahead of daemons
    being updated to send the token. Flip the env var to enforce.
    """
    global _service_auth_warned
    if not settings.DISPATCH_TOKEN:
        if not _service_auth_warned:
            logger.warning(
                "service.auth.disabled",
                extra={"reason": "DISPATCH_TOKEN unset; dispatch endpoints are open"},
            )
            _service_auth_warned = True
        return

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    # Constant-time compare to avoid leaking the token byte-by-byte.
    if not hmac.compare_digest(token, settings.DISPATCH_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid bearer token")
