"""Minimal Postgres job queue: SKIP LOCKED claims, retries with backoff, dead letters."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from .db import Conn

DISPATCH_QUEUE = "voice-call-dispatch"


def enqueue(
    conn: Conn,
    queue: str,
    tenant_id: UUID | str,
    payload: dict[str, Any],
    *,
    singleton_key: str | None = None,
    max_attempts: int = 3,
) -> int | None:
    row = conn.execute(
        """insert into jobs (queue, tenant_id, payload, singleton_key, max_attempts)
           values (%s, %s, %s::jsonb, %s, %s)
           on conflict (queue, singleton_key) where singleton_key is not null do nothing
           returning id""",
        (queue, str(tenant_id), json.dumps(payload), singleton_key, max_attempts),
    ).fetchone()
    return int(row["id"]) if row else None


def claim(conn: Conn, queue: str, lock_seconds: int = 120) -> dict[str, Any] | None:
    return conn.execute(
        """update jobs set status='active', attempts=attempts+1,
                  locked_until=now() + make_interval(secs => %s)
           where id = (
             select id from jobs
              where queue=%s and run_after <= now()
                and (status='pending' or (status='active' and locked_until < now()))
              order by run_after, id
              for update skip locked limit 1)
           returning *""",
        (lock_seconds, queue),
    ).fetchone()


def complete(conn: Conn, job_id: int) -> None:
    conn.execute("update jobs set status='done', locked_until=null where id=%s", (job_id,))


def fail(conn: Conn, job: dict[str, Any], error: str) -> str:
    dead = job["attempts"] >= job["max_attempts"]
    status = "dead" if dead else "pending"
    conn.execute(
        """update jobs set status=%s, last_error=%s, locked_until=null,
                  run_after = now() + make_interval(secs => %s)
           where id=%s""",
        (status, error[:500], 5 * 2 ** job["attempts"], job["id"]),
    )
    return status
