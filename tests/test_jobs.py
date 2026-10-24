from conftest import new_key

from agros_voice import jobs, services
from agros_voice.db import unscoped, with_tenant


def test_retry_then_dead_letter(database) -> None:  # type: ignore[no-untyped-def]
    t1 = list(database.values())[0]
    with with_tenant(t1) as conn:
        jid = jobs.enqueue(conn, "test-q", t1, {"x": 1}, max_attempts=2)
    statuses = []
    for _ in range(2):
        with unscoped() as conn:
            conn.execute("update jobs set run_after=now() where id=%s", (jid,))
            job = jobs.claim(conn, "test-q")
            assert job and job["id"] == jid
            statuses.append(jobs.fail(conn, job, "boom"))
    assert statuses == ["pending", "dead"]
    with unscoped() as conn:
        assert jobs.claim(conn, "test-q") is None


def test_singleton_dedupes(database) -> None:  # type: ignore[no-untyped-def]
    t1 = list(database.values())[0]
    with with_tenant(t1) as conn:
        assert jobs.enqueue(conn, "test-s", t1, {}, singleton_key="k1") is not None
        assert jobs.enqueue(conn, "test-s", t1, {}, singleton_key="k1") is None


def test_webhook_delivery_failure_retries(client, auth, fresh_contact) -> None:  # type: ignore[no-untyped-def]
    from agros_voice import worker

    h = auth("admin")
    cid = fresh_contact()
    a = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls",
        headers={**h, "Idempotency-Key": new_key()},
        json={"persona": "interested"},
    ).json()
    worker.run_once(lambda *_: 503)
    with unscoped() as conn:
        row = conn.execute(
            "select status, attempts, last_error from jobs where singleton_key=%s",
            (f"fixture:{a['id']}",),
        ).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1 and "503" in row["last_error"]


def test_timeout_sweep(client, auth, fresh_contact, settings) -> None:  # type: ignore[no-untyped-def]
    h = auth("admin")
    cid = fresh_contact()
    a = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls", headers={**h, "Idempotency-Key": new_key()}
    ).json()
    services.dispatch_attempt(a["tenant_id"], a["id"])
    settings.voice_call_timeout_minutes = 0
    assert services.sweep_timeouts() >= 1
    att = client.get(f"/api/voice-enrollment/attempts/{a['id']}", headers=h).json()
    assert att["status"] == "timed_out"
    d = client.get("/api/voice-enrollment/dashboard", headers=h).json()
    assert any(x["contact_id"] == cid and x["kind"] == "human_review" for x in d["handoffs"])


def test_provider_unavailable_fails_attempt(client, auth, fresh_contact, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from agros_voice.providers import ProviderUnavailable
    from agros_voice.providers.fixture import FixtureProvider

    def boom(self, inp):  # type: ignore[no-untyped-def]
        raise ProviderUnavailable("down")

    monkeypatch.setattr(FixtureProvider, "start_call", boom)
    h = auth("admin")
    a = client.post(
        f"/api/voice-enrollment/contacts/{fresh_contact()}/calls",
        headers={**h, "Idempotency-Key": new_key()},
    ).json()
    assert services.dispatch_attempt(a["tenant_id"], a["id"]) == "failed"
    assert (
        client.get(f"/api/voice-enrollment/attempts/{a['id']}", headers=h).json()["status"]
        == "failed"
    )
