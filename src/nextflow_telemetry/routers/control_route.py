"""Shared plumbing for the control-plane routers (ADR-0013 #373)."""
from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


class ControlRoute(APIRoute):
    """v2's wire errors: a 422 body is {"detail": "<message>"}, not FastAPI's error list."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def with_string_detail(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                detail = "; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors())
                return JSONResponse({"detail": detail}, status_code=422)

        return with_string_detail


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clamp(n: int, lo: int, hi: int) -> int:
    return min(hi, max(lo, n))
