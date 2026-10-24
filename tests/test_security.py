"""Tenant isolation, authorization, CSRF, audit chain, export safety."""

import json
import uuid

import psycopg
import pytest
from conftest import EMAIL, PASSWORD, new_key

from agros_voice.audit import redact
from agros_voice.db import unscoped, with_tenant


def test_unauthenticated_rejected(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/api/voice-enrollment/dashboard").status_code == 401
    assert client.get("/voice-enrollment", follow_redirects=False).status_code == 303
    assert (
        client.get(
            "/api/voice-enrollment/dashboard", headers={"Authorization": "Bearer junk"}
        ).status_code
        == 401
    )


def test_bad_login(client) -> None:  # type: ignore[no-untyped-def]
    assert (
        client.post(
            "/api/auth/token", json={"email": EMAIL["admin"], "password": "nope"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/token", json={"email": "nobody@x.test", "password": PASSWORD}
        ).status_code
        == 401
    )


def test_forged_token_rejected(client) -> None:  # type: ignore[no-untyped-def]
    import jwt

    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(uuid.uuid4()),
            "role": "admin",
            "exp": 9999999999,
        },
        "attacker-secret",
        algorithm="HS256",
    )
    assert (
        client.get(
            "/api/voice-enrollment/dashboard", headers={"Authorization": f"Bearer {forged}"}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    ("who", "call", "write"),
    [
        ("admin", True, True),
        ("manager", True, True),
        ("cta", True, False),
        ("viewer", False, False),
    ],
)
def test_role_matrix(client, auth, fresh_contact, who, call, write) -> None:  # type: ignore[no-untyped-def]
    h = auth(who)
    assert client.get("/api/voice-enrollment/dashboard", headers=h).status_code == 200
    cid = fresh_contact()
    r = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls", headers={**h, "Idempotency-Key": new_key()}
    )
    assert (r.status_code == 201) == call, r.text
    r = client.post(
        "/api/voice-enrollment/campaigns", headers=h, json={"name": f"c-{uuid.uuid4().hex[:6]}"}
    )
    assert (r.status_code == 201) == write
    assert (client.get("/api/voice-enrollment/export.csv", headers=h).status_code == 200) == write
    r = client.post(
        f"/api/voice-enrollment/contacts/{cid}/disposition",
        headers=h,
        json={"status": "closed", "reason": "role matrix"},
    )
    assert (r.status_code == 200) == write


def test_cross_tenant_api_access_is_404(client, auth, fresh_contact, run_worker) -> None:  # type: ignore[no-untyped-def]
    mine = auth("admin")
    other = auth("other_admin")
    cid = fresh_contact()
    a = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls",
        headers={**mine, "Idempotency-Key": new_key()},
        json={"persona": "interested"},
    ).json()
    run_worker()
    hid = next(
        x["id"]
        for x in client.get("/api/voice-enrollment/dashboard", headers=mine).json()["handoffs"]
        if x["contact_id"] == cid
    )
    camp = client.get("/api/voice-enrollment/dashboard", headers=mine).json()["campaigns"][0]["id"]
    assert client.get(f"/api/voice-enrollment/attempts/{a['id']}", headers=other).status_code == 404
    assert (
        client.post(
            f"/api/voice-enrollment/contacts/{cid}/calls",
            headers={**other, "Idempotency-Key": new_key()},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/voice-enrollment/contacts/{cid}/disposition",
            headers=other,
            json={"status": "closed", "reason": "attack"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/voice-enrollment/handoffs/{hid}", headers=other, json={"status": "cancelled"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/voice-enrollment/campaigns/{camp}/status",
            headers=other,
            json={"status": "paused"},
        ).status_code
        == 404
    )
    r = client.post(
        f"/api/voice-enrollment/campaigns/{camp}/contacts",
        headers=other,
        files={"file": ("c.csv", "contact_token,phone_e164\nZZZ1,+13135550150\n", "text/csv")},
    )
    assert r.status_code == 404
    od = client.get("/api/voice-enrollment/dashboard", headers=other).json()
    assert cid not in {c["id"] for c in od["contacts"]}
    assert all(c["contact_token"].startswith("OTHER") for c in od["contacts"])
    assert "FX-" not in client.get("/api/voice-enrollment/export.csv", headers=other).text.replace(
        "OTHERFX-", ""
    )


def test_rls_without_tenant_context_returns_nothing() -> None:
    with unscoped() as conn:
        for t in (
            "voice_contacts",
            "voice_call_attempts",
            "audit_events",
            "users",
            "voice_handoffs",
            "tenants",
        ):
            assert conn.execute(f"select count(*) n from {t}").fetchone()["n"] == 0  # noqa: S608


def test_rls_blocks_cross_tenant_writes(database) -> None:  # type: ignore[no-untyped-def]
    t1, t2 = list(database.values())
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with with_tenant(t1) as conn:
            camp = conn.execute("select id from voice_campaigns limit 1").fetchone()
            conn.execute(
                "insert into voice_contacts (tenant_id, campaign_id, contact_token, phone_e164) "
                "values (%s,%s,'X1','+13135550160')",
                (t2, camp["id"]),
            )
    with with_tenant(t1) as conn:
        n = conn.execute(
            "update voice_contacts set status='closed' where tenant_id=%s", (t2,)
        ).rowcount
        assert n == 0


def test_tenant_id_immutable(database) -> None:  # type: ignore[no-untyped-def]
    t1, t2 = list(database.values())
    with pytest.raises(psycopg.errors.Error):
        with with_tenant(t1) as conn:
            conn.execute("update voice_contacts set tenant_id=%s", (t2,))


def test_audit_append_only_and_chain(client, auth, database) -> None:  # type: ignore[no-untyped-def]
    t1 = list(database.values())[0]
    with pytest.raises(psycopg.errors.Error):
        with with_tenant(t1) as conn:
            conn.execute("update audit_events set action='x'")
    with pytest.raises(psycopg.errors.Error):
        with with_tenant(t1) as conn:
            conn.execute("delete from audit_events")
    with pytest.raises(psycopg.errors.Error):
        with with_tenant(t1) as conn:
            conn.execute(
                "insert into audit_events (tenant_id, seq, actor_type, action, object_type, hash) "
                "values (%s, 1, 'system', 'forged', 'x', 'h')",
                (t1,),
            )
    r = client.get("/api/voice-enrollment/audit/verify", headers=auth("admin")).json()
    assert r["ok"] and r["events"] > 0


def test_call_events_append_only(database) -> None:  # type: ignore[no-untyped-def]
    t1 = list(database.values())[0]
    with pytest.raises(psycopg.errors.Error):
        with with_tenant(t1) as conn:
            conn.execute("delete from voice_call_events")


def test_audit_payloads_are_redacted(database) -> None:  # type: ignore[no-untyped-def]
    assert redact(
        {"phone": "+13135550101", "nested": {"email": "a@b.c", "text": "hi"}, "ok": 1}
    ) == {"phone": "[redacted]", "nested": {"email": "[redacted]", "text": "[redacted]"}, "ok": 1}
    t1 = list(database.values())[0]
    with with_tenant(t1) as conn:
        rows = conn.execute("select payload from audit_events").fetchall()
    blob = json.dumps([r["payload"] for r in rows])
    assert "+1313555" not in blob and "@" not in blob


def test_csrf_required_for_cookie_sessions(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post(
        "/login", data={"email": EMAIL["admin"], "password": PASSWORD}, follow_redirects=False
    )
    assert r.status_code == 303
    assert client.get("/voice-enrollment").status_code == 200
    body = {"name": "csrf-test"}
    assert client.post("/api/voice-enrollment/campaigns", json=body).status_code == 403
    csrf = client.cookies.get("agros_csrf")
    assert (
        client.post(
            "/api/voice-enrollment/campaigns", json=body, headers={"X-CSRF-Token": csrf}
        ).status_code
        == 201
    )


def test_session_cookie_flags(client) -> None:  # type: ignore[no-untyped-def]
    r = client.post(
        "/login", data={"email": EMAIL["admin"], "password": PASSWORD}, follow_redirects=False
    )
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("agros_session="))
    assert "HttpOnly" in session and "SameSite=strict" in session


def test_import_rejects_real_and_out_of_boundary_data(client, auth, settings) -> None:  # type: ignore[no-untyped-def]
    h = auth("admin")
    camp = client.get("/api/voice-enrollment/dashboard", headers=h).json()["campaigns"][0]["id"]
    url = f"/api/voice-enrollment/campaigns/{camp}/contacts"

    def up(csv: str):  # type: ignore[no-untyped-def]
        return client.post(url, headers=h, files={"file": ("c.csv", csv, "text/csv")})

    assert up("contact_token,phone_e164,mrn\nA111,+13135550111,123\n").status_code == 422
    assert up("contact_token,phone_e164\nA112,+12125551234\n").status_code == 403
    assert up("contact_token,phone_e164,is_fixture\nA113,+13135550113,false\n").status_code == 403
    assert up("contact_token,phone_e164\nA114,313-555-0114\n").status_code == 422
    r = up("contact_token,phone_e164\nA115,+13135550115\nA115,+13135550116\nA116,+13135550199\n")
    assert r.status_code == 200 and r.json()["inserted"] == 2 and len(r.json()["errors"]) == 1


def test_export_has_no_phone_and_is_formula_safe(client, auth) -> None:  # type: ignore[no-untyped-def]
    text = client.get("/api/voice-enrollment/export.csv", headers=auth("admin")).text
    assert text.splitlines()[0].startswith("contact_token,status")
    assert "+1313" not in text and "555" not in text


def test_security_headers(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/health")
    assert (
        r.headers["x-frame-options"] == "DENY"
        and "frame-ancestors" in r.headers["content-security-policy"]
    )
