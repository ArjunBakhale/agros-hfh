from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


Conn = psycopg.Connection[dict[str, Any]]


@contextmanager
def with_tenant(tenant_id: UUID | str) -> Iterator[Conn]:
    """Open a transaction whose every statement is RLS-scoped to exactly one tenant."""
    tid = str(UUID(str(tenant_id)))
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute("select set_config('app.tenant_id', %s, true)", (tid,))
            yield cast(Conn, conn)


@contextmanager
def unscoped() -> Iterator[Conn]:
    """Transaction with no tenant: RLS tables return zero rows. For SECURITY DEFINER
    lookups (login, webhook correlation) and the non-tenant job queue only."""
    with pool().connection() as conn:
        with conn.transaction():
            yield cast(Conn, conn)


def _migration_files() -> list[tuple[str, str]]:
    pkg = resources.files("agros_voice.migrations")
    files = sorted(p.name for p in pkg.iterdir() if p.name.endswith(".sql"))
    return [(name, (pkg / name).read_text()) for name in files]


def migrate(admin_url: str, app_password: str) -> list[str]:
    """Apply pending migrations as the owner role, then (re)create the RLS-subject app role."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{12,}", app_password):
        raise ValueError("APP_DB_PASSWORD must be >=12 chars of [A-Za-z0-9_-]")
    applied: list[str] = []
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(727001)")
        try:
            conn.execute(
                "create table if not exists schema_migrations "
                "(name text primary key, applied_at timestamptz not null default now())"
            )
            done = {r[0] for r in conn.execute("select name from schema_migrations").fetchall()}
            for name, stmt in _migration_files():
                if name in done:
                    continue
                with conn.transaction():
                    conn.execute(stmt)  # type: ignore[arg-type]
                    conn.execute("insert into schema_migrations(name) values (%s)", (name,))
                applied.append(name)
            exists = conn.execute("select 1 from pg_roles where rolname='agros_app'").fetchone()
            verb = "alter" if exists else "create"
            conn.execute(
                sql.SQL("{} role agros_app login nobypassrls password {}").format(
                    sql.SQL(verb), sql.Literal(app_password)
                )
            )
            conn.execute(
                """
                grant usage on schema public to agros_app;
                grant select, insert, update on all tables in schema public to agros_app;
                revoke update, delete on audit_events, voice_call_events from agros_app;
                revoke insert, update, delete on tenants from agros_app;
                grant usage, select on all sequences in schema public to agros_app;
                grant delete on jobs to agros_app;
                grant insert on voice_webhook_quarantine to agros_app;
                revoke insert, update, delete on schema_migrations from agros_app;
                """
            )
        finally:
            conn.execute("select pg_advisory_unlock(727001)")
    return applied
