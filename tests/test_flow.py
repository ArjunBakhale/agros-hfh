"""M0 end-to-end fixture flow through the HTTP API, worker and signed webhooks."""

import pytest
from conftest import new_key


def _call(client, h, cid, persona):  # type: ignore[no-untyped-def]
    r = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls",
        headers={**h, "Idempotency-Key": new_key()},
        json={"persona": persona},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize(
    ("persona", "flags", "disposition", "contact_status", "handoff"),
    [
        (
            "interested",
            "true,true",
            "scheduling_requested",
            "scheduling_requested",
            "coordinator_followup",
        ),
        ("declined", "true,true", "declined", "declined", None),
        ("clinical_question", "true,true", "needs_human", "needs_human", "human_review"),
        ("not_eligible", ",", "not_eligible", "screened", None),
        ("not_eligible", "true,true", "eligibility_mismatch", "needs_human", "human_review"),
        ("language", "true,true", "language_unsupported", "needs_human", "human_review"),
        ("opt_out", "true,true", "opted_out", "declined", None),
        ("wrong_number", "true,true", "wrong_number", "closed", None),
        ("unclear", "true,true", "no_response", "needs_human", "human_review"),
        ("no_answer", "true,true", "unreachable", "unreachable", None),
    ],
)
def test_persona_end_to_end(
    client,
    auth,
    fresh_contact,
    run_worker,
    persona,
    flags,
    disposition,  # type: ignore[no-untyped-def]
    contact_status,
    handoff,
) -> None:
    h = auth("cta")
    cid = fresh_contact("admin", flags)
    a = _call(client, h, cid, persona)
    assert a["status"] == "queued"
    run_worker()
    att = client.get(f"/api/voice-enrollment/attempts/{a['id']}", headers=h).json()
    assert att["disposition"] == disposition, att
    d = client.get("/api/voice-enrollment/dashboard", headers=h).json()
    c = next(c for c in d["contacts"] if c["id"] == cid)
    assert c["status"] == contact_status
    assert "phone_e164" not in c and c["phone_masked"].startswith("***")
    hs = [x for x in d["handoffs"] if x["contact_id"] == cid]
    assert [x["kind"] for x in hs] == ([handoff] if handoff else [])
    if persona in ("opt_out", "wrong_number"):
        assert c["do_not_call"] is True
        r = client.post(
            f"/api/voice-enrollment/contacts/{cid}/calls",
            headers={**h, "Idempotency-Key": new_key()},
        )
        assert r.status_code == 409


def test_idempotent_call_request(client, auth, fresh_contact) -> None:  # type: ignore[no-untyped-def]
    h = auth("admin")
    cid = fresh_contact()
    key = new_key()
    r1 = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls", headers={**h, "Idempotency-Key": key}
    )
    r2 = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls", headers={**h, "Idempotency-Key": key}
    )
    assert (r1.status_code, r2.status_code) == (201, 200)
    assert r1.json()["id"] == r2.json()["id"]
    other = fresh_contact()
    assert (
        client.post(
            f"/api/voice-enrollment/contacts/{other}/calls", headers={**h, "Idempotency-Key": key}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/voice-enrollment/contacts/{cid}/calls",
            headers={**h, "Idempotency-Key": new_key()},
        ).status_code
        == 409
    )  # in progress


def test_missing_idempotency_key(client, auth, fresh_contact) -> None:  # type: ignore[no-untyped-def]
    h = auth("admin")
    assert (
        client.post(
            f"/api/voice-enrollment/contacts/{fresh_contact()}/calls", headers=h
        ).status_code
        == 422
    )


def test_disabled_agent_blocks_calls(client, auth, fresh_contact, settings) -> None:  # type: ignore[no-untyped-def]
    settings.voice_agent_enabled = False
    h = auth("admin")
    r = client.post(
        f"/api/voice-enrollment/contacts/{fresh_contact()}/calls",
        headers={**h, "Idempotency-Key": new_key()},
    )
    assert r.status_code == 503


def test_live_provider_gates(client, auth, fresh_contact, settings) -> None:  # type: ignore[no-untyped-def]
    settings.voice_agent_provider = "livekit"
    h = auth("admin")
    cid = fresh_contact()
    r = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls", headers={**h, "Idempotency-Key": new_key()}
    )
    assert r.status_code == 503 and "not approved" in r.json()["error"]
    settings.voice_live_calls_approved = True
    r = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls", headers={**h, "Idempotency-Key": new_key()}
    )
    assert r.status_code == 403 and "allowlist" in r.json()["error"]


def test_paused_campaign_blocks_calls(client, auth, fresh_contact) -> None:  # type: ignore[no-untyped-def]
    h = auth("admin")
    cid = fresh_contact()
    camp = client.get("/api/voice-enrollment/dashboard", headers=h).json()["campaigns"][0]["id"]
    assert (
        client.post(
            f"/api/voice-enrollment/campaigns/{camp}/status", headers=h, json={"status": "paused"}
        ).status_code
        == 200
    )
    try:
        r = client.post(
            f"/api/voice-enrollment/contacts/{cid}/calls",
            headers={**h, "Idempotency-Key": new_key()},
        )
        assert r.status_code == 409
    finally:
        client.post(
            f"/api/voice-enrollment/campaigns/{camp}/status", headers=h, json={"status": "active"}
        )


def test_max_attempts_and_retry_after_unreachable(client, auth, fresh_contact, run_worker) -> None:  # type: ignore[no-untyped-def]
    h = auth("admin")
    cid = fresh_contact()
    for _ in range(3):
        _call(client, h, cid, "no_answer")
        run_worker()
    r = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls", headers={**h, "Idempotency-Key": new_key()}
    )
    assert r.status_code == 409 and "max attempts" in r.json()["error"]


def test_handoff_resolution_and_override(client, auth, fresh_contact, run_worker) -> None:  # type: ignore[no-untyped-def]
    h = auth("admin")
    cid = fresh_contact()
    _call(client, h, cid, "interested")
    run_worker()
    d = client.get("/api/voice-enrollment/dashboard", headers=h).json()
    hid = next(x["id"] for x in d["handoffs"] if x["contact_id"] == cid)
    assert (
        client.post(
            f"/api/voice-enrollment/handoffs/{hid}",
            headers=auth("cta"),
            json={"status": "completed"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/voice-enrollment/handoffs/{hid}", headers=h, json={"status": "completed"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/voice-enrollment/contacts/{cid}/disposition",
            headers=h,
            json={"status": "scheduled", "reason": ""},
        ).status_code
        == 422
    )
    r = client.post(
        f"/api/voice-enrollment/contacts/{cid}/disposition",
        headers=h,
        json={"status": "scheduled", "reason": "booked by coordinator"},
    )
    assert r.status_code == 200 and r.json()["status"] == "scheduled"
