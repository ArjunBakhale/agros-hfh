"""Domain operations. Every tenant-scoped statement runs inside db.with_tenant(); every
state change appends an audit event in the same transaction."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from . import jobs, webhooks
from .audit import append_audit_event, mask_phone
from .config import get_settings
from .db import Conn, unscoped, with_tenant
from .providers import ProviderUnavailable, StartCallInput, get_provider
from .script import OUTCOMES, load_script, replay

FIXTURE_RUN_QUEUE = "voice-fixture-run"
FICTIONAL_PHONE = re.compile(r"^\+1[2-9]\d{2}5550[1][0-9]{2}$")  # NANP 555-0100..0199
E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
TERMINAL_ATTEMPT = {"completed", "failed", "cancelled", "timed_out"}
ELIGIBILITY_FLAG_KEYS = {"adult", "on_or_preparing_for_dialysis"}


class DomainError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Actor:
    def __init__(self, tenant_id: str, user_id: str | None, role: str) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.role = role


def _uuid(value: str, what: str) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError as exc:
        raise DomainError(404, f"{what} not found") from exc


# ---------------------------------------------------------------- campaigns & contacts


def create_campaign(
    actor: Actor, name: str, script_id: str, max_attempts: int = 1
) -> dict[str, Any]:
    load_script(script_id)
    name = name.strip()
    if not name or len(name) > 200:
        raise DomainError(422, "campaign name required")
    with with_tenant(actor.tenant_id) as conn:
        row = conn.execute(
            """insert into voice_campaigns (tenant_id, name, script_id, max_attempts, created_by, status)
               values (%s,%s,%s,%s,%s,'active') returning *""",
            (actor.tenant_id, name, script_id, max_attempts, actor.user_id),
        ).fetchone()
        assert row
        append_audit_event(
            conn,
            tenant_id=actor.tenant_id,
            actor_type="user",
            actor_id=actor.user_id,
            action="voice_campaign.created",
            object_type="voice_campaign",
            object_id=row["id"],
            payload={"script_id": script_id, "max_attempts": max_attempts},
        )
        return dict(row)


def set_campaign_status(actor: Actor, campaign_id: str, status: str) -> dict[str, Any]:
    if status not in ("active", "paused", "completed", "archived"):
        raise DomainError(422, "invalid status")
    cid = _uuid(campaign_id, "campaign")
    with with_tenant(actor.tenant_id) as conn:
        before = conn.execute(
            "select status from voice_campaigns where id=%s for update", (cid,)
        ).fetchone()
        if not before:
            raise DomainError(404, "campaign not found")
        row = conn.execute(
            "update voice_campaigns set status=%s where id=%s returning *", (status, cid)
        ).fetchone()
        append_audit_event(
            conn,
            tenant_id=actor.tenant_id,
            actor_type="user",
            actor_id=actor.user_id,
            action="voice_campaign.status_changed",
            object_type="voice_campaign",
            object_id=cid,
            payload={"from": before["status"], "to": status},
        )
        return dict(row or {})


def parse_contacts_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate + dedupe a contact CSV. Required columns: contact_token, phone_e164.
    Optional: language, age_band, adult, on_or_preparing_for_dialysis, is_fixture."""
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.strip() for f in (reader.fieldnames or [])}
    if not {"contact_token", "phone_e164"} <= fields:
        return [], ["CSV must include contact_token and phone_e164 columns"]
    forbidden = fields & {
        "name",
        "first_name",
        "last_name",
        "dob",
        "mrn",
        "email",
        "diagnosis",
        "notes",
    }
    if forbidden:
        return [], [f"CSV contains columns outside the contact-layer boundary: {sorted(forbidden)}"]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_tokens: set[str] = set()
    seen_phones: set[str] = set()
    for i, raw in enumerate(reader, start=2):
        r = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        token, phone = r.get("contact_token", ""), r.get("phone_e164", "")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{3,64}", token):
            errors.append(f"line {i}: invalid contact_token")
            continue
        if not E164.fullmatch(phone):
            errors.append(f"line {i}: phone must be E.164")
            continue
        if token in seen_tokens or phone in seen_phones:
            errors.append(f"line {i}: duplicate contact_token or phone (skipped)")
            continue
        seen_tokens.add(token)
        seen_phones.add(phone)
        flags: dict[str, Any] = {}
        for key in ELIGIBILITY_FLAG_KEYS:
            if r.get(key):
                flags[key] = r[key].lower() in ("1", "true", "yes", "y")
        rows.append(
            {
                "contact_token": token,
                "phone_e164": phone,
                "language": (r.get("language") or "en")[:8],
                "age_band": r.get("age_band") or None,
                "eligibility_flags": flags,
                "is_fixture": (r.get("is_fixture") or "true").lower() in ("1", "true", "yes", "y"),
            }
        )
    return rows, errors


def import_contacts(actor: Actor, campaign_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    s = get_settings()
    cid = _uuid(campaign_id, "campaign")
    allow = s.live_allowlist()
    for r in rows:
        if not s.voice_real_contacts_approved:
            if not r["is_fixture"]:
                raise DomainError(
                    403, "real contacts are disabled (VOICE_REAL_CONTACTS_APPROVED=false)"
                )
            if not (FICTIONAL_PHONE.fullmatch(r["phone_e164"]) or r["phone_e164"] in allow):
                raise DomainError(
                    403,
                    "fixture contacts must use fictional 555-01xx numbers or an allowlisted test number",
                )
    with with_tenant(actor.tenant_id) as conn:
        if not conn.execute("select 1 from voice_campaigns where id=%s", (cid,)).fetchone():
            raise DomainError(404, "campaign not found")
        inserted = 0
        for r in rows:
            res = conn.execute(
                """insert into voice_contacts (tenant_id, campaign_id, contact_token, phone_e164,
                     language, age_band, eligibility_flags, is_fixture)
                   values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                   on conflict (tenant_id, campaign_id, contact_token) do nothing returning id""",
                (
                    actor.tenant_id,
                    cid,
                    r["contact_token"],
                    r["phone_e164"],
                    r["language"],
                    r["age_band"],
                    json.dumps(r["eligibility_flags"]),
                    r["is_fixture"],
                ),
            ).fetchone()
            inserted += 1 if res else 0
        append_audit_event(
            conn,
            tenant_id=actor.tenant_id,
            actor_type="user",
            actor_id=actor.user_id,
            action="voice_contacts.imported",
            object_type="voice_campaign",
            object_id=cid,
            payload={
                "received": len(rows),
                "inserted": inserted,
                "duplicates": len(rows) - inserted,
            },
        )
    return {"received": len(rows), "inserted": inserted, "duplicates": len(rows) - inserted}


# ---------------------------------------------------------------- calls


def _within_window(campaign: dict[str, Any]) -> bool:
    now = datetime.now(ZoneInfo(campaign["timezone"])).time()
    return bool(campaign["call_window_start"] <= now <= campaign["call_window_end"])


def request_call(
    actor: Actor, contact_id: str, idempotency_key: str, persona: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Queue one call attempt. Returns (attempt, created). Same key => same attempt."""
    s = get_settings()
    if not s.voice_agent_enabled:
        raise DomainError(503, "voice agent is disabled (VOICE_AGENT_ENABLED=false)")
    if not re.fullmatch(r"[A-Za-z0-9_\-:.]{8,128}", idempotency_key or ""):
        raise DomainError(422, "Idempotency-Key header (8-128 safe chars) is required")
    ctid = _uuid(contact_id, "contact")
    provider = s.voice_agent_provider
    with with_tenant(actor.tenant_id) as conn:
        existing = conn.execute(
            "select * from voice_call_attempts where idempotency_key=%s", (idempotency_key,)
        ).fetchone()
        if existing:
            if str(existing["contact_id"]) != ctid:
                raise DomainError(409, "Idempotency-Key reused for a different contact")
            return dict(existing), False
        contact = conn.execute(
            "select * from voice_contacts where id=%s for update", (ctid,)
        ).fetchone()
        if not contact:
            raise DomainError(404, "contact not found")
        campaign = conn.execute(
            "select * from voice_campaigns where id=%s", (contact["campaign_id"],)
        ).fetchone()
        assert campaign
        if campaign["status"] != "active":
            raise DomainError(409, f"campaign is {campaign['status']}")
        if contact["do_not_call"]:
            raise DomainError(409, "contact is do-not-call")
        if contact["status"] in ("queued", "calling"):
            raise DomainError(409, "a call is already in progress for this contact")
        if contact["attempt_count"] >= campaign["max_attempts"]:
            raise DomainError(409, "max attempts reached")
        if provider == "fixture":
            if not contact["is_fixture"]:
                raise DomainError(409, "fixture provider can only call fixture contacts")
            persona = persona or "interested"
        else:
            persona = None
            if not s.voice_live_calls_approved:
                raise DomainError(
                    503, "live calls are not approved (VOICE_LIVE_CALLS_APPROVED=false)"
                )
            if contact["phone_e164"] not in s.live_allowlist():
                raise DomainError(403, "destination is not on the live-call allowlist")
            if s.voice_enforce_call_window and not _within_window(campaign):
                raise DomainError(409, "outside the campaign call window")
        from .providers.fixture import PERSONAS

        if persona is not None and persona not in PERSONAS:
            raise DomainError(422, "unknown fixture persona")
        script = load_script(campaign["script_id"])
        attempt = conn.execute(
            """insert into voice_call_attempts (tenant_id, campaign_id, contact_id, script_id,
                 script_version, provider, fixture_persona, requested_by, idempotency_key)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
            (
                actor.tenant_id,
                campaign["id"],
                ctid,
                script.id,
                script.version,
                provider,
                persona,
                actor.user_id,
                idempotency_key,
            ),
        ).fetchone()
        assert attempt
        conn.execute(
            """update voice_contacts set status='queued', attempt_count=attempt_count+1,
                 last_attempt_at=now() where id=%s""",
            (ctid,),
        )
        append_audit_event(
            conn,
            tenant_id=actor.tenant_id,
            actor_type="user",
            actor_id=actor.user_id,
            action="voice_call.requested",
            object_type="voice_call_attempt",
            object_id=attempt["id"],
            payload={
                "contact_id": ctid,
                "provider": provider,
                "persona": persona,
                "script_id": script.id,
                "script_version": script.version,
                "phone_last4": contact["phone_e164"][-4:],
            },
        )
        jobs.enqueue(
            conn,
            jobs.DISPATCH_QUEUE,
            actor.tenant_id,
            {"attempt_id": str(attempt["id"])},
            singleton_key=f"dispatch:{attempt['id']}",
        )
        return dict(attempt), True


def _fail_attempt(
    conn: Conn, tenant_id: str, attempt: dict[str, Any], status: str, reason: str
) -> None:
    conn.execute(
        """update voice_call_attempts set status=%s, dialogue_state='failed', failure_reason=%s,
             disposition=coalesce(disposition, %s), ended_at=now() where id=%s""",
        (status, reason, "unreachable" if status == "failed" else "needs_human", attempt["id"]),
    )
    contact_status = "unreachable" if status == "failed" else "needs_human"
    conn.execute(
        "update voice_contacts set status=%s where id=%s", (contact_status, attempt["contact_id"])
    )
    if contact_status == "needs_human":
        _open_handoff(conn, tenant_id, attempt, "human_review", f"call {status}: {reason}", None)
    append_audit_event(
        conn,
        tenant_id=tenant_id,
        actor_type="system",
        actor_id=None,
        action=f"voice_call.{status}",
        object_type="voice_call_attempt",
        object_id=attempt["id"],
        payload={"reason": reason},
    )


def dispatch_attempt(tenant_id: str, attempt_id: str) -> str:
    """Worker: hand a queued attempt to the provider. Raises to trigger job retry."""
    s = get_settings()
    with with_tenant(tenant_id) as conn:
        attempt = conn.execute(
            "select * from voice_call_attempts where id=%s for update", (attempt_id,)
        ).fetchone()
        if not attempt or attempt["status"] != "queued":
            return "skipped"
        contact = conn.execute(
            "select * from voice_contacts where id=%s", (attempt["contact_id"],)
        ).fetchone()
        assert contact
        try:
            started = get_provider(attempt["provider"]).start_call(
                StartCallInput(
                    attempt_id=str(attempt["id"]),
                    phone_e164=contact["phone_e164"],
                    script_id=attempt["script_id"],
                    eligibility_flags=contact["eligibility_flags"],
                    fixture_persona=attempt["fixture_persona"],
                )
            )
        except ProviderUnavailable as exc:
            _fail_attempt(conn, tenant_id, dict(attempt), "failed", f"provider_unavailable: {exc}")
            return "failed"
        conn.execute(
            """update voice_call_attempts set status='calling', provider_call_id=%s,
                 dialogue_state='calling', started_at=now() where id=%s""",
            (started.provider_call_id, attempt["id"]),
        )
        conn.execute(
            "update voice_contacts set status='calling' where id=%s", (attempt["contact_id"],)
        )
        append_audit_event(
            conn,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id=None,
            action="voice_call.dispatched",
            object_type="voice_call_attempt",
            object_id=attempt["id"],
            payload={"provider": attempt["provider"], "provider_call_id": started.provider_call_id},
        )
        if attempt["provider"] == "fixture":
            jobs.enqueue(
                conn,
                FIXTURE_RUN_QUEUE,
                tenant_id,
                {"attempt_id": str(attempt["id"]), "provider_call_id": started.provider_call_id},
                singleton_key=f"fixture:{attempt['id']}",
            )
    _ = s
    return "dispatched"


def run_fixture_call(tenant_id: str, attempt_id: str, post: Any) -> int:
    """Worker: simulate the fixture conversation and deliver it as signed webhooks.
    `post(path, body, headers) -> status_code` is HTTP in production, in-process in tests."""
    from .providers.fixture import simulate

    s = get_settings()
    with with_tenant(tenant_id) as conn:
        attempt = conn.execute(
            "select * from voice_call_attempts where id=%s", (attempt_id,)
        ).fetchone()
        if not attempt or attempt["status"] != "calling":
            return 0
        contact = conn.execute(
            "select eligibility_flags from voice_contacts where id=%s", (attempt["contact_id"],)
        ).fetchone()
        assert contact
    events = simulate(
        attempt["provider_call_id"],
        attempt["script_id"],
        attempt["fixture_persona"] or "interested",
        contact["eligibility_flags"],
    )
    for ev in events:
        body = json.dumps(ev).encode()
        status = post(
            "/api/voice-enrollment/webhooks/fixture",
            body,
            webhooks.sign(s.voice_webhook_secret, body),
        )
        if status >= 300:
            raise RuntimeError(f"webhook delivery failed with {status}")
    return len(events)


# ---------------------------------------------------------------- webhooks


def _quarantine(provider: str, provider_call_id: str | None, reason: str, body: bytes) -> None:
    with unscoped() as conn:
        conn.execute(
            """insert into voice_webhook_quarantine (provider, provider_call_id, reason, payload_sha256)
               values (%s,%s,%s,%s)""",
            (
                provider,
                (provider_call_id or "")[:200] or None,
                reason,
                hashlib.sha256(body).hexdigest(),
            ),
        )


def ingest_webhook(
    provider: str, body: bytes, signature: str | None, timestamp: str | None
) -> dict[str, Any]:
    s = get_settings()
    if provider not in ("fixture", "livekit"):
        raise DomainError(404, "unknown provider")
    try:
        webhooks.verify(
            s.voice_webhook_secret, body, signature, timestamp, s.voice_webhook_tolerance_seconds
        )
    except webhooks.SignatureError as exc:
        raise DomainError(401, "invalid webhook signature") from exc
    try:
        ev = json.loads(body)
        event_id = str(ev["event_id"])
        etype = str(ev["type"])
        call_id = str(ev["provider_call_id"])
        occurred_at = datetime.fromisoformat(str(ev["occurred_at"]))
        payload = dict(ev.get("payload") or {})
    except (ValueError, KeyError, TypeError) as exc:
        _quarantine(provider, None, "malformed", body)
        raise DomainError(422, "malformed event") from exc
    if etype not in ("call.started", "turn.completed", "call.ended", "call.failed"):
        _quarantine(provider, call_id, "unknown_event_type", body)
        return {"status": "quarantined"}
    with unscoped() as conn:
        matches = conn.execute(
            "select * from resolve_call_attempt(%s,%s)", (provider, call_id)
        ).fetchall()
    if len(matches) != 1:
        _quarantine(provider, call_id, "uncorrelated" if not matches else "ambiguous", body)
        return {"status": "quarantined"}
    tenant_id, attempt_id = str(matches[0]["tenant_id"]), str(matches[0]["attempt_id"])
    safe_payload = _event_payload(etype, payload)
    with with_tenant(tenant_id) as conn:
        attempt = conn.execute(
            "select * from voice_call_attempts where id=%s for update", (attempt_id,)
        ).fetchone()
        assert attempt
        inserted = conn.execute(
            """insert into voice_call_events (tenant_id, call_attempt_id, provider_event_id, event_type,
                 seq, occurred_at, payload)
               values (%s,%s,%s,%s,%s,%s,%s::jsonb)
               on conflict (tenant_id, call_attempt_id, provider_event_id) do nothing returning id""",
            (
                tenant_id,
                attempt_id,
                event_id,
                etype,
                safe_payload.get("seq"),
                occurred_at,
                json.dumps(safe_payload),
            ),
        ).fetchone()
        if not inserted:
            return {"status": "duplicate", "attempt_id": attempt_id}
        if attempt["status"] in TERMINAL_ATTEMPT:
            return {"status": "ignored_terminal", "attempt_id": attempt_id}
        result = _reconcile(conn, tenant_id, dict(attempt))
        return {"status": result, "attempt_id": attempt_id}


def _event_payload(etype: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Whitelist fields; utterance text/audio never persist."""
    if etype == "turn.completed":
        seq = payload.get("seq")
        if not isinstance(seq, int) or seq < 1 or seq > 200:
            raise DomainError(422, "turn.completed requires integer seq")
        return {
            "seq": seq,
            "node": str(payload.get("node", ""))[:64],
            "intent": str(payload.get("intent", ""))[:64],
        }
    if etype == "call.ended":
        out: dict[str, Any] = {"reason": str(payload.get("reason", ""))[:64]}
        for k in ("duration_seconds", "turn_count"):
            if isinstance(payload.get(k), int):
                out[k] = payload[k]
        return out
    if etype == "call.failed":
        return {"reason": str(payload.get("reason", "unknown"))[:64]}
    return {}


def _reconcile(conn: Conn, tenant_id: str, attempt: dict[str, Any]) -> str:
    """Recompute dialogue from all stored events (order-independent, replay-safe)."""
    events = conn.execute(
        "select event_type, seq, payload from voice_call_events where call_attempt_id=%s",
        (attempt["id"],),
    ).fetchall()
    by_type: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_type.setdefault(e["event_type"], []).append(e)
    if "call.failed" in by_type:
        _fail_attempt(
            conn, tenant_id, attempt, "failed", by_type["call.failed"][0]["payload"]["reason"]
        )
        return "failed"
    turns = sorted(by_type.get("turn.completed", []), key=lambda e: e["seq"])
    contact = conn.execute(
        "select * from voice_contacts where id=%s", (attempt["contact_id"],)
    ).fetchone()
    assert contact
    script = load_script(attempt["script_id"])
    state = replay(script, [t["payload"]["intent"] for t in turns], contact["eligibility_flags"])
    seqs = [t["seq"] for t in turns]
    contiguous = seqs == list(range(1, len(seqs) + 1))
    conn.execute(
        "update voice_call_attempts set dialogue_state=%s, answers=%s::jsonb where id=%s",
        (state.phase, json.dumps(state.answers), attempt["id"]),
    )
    ended = by_type.get("call.ended")
    if not ended:
        return "applied"
    end_payload = ended[0]["payload"]
    expected_turns = end_payload.get("turn_count", len(turns))
    if not contiguous or len(turns) < expected_turns:
        return "awaiting_turns"
    outcome = state.outcome or "no_response"
    _finalize(
        conn,
        tenant_id,
        attempt,
        contact,
        state.to_json(),
        outcome,
        state.handoff,
        end_payload.get("duration_seconds"),
    )
    return "completed"


def _open_handoff(
    conn: Conn, tenant_id: str, attempt: dict[str, Any], kind: str, reason: str, window: str | None
) -> None:
    row = conn.execute(
        """insert into voice_handoffs (tenant_id, contact_id, call_attempt_id, kind, reason, preferred_window)
           values (%s,%s,%s,%s,%s,%s) on conflict (tenant_id, call_attempt_id, kind) do nothing returning id""",
        (tenant_id, attempt["contact_id"], attempt["id"], kind, reason, window),
    ).fetchone()
    if row:
        append_audit_event(
            conn,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id=None,
            action="voice_handoff.opened",
            object_type="voice_handoff",
            object_id=row["id"],
            payload={"kind": kind, "reason": reason},
        )


def _finalize(
    conn: Conn,
    tenant_id: str,
    attempt: dict[str, Any],
    contact: dict[str, Any],
    state: dict[str, Any],
    outcome: str,
    handoff: str | None,
    duration: int | None,
) -> None:
    phase, contact_status = OUTCOMES[outcome]
    attempt_status = "failed" if phase == "failed" else "completed"
    conn.execute(
        """update voice_call_attempts set status=%s, dialogue_state=%s, disposition=%s, answers=%s::jsonb,
             ended_at=now(), duration_seconds=%s where id=%s""",
        (attempt_status, phase, outcome, json.dumps(state["answers"]), duration, attempt["id"]),
    )
    do_not_call = outcome in ("opted_out", "wrong_number")
    conn.execute(
        """update voice_contacts set status=%s, final_disposition=%s, do_not_call = do_not_call or %s
           where id=%s""",
        (contact_status, outcome, do_not_call, contact["id"]),
    )
    window = state["answers"].get("preferred_window")
    if handoff and outcome == "scheduling_requested":
        _open_handoff(
            conn, tenant_id, attempt, handoff, "participant requested coordinator follow-up", window
        )
    if phase == "needs_human":
        _open_handoff(conn, tenant_id, attempt, "human_review", outcome, None)
    append_audit_event(
        conn,
        tenant_id=tenant_id,
        actor_type="provider",
        actor_id=None,
        action="voice_call.completed",
        object_type="voice_call_attempt",
        object_id=attempt["id"],
        payload={
            "disposition": outcome,
            "dialogue_state": phase,
            "answers": state["answers"],
            "turns": state["turns"],
            "do_not_call": do_not_call,
        },
    )


def sweep_timeouts() -> int:
    s = get_settings()
    with unscoped() as conn:
        stuck = conn.execute(
            "select * from sweep_stuck_attempts(make_interval(mins => %s))",
            (s.voice_call_timeout_minutes,),
        ).fetchall()
    n = 0
    for row in stuck:
        with with_tenant(row["tenant_id"]) as conn:
            attempt = conn.execute(
                "select * from voice_call_attempts where id=%s for update", (row["attempt_id"],)
            ).fetchone()
            if attempt and attempt["status"] in ("queued", "calling"):
                _fail_attempt(
                    conn, str(row["tenant_id"]), dict(attempt), "timed_out", "no terminal event"
                )
                n += 1
    return n


# ---------------------------------------------------------------- coordinator actions


DISPOSITIONS = {
    "scheduling_requested",
    "scheduled",
    "declined",
    "unreachable",
    "needs_human",
    "closed",
    "interested",
    "screened",
}


def override_disposition(actor: Actor, contact_id: str, status: str, reason: str) -> dict[str, Any]:
    if status not in DISPOSITIONS:
        raise DomainError(422, "invalid disposition")
    reason = reason.strip()
    if not 3 <= len(reason) <= 500:
        raise DomainError(422, "a reason (3-500 chars) is required")
    ctid = _uuid(contact_id, "contact")
    with with_tenant(actor.tenant_id) as conn:
        before = conn.execute(
            "select status from voice_contacts where id=%s for update", (ctid,)
        ).fetchone()
        if not before:
            raise DomainError(404, "contact not found")
        row = conn.execute(
            "update voice_contacts set status=%s, final_disposition=%s where id=%s returning *",
            (status, status, ctid),
        ).fetchone()
        append_audit_event(
            conn,
            tenant_id=actor.tenant_id,
            actor_type="user",
            actor_id=actor.user_id,
            action="voice_contact.disposition_overridden",
            object_type="voice_contact",
            object_id=ctid,
            payload={"from": before["status"], "to": status, "reason": reason},
        )
        return dict(row or {})


def resolve_handoff(actor: Actor, handoff_id: str, status: str) -> dict[str, Any]:
    if status not in ("in_progress", "completed", "cancelled"):
        raise DomainError(422, "invalid status")
    hid = _uuid(handoff_id, "handoff")
    with with_tenant(actor.tenant_id) as conn:
        before = conn.execute(
            "select status from voice_handoffs where id=%s for update", (hid,)
        ).fetchone()
        if not before:
            raise DomainError(404, "handoff not found")
        if before["status"] in ("completed", "cancelled"):
            raise DomainError(409, "handoff already closed")
        closing = status in ("completed", "cancelled")
        row = conn.execute(
            """update voice_handoffs set status=%s, resolved_by=%s, resolved_at=%s where id=%s returning *""",
            (
                status,
                actor.user_id if closing else None,
                datetime.now(UTC) if closing else None,
                hid,
            ),
        ).fetchone()
        append_audit_event(
            conn,
            tenant_id=actor.tenant_id,
            actor_type="user",
            actor_id=actor.user_id,
            action="voice_handoff.status_changed",
            object_type="voice_handoff",
            object_id=hid,
            payload={"from": before["status"], "to": status},
        )
        return dict(row or {})


# ---------------------------------------------------------------- reads


def dashboard(actor: Actor) -> dict[str, Any]:
    with with_tenant(actor.tenant_id) as conn:
        tenant = conn.execute(
            "select id, name from tenants where id=%s", (actor.tenant_id,)
        ).fetchone()
        campaigns = conn.execute("select * from voice_campaigns order by created_at").fetchall()
        contacts = conn.execute(
            """select c.*, a.id as last_attempt_id, a.status as last_attempt_status, a.disposition,
                      a.dialogue_state, a.answers, a.failure_reason, a.provider, a.fixture_persona
                 from voice_contacts c
                 left join lateral (select * from voice_call_attempts a where a.contact_id=c.id
                                    order by a.created_at desc limit 1) a on true
                order by c.created_at, c.contact_token"""
        ).fetchall()
        counts = conn.execute(
            "select status, count(*) n from voice_contacts group by status"
        ).fetchall()
        handoffs = conn.execute(
            """select h.*, c.contact_token from voice_handoffs h join voice_contacts c on c.id=h.contact_id
                where h.status in ('open','in_progress') order by h.created_at"""
        ).fetchall()
        failed = conn.execute(
            """select a.*, c.contact_token from voice_call_attempts a join voice_contacts c on c.id=a.contact_id
                where a.status in ('failed','timed_out') order by a.created_at desc limit 50"""
        ).fetchall()
        audit = conn.execute(
            "select seq, actor_type, action, object_type, created_at from audit_events order by seq desc limit 25"
        ).fetchall()
    for c in contacts:
        c["phone_masked"] = mask_phone(c.pop("phone_e164"))
    return {
        "tenant": tenant,
        "campaigns": campaigns,
        "contacts": contacts,
        "counts": {r["status"]: r["n"] for r in counts},
        "handoffs": handoffs,
        "failed": failed,
        "audit": audit,
    }


def get_attempt(actor: Actor, attempt_id: str) -> dict[str, Any]:
    aid = _uuid(attempt_id, "attempt")
    with with_tenant(actor.tenant_id) as conn:
        row = conn.execute("select * from voice_call_attempts where id=%s", (aid,)).fetchone()
        if not row:
            raise DomainError(404, "attempt not found")
        row["events"] = conn.execute(
            "select event_type, seq, occurred_at, payload from voice_call_events where call_attempt_id=%s "
            "order by occurred_at, seq nulls first",
            (aid,),
        ).fetchall()
        return dict(row)


def export_outcomes_csv(actor: Actor, campaign_id: str | None = None) -> str:
    """Operational outcomes only: no phone numbers, transcripts or free text."""
    with with_tenant(actor.tenant_id) as conn:
        q = """select c.contact_token, c.status, c.final_disposition, c.attempt_count, c.do_not_call,
                      a.answers, a.dialogue_state, a.ended_at
                 from voice_contacts c
                 left join lateral (select * from voice_call_attempts a where a.contact_id=c.id
                                    order by a.created_at desc limit 1) a on true"""
        params: tuple[Any, ...] = ()
        if campaign_id:
            q += " where c.campaign_id=%s"
            params = (_uuid(campaign_id, "campaign"),)
        rows = conn.execute(q + " order by c.contact_token", params).fetchall()
        append_audit_event(
            conn,
            tenant_id=actor.tenant_id,
            actor_type="user",
            actor_id=actor.user_id,
            action="voice_outcomes.exported",
            object_type="voice_campaign",
            object_id=campaign_id and _uuid(campaign_id, "campaign"),
            payload={"rows": len(rows)},
        )
    out = io.StringIO()
    w = csv.writer(out)
    cols = [
        "contact_token",
        "status",
        "final_disposition",
        "attempt_count",
        "do_not_call",
        "dialogue_state",
        "dialysis_status",
        "adult",
        "interest",
        "coordinator_callback_ok",
        "preferred_window",
        "ended_at",
    ]
    w.writerow(cols)
    for r in rows:
        a = r["answers"] or {}
        w.writerow(
            [
                _csv_safe(v)
                for v in [
                    r["contact_token"],
                    r["status"],
                    r["final_disposition"],
                    r["attempt_count"],
                    r["do_not_call"],
                    r["dialogue_state"],
                    a.get("dialysis_status"),
                    a.get("adult"),
                    a.get("interest"),
                    a.get("coordinator_callback_ok"),
                    a.get("preferred_window"),
                    r["ended_at"].isoformat() if r["ended_at"] else "",
                ]
            ]
        )
    return out.getvalue()


def _csv_safe(v: Any) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s
