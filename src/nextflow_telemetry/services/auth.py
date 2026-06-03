"""User/role lookup service."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import users_tbl


@dataclass
class UserService:
    engine: AsyncEngine

    async def get_role(self, email: str) -> str | None:
        """Return the role for an email, or None if the user isn't registered."""
        async with self.engine.begin() as conn:
            row = (await conn.execute(
                select(users_tbl.c.role).where(users_tbl.c.email == email)
            )).first()
        return row[0] if row else None
