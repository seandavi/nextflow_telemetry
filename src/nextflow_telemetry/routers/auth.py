"""Auth router — Google OAuth login + session cookie.

Flow:
  GET  /auth/login    → redirects user to Google's authorize URL
  GET  /auth/callback → exchanges code, stores email in session cookie
  GET  /auth/me       → {email, role} or 401
  POST /auth/logout   → clears session cookie

The cookie only carries the email; role is looked up fresh from users_tbl
on every request so role changes take effect without forcing a re-login.
"""
from __future__ import annotations

from authlib.integrations.starlette_client import OAuth, OAuthError  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from ..config import settings
from ..deps import CurrentUser, get_current_user


def create_auth_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    # Register only when configured so unit tests / dev environments without
    # OAuth credentials don't fail at import time. /auth/login returns 503
    # when the client isn't registered.
    oauth = OAuth()
    if settings.OAUTH_CLIENT_ID and settings.OAUTH_CLIENT_SECRET:
        oauth.register(
            name="google",
            client_id=settings.OAUTH_CLIENT_ID,
            client_secret=settings.OAUTH_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    @router.get("/login", summary="Begin Google OAuth login")
    async def login(request: Request):
        if oauth._clients.get("google") is None:
            raise HTTPException(
                status_code=503,
                detail="OAuth is not configured (OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET unset)",
            )
        return await oauth.google.authorize_redirect(request, settings.OAUTH_REDIRECT_URI)

    @router.get("/callback", summary="OAuth callback — exchanges the authorization code")
    async def callback(request: Request):
        if oauth._clients.get("google") is None:
            raise HTTPException(status_code=503, detail="OAuth is not configured")
        try:
            token = await oauth.google.authorize_access_token(request)
        except OAuthError as exc:
            raise HTTPException(status_code=400, detail=f"OAuth error: {exc.error}")

        userinfo = token.get("userinfo") or {}
        email = userinfo.get("email")
        if not email:
            raise HTTPException(status_code=400, detail="Google response missing email")

        request.session["email"] = email
        return RedirectResponse(url="/")

    @router.get(
        "/me",
        summary="Return the current user's identity and role",
        description=(
            "Returns {email, role} for the logged-in user. `role` is null when "
            "the email isn't yet registered in users_tbl (login worked, but the "
            "user has no permissions yet). Returns 401 when no session cookie "
            "is present."
        ),
    )
    async def me(user: CurrentUser | None = Depends(get_current_user)):
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return {"email": user.email, "role": user.role}

    @router.post("/logout", summary="Clear the session cookie")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    return router
