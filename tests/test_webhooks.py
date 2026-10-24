import json
import time
import uuid

from conftest import new_key

from agros_voice import webhooks

SECRET = "test-webhook-secret-0123456789abcdef"


def _post(client, body: bytes, headers=None, provider="fixture"):  # type: ignore[no-untyped-def]
    h = webhooks.sign(SECRET, body) if headers is None else headers
    return client.post(
        f"/api/voice-enrollment/webhooks/{provider}",
        content=body,
        headers={**h, "Content-Type": "application/json"},
    )


def _ev(call_id: str, etype: str, payload=None) -> bytes:  # type: ignore[no-untyped-def]
    return json.dumps(
        {
            "event_id": str(uuid.uuid4()),
            "type": etype,
            "provider_call_id": call_id,
            "occurred_at": "2026-10-24T00:00:00+00:00",
            "payload": payload or {},
        }
    ).encode()


def test_missing_signature_rejected(client) -> None:  # type: ignore[no-untyped-def]
    assert _post(client, _ev("fx-1", "call.started"), headers={}).status_code == 401


def test_bad_signature_rejected(client) -> None:  # type: ignore[no-untyped-def]
    body = _ev("fx-1", "call.started")
    h = webhooks.sign("wrong-secret", body)
    assert _post(client, body, headers=h).status_code == 401


def test_tampered_body_rejected(client) -> None:  # type: ignore[no-untyped-def]
    body = _ev("fx-1", "call.started")
    h = webhooks.sign(SECRET, body)
    assert (
        _post(client, body.replace(b"call.started", b"call.ended  "), headers=h).status_code == 401
    )


def test_stale_timestamp_rejected(client) -> None:  # type: ignore[no-untyped-def]
    body = _ev("fx-1", "call.started")
    h = webhooks.sign(SECRET, body, timestamp=int(time.time()) - 3600)
    assert _post(client, body, headers=h).status_code == 401


def test_unknown_provider_404(client) -> None:  # type: ignore[no-untyped-def]
    assert _post(client, _ev("x", "call.started"), provider="vapi").status_code == 404


def test_uncorrelated_event_quarantined(client) -> None:  # type: ignore[no-untyped-def]
    r = _post(client, _ev("fx-does-not-exist", "call.ended", {"reason": "completed"}))
    assert r.status_code == 200 and r.json()["status"] == "quarantined"


def test_malformed_event_rejected(client) -> None:  # type: ignore[no-untyped-def]
    assert _post(client, b'{"type": "call.started"}').status_code == 422


def _calling_attempt(client, auth, fresh_contact, persona="interested"):  # type: ignore[no-untyped-def]
    from agros_voice import services

    h = auth("admin")
    cid = fresh_contact()
    a = client.post(
        f"/api/voice-enrollment/contacts/{cid}/calls",
        headers={**h, "Idempotency-Key": new_key()},
        json={"persona": persona},
    ).json()
    assert services.dispatch_attempt(a["tenant_id"], a["id"]) == "dispatched"
    return h, a["id"], f"fx-{a['id']}"


def test_duplicate_and_reordered_events(client, auth, fresh_contact) -> None:  # type: ignore[no-untyped-def]
    h, aid, call = _calling_attempt(client, auth, fresh_contact)
    intents = ["yes", "yes", "yes", "yes", "yes", "evening"]
    nodes = [
        "identity_or_availability_check",
        "q_dialysis",
        "q_adult",
        "q_interest",
        "q_handoff",
        "q_window",
    ]
    turn_bodies = [
        _ev(call, "turn.completed", {"seq": i + 1, "node": n, "intent": t})
        for i, (n, t) in enumerate(zip(nodes, intents, strict=True))
    ]
    end = _ev(call, "call.ended", {"reason": "completed", "turn_count": 6, "duration_seconds": 60})
    # call.ended first, then turns out of order, with a duplicate.
    assert _post(client, end).json()["status"] == "awaiting_turns"
    order = [5, 0, 3, 1, 2]
    for i in order:
        _post(client, turn_bodies[i])
    assert _post(client, turn_bodies[0]).json()["status"] == "duplicate"
    assert (
        client.get(f"/api/voice-enrollment/attempts/{aid}", headers=h).json()["status"] == "calling"
    )
    assert _post(client, turn_bodies[4]).json()["status"] == "completed"
    a = client.get(f"/api/voice-enrollment/attempts/{aid}", headers=h).json()
    assert a["disposition"] == "scheduling_requested"
    assert a["answers"]["preferred_window"] == "evening"
    # Late events after terminal state never change it.
    late = _ev(call, "turn.completed", {"seq": 7, "node": "q_window", "intent": "no"})
    assert _post(client, late).json()["status"] == "ignored_terminal"
    assert (
        client.get(f"/api/voice-enrollment/attempts/{aid}", headers=h).json()["disposition"]
        == "scheduling_requested"
    )


def test_utterance_text_is_never_stored(client, auth, fresh_contact) -> None:  # type: ignore[no-untyped-def]
    h, aid, call = _calling_attempt(client, auth, fresh_contact)
    _post(
        client,
        _ev(
            call,
            "turn.completed",
            {
                "seq": 1,
                "node": "identity_or_availability_check",
                "intent": "yes",
                "text": "my SSN is 123-45-6789",
                "audio": "base64",
            },
        ),
    )
    a = client.get(f"/api/voice-enrollment/attempts/{aid}", headers=h).json()
    assert "123-45" not in json.dumps(a)
