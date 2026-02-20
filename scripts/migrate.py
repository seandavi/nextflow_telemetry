from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import os
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "sql" / "migrations"


def normalize_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    return dsn


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def needs_no_transaction(sql: str) -> bool:
    for line in sql.splitlines()[:5]:
        if line.strip().lower() == "-- migrate: no-transaction":
            return True
    return False


CREATE_TRACKING_TABLE = """
create table if not exists schema_migrations (
  version text primary key,
  filename text not null,
  checksum text not null,
  applied_at timestamptz not null default now()
)
"""


async def ensure_tracking_table(conn: asyncpg.Connection) -> None:
    await conn.execute(CREATE_TRACKING_TABLE)


async def applied_versions(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch("select version, checksum from schema_migrations order by version")
    return {r["version"]: r["checksum"] for r in rows}


async def apply_migration(conn: asyncpg.Connection, path: Path) -> None:
    version = path.stem.split("_", 1)[0]
    sql = path.read_text()
    checksum = file_sha256(path)

    if needs_no_transaction(sql):
        await conn.execute(sql)
        await conn.execute(
            "insert into schema_migrations(version, filename, checksum, applied_at) values($1,$2,$3,$4)",
            version,
            path.name,
            checksum,
            dt.datetime.now(dt.timezone.utc),
        )
        return

    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            "insert into schema_migrations(version, filename, checksum, applied_at) values($1,$2,$3,$4)",
            version,
            path.name,
            checksum,
            dt.datetime.now(dt.timezone.utc),
        )


async def run_status(conn: asyncpg.Connection) -> int:
    await ensure_tracking_table(conn)
    applied = await applied_versions(conn)
    files = migration_files()

    print("version\tstate\tfilename")
    for path in files:
        version = path.stem.split("_", 1)[0]
        checksum = file_sha256(path)
        if version not in applied:
            state = "pending"
        elif applied[version] != checksum:
            state = "checksum_mismatch"
        else:
            state = "applied"
        print(f"{version}\t{state}\t{path.name}")

    mismatches = [p for p in files if p.stem.split("_", 1)[0] in applied and applied[p.stem.split("_", 1)[0]] != file_sha256(p)]
    return 1 if mismatches else 0


async def run_up(conn: asyncpg.Connection) -> None:
    await ensure_tracking_table(conn)
    applied = await applied_versions(conn)

    for path in migration_files():
        version = path.stem.split("_", 1)[0]
        checksum = file_sha256(path)
        if version in applied:
            if applied[version] != checksum:
                raise RuntimeError(f"Checksum mismatch for applied migration {path.name}")
            continue
        print(f"Applying {path.name} ...")
        await apply_migration(conn, path)
        print(f"Applied {path.name}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Simple SQL migration runner")
    parser.add_argument("command", choices=["up", "status"])
    parser.add_argument("--dsn", default=os.environ.get("SQLALCHEMY_URI", ""))
    args = parser.parse_args()

    if not args.dsn:
        raise SystemExit("SQLALCHEMY_URI (or --dsn) is required")

    dsn = normalize_dsn(args.dsn)
    conn = await asyncpg.connect(dsn, timeout=30)
    try:
        if args.command == "status":
            return await run_status(conn)
        await run_up(conn)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
