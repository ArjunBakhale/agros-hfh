"""Background worker: call dispatch, fixture simulation, timeout sweep."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from . import jobs, services
from .config import get_settings
from .db import unscoped

log = logging.getLogger("agros_voice.worker")

Poster = Callable[[str, bytes, dict[str, str]], int]


def http_poster() -> Poster:
    base = get_settings().webhook_base_url.rstrip("/")
    client = httpx.Client(timeout=10)

    def post(path: str, body: bytes, headers: dict[str, str]) -> int:
        r = client.post(
            base + path, content=body, headers={**headers, "Content-Type": "application/json"}
        )
        return r.status_code

    return post


def _handle(queue: str, job: dict[str, Any], post: Poster) -> None:
    p = job["payload"]
    tenant = str(job["tenant_id"])
    if queue == jobs.DISPATCH_QUEUE:
        services.dispatch_attempt(tenant, p["attempt_id"])
    elif queue == services.FIXTURE_RUN_QUEUE:
        services.run_fixture_call(tenant, p["attempt_id"], post)


def run_once(post: Poster) -> int:
    """Process every due job once. Returns the number processed."""
    n = 0
    for queue in (jobs.DISPATCH_QUEUE, services.FIXTURE_RUN_QUEUE):
        while True:
            with unscoped() as conn:
                job = jobs.claim(conn, queue)
            if not job:
                break
            n += 1
            try:
                _handle(queue, job, post)
            except Exception as exc:  # noqa: BLE001 - every failure is retried or dead-lettered
                with unscoped() as conn:
                    status = jobs.fail(conn, job, f"{type(exc).__name__}: {exc}")
                log.warning("job %s on %s failed (%s): %s", job["id"], queue, status, exc)
                continue
            with unscoped() as conn:
                jobs.complete(conn, job["id"])
    return n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    post = http_poster()
    last_sweep = 0.0
    log.info("worker started")
    while True:
        try:
            processed = run_once(post)
            if time.time() - last_sweep > 60:
                swept = services.sweep_timeouts()
                if swept:
                    log.info("timed out %d attempts", swept)
                last_sweep = time.time()
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            processed = 0
        if not processed:
            time.sleep(1)
