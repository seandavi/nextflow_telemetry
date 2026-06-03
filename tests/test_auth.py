"""Tests for auth plumbing: session cookie, /auth/me, require_role, require_service."""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------

def test_me_returns_401_when_anonymous(app_module):
    with TestClient(app_module.app) as client:
        response = client.get("/auth/me")
    assert response.status_code == 401


def test_me_returns_email_and_role_when_session_set(app_module, monkeypatch):
    """Simulate a logged-in session by setting the cookie via a helper route."""
    from nextflow_telemetry.services.auth import UserService

    async def fake_get_role(self, email):
        return "admin" if email == "admin@example.com" else None

    monkeypatch.setattr(UserService, "get_role", fake_get_role)

    # Add a helper route to plant an email in the session — exercising the
    # same middleware path the OAuth callback uses, without faking the full
    # Google round-trip.
    @app_module.app.get("/_test/login_as")
    async def _login_as(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    with TestClient(app_module.app) as client:
        client.get("/_test/login_as", params={"email": "admin@example.com"})
        response = client.get("/auth/me")

    assert response.status_code == 200
    assert response.json() == {"email": "admin@example.com", "role": "admin"}


def test_me_returns_null_role_for_unregistered_user(app_module, monkeypatch):
    from nextflow_telemetry.services.auth import UserService

    async def fake_get_role(self, email):
        return None

    monkeypatch.setattr(UserService, "get_role", fake_get_role)

    @app_module.app.get("/_test/login_as2")
    async def _login_as2(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    with TestClient(app_module.app) as client:
        client.get("/_test/login_as2", params={"email": "stranger@example.com"})
        response = client.get("/auth/me")

    assert response.status_code == 200
    assert response.json() == {"email": "stranger@example.com", "role": None}


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------

def _build_role_test_app(role_for_email: dict[str, str | None]):
    """Standalone FastAPI app wired with our deps + a stub UserService."""
    from nextflow_telemetry.deps import get_current_user, require_role
    from nextflow_telemetry.services import auth as auth_service_mod

    async def fake_get_role(self, email):
        return role_for_email.get(email)

    auth_service_mod.UserService.get_role = fake_get_role  # type: ignore[method-assign]

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test")
    # Give get_current_user a stand-in engine via app.state.
    app.state.engine = object()  # never actually queried (we stubbed get_role)

    @app.get("/_test/login_as")
    async def _login_as(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    @app.get("/contributor-only")
    async def c(user=Depends(require_role("contributor"))):
        return {"email": user.email, "role": user.role}

    @app.get("/admin-only")
    async def a(user=Depends(require_role("admin"))):
        return {"email": user.email, "role": user.role}

    return app


def test_require_role_anonymous_gets_401(app_module):
    app = _build_role_test_app({})
    with TestClient(app) as client:
        assert client.get("/contributor-only").status_code == 401
        assert client.get("/admin-only").status_code == 401


def test_require_role_admin_satisfies_contributor(app_module):
    app = _build_role_test_app({"a@example.com": "admin"})
    with TestClient(app) as client:
        client.get("/_test/login_as", params={"email": "a@example.com"})
        assert client.get("/contributor-only").status_code == 200
        assert client.get("/admin-only").status_code == 200


def test_require_role_contributor_blocked_from_admin(app_module):
    app = _build_role_test_app({"c@example.com": "contributor"})
    with TestClient(app) as client:
        client.get("/_test/login_as", params={"email": "c@example.com"})
        assert client.get("/contributor-only").status_code == 200
        assert client.get("/admin-only").status_code == 403


def test_require_role_user_without_role_gets_403(app_module):
    app = _build_role_test_app({"x@example.com": None})
    with TestClient(app) as client:
        client.get("/_test/login_as", params={"email": "x@example.com"})
        assert client.get("/contributor-only").status_code == 403
        assert client.get("/admin-only").status_code == 403


# ---------------------------------------------------------------------------
# require_service
# ---------------------------------------------------------------------------

def test_require_service_allows_when_token_unset(app_module, monkeypatch):
    """With DISPATCH_TOKEN empty the dep is permissive (rollout-friendly)."""
    from nextflow_telemetry import deps
    from nextflow_telemetry.config import settings

    monkeypatch.setattr(settings, "DISPATCH_TOKEN", "")
    monkeypatch.setattr(deps, "_service_auth_warned", False)

    app = FastAPI()

    @app.get("/svc")
    async def svc(_=Depends(deps.require_service)):
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/svc").status_code == 200
        # And without bearer it still works
        assert client.get("/svc", headers={"authorization": "garbage"}).status_code == 200


def test_require_service_rejects_missing_and_wrong_tokens(app_module, monkeypatch):
    from nextflow_telemetry import deps
    from nextflow_telemetry.config import settings

    monkeypatch.setattr(settings, "DISPATCH_TOKEN", "the-right-token")

    app = FastAPI()

    @app.get("/svc")
    async def svc(_=Depends(deps.require_service)):
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/svc").status_code == 401
        assert client.get("/svc", headers={"authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/svc", headers={"authorization": "Bearer the-right-token"}).status_code == 200


# ---------------------------------------------------------------------------
# /auth/login behavior when OAuth isn't configured
# ---------------------------------------------------------------------------

def test_login_returns_503_when_oauth_unconfigured(app_module):
    """In unit-test env OAUTH_CLIENT_ID is empty by default — login should 503."""
    with TestClient(app_module.app) as client:
        response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 503
