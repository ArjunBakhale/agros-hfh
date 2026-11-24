from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

PG_ADMIN = os.environ.get("TEST_PG_ADMIN_URL", "postgresql://postgres:postgres@localhost:55432")
TEST_DB = "agros_test"
APP_PW = "agros_app_test_pw"
PASSWORD = "Sandbox-pass-123"

os.environ.update(
    {
        "DATABASE_URL": f"postgresql://agros_app:{APP_PW}@{PG_ADMIN.split('@', 1)[1]}/{TEST_DB}",
        "ADMIN_DATABASE_URL": f"{PG_ADMIN}/{TEST_DB}",
        "VOICE_AGENT_ENABLED": "true",
        "VOICE_AGENT_PROVIDER": "fixture",
        "VOICE_WEBHOOK_SECRET": "test-webhook-secret-0123456789abcdef",
        "SESSION_SECRET": "test-session-secret-0123456789abcdef",
        "APP_ENV": "test",
        "VOICE_LIVE_CALL_ALLOWLIST": "+13135550199",
    }
)


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[dict[str, str]]:
    from agros_voice import cli, db
    from agros_voice.config import get_settings

    get_settings.cache_clear()
    with psycopg.connect(f"{PG_ADMIN}/postgres", autocommit=True) as c:
        c.execute(f"drop database if exists {TEST_DB} with (force)")
        c.execute(f"create database {TEST_DB}")
    db.migrate(os.environ["ADMIN_DATABASE_URL"], APP_PW)
    tenants = cli.seed(os.environ["ADMIN_DATABASE_URL"], PASSWORD)
    yield tenants
    db.close_pool()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Mutable settings; restored after each test."""
    from agros_voice.config import get_settings

    s = get_settings()
    snapshot = s.model_dump()
    yield s
    for k, v in snapshot.items():
        object.__setattr__(s, k, v)


@pytest.fixture
def client() -> Any:
    from fastapi.testclient import TestClient

    from agros_voice.api import app

    return TestClient(app)


EMAIL = {
    "admin": "admin@agros-demo.test",
    "manager": "admin+manager@agros-demo.test",
    "cta": "admin+cta@agros-demo.test",
    "viewer": "admin+viewer@agros-demo.test",
    "other_admin": "other+admin@agros-demo.test",
}


def token(client: Any, who: str) -> dict[str, str]:
    r = client.post("/api/auth/token", json={"email": EMAIL[who], "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def auth(client: Any) -> Any:
    return lambda who: token(client, who)


def poster(client: Any) -> Any:
    def post(path: str, body: bytes, headers: dict[str, str]) -> int:
        return int(
            client.post(
                path, content=body, headers={**headers, "Content-Type": "application/json"}
            ).status_code
        )

    return post


@pytest.fixture
def run_worker(client: Any) -> Any:
    from agros_voice import worker

    return lambda: worker.run_once(poster(client))


@pytest.fixture
def fresh_contact(client: Any, auth: Any) -> Any:
    """Create a new fixture contact in the demo campaign; returns its id."""

    def make(who: str = "admin", flags: str = "true,true") -> str:
        h = auth(who)
        d = client.get("/api/voice-enrollment/dashboard", headers=h).json()
        camp = d["campaigns"][0]["id"]
        tok = "T" + uuid.uuid4().hex[:10]
        n = uuid.uuid4().int % 100
        adult, dial = flags.split(",")
        csv = f"contact_token,phone_e164,adult,on_or_preparing_for_dialysis\n{tok},+1313555{100 + n:04d},{adult},{dial}\n"
        r = client.post(
            f"/api/voice-enrollment/campaigns/{camp}/contacts",
            headers=h,
            files={"file": ("c.csv", csv, "text/csv")},
        )
        assert r.status_code == 200, r.text
        d = client.get("/api/voice-enrollment/dashboard", headers=h).json()
        return next(c["id"] for c in d["contacts"] if c["contact_token"] == tok)

    return make


def new_key() -> str:
    return "test-" + uuid.uuid4().hex
