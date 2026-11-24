from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
from uuid import UUID

from .db import Conn

ActorType = Literal["user", "system", "provider"]

_PHONE = re.compile(r"(?<![\w\-])\+?\d[\d\-\s().]{6,}\d(?![\w\-])")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_FORBIDDEN_KEYS = {"phone", "phone_e164", "email", "transcript", "text", "audio", "recording"}


def redact(value: Any) -> Any:
    """Audit payloads never carry phone numbers, emails, transcripts or audio."""
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if k.lower() in _FORBIDDEN_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _EMAIL.sub("[email]", _PHONE.sub("[phone]", value))
    return value


def mask_phone(phone: str) -> str:
    return f"***{phone[-4:]}" if len(phone) >= 4 else "***"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str | None, body: dict[str, Any]) -> str:
    return hashlib.sha256(((prev_hash or "") + _canonical(body)).encode()).hexdigest()


def append_audit_event(
    conn: Conn,
    *,
    tenant_id: UUID | str,
    actor_type: ActorType,
    actor_id: UUID | str | None,
    action: str,
    object_type: str,
    object_id: UUID | str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The only writer for audit_events. Must run inside with_tenant(tenant_id)."""
    tid = str(tenant_id)
    conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (tid,))
    last = conn.execute(
        "select seq, hash from audit_events where tenant_id = %s order by seq desc limit 1",
        (tid,),
    ).fetchone()
    seq = (last["seq"] + 1) if last else 1
    prev_hash = last["hash"] if last else None
    body = {
        "tenant_id": tid,
        "seq": seq,
        "actor_type": actor_type,
        "actor_id": str(actor_id) if actor_id else None,
        "action": action,
        "object_type": object_type,
        "object_id": str(object_id) if object_id else None,
        "payload": redact(payload or {}),
    }
    h = compute_hash(prev_hash, body)
    row = conn.execute(
        """insert into audit_events
           (tenant_id, seq, actor_type, actor_id, action, object_type, object_id, payload,
            prev_hash, hash)
           values (%(tenant_id)s, %(seq)s, %(actor_type)s, %(actor_id)s, %(action)s,
                   %(object_type)s, %(object_id)s, %(payload)s::jsonb, %(prev)s, %(hash)s)
           returning *""",
        {**body, "payload": _canonical(body["payload"]), "prev": prev_hash, "hash": h},
    ).fetchone()
    assert row is not None
    return row


def verify_chain(conn: Conn, tenant_id: UUID | str) -> tuple[bool, int, str | None]:
    rows = conn.execute(
        "select * from audit_events where tenant_id = %s order by seq", (str(tenant_id),)
    ).fetchall()
    prev: str | None = None
    for i, r in enumerate(rows, start=1):
        body = {
            "tenant_id": str(r["tenant_id"]),
            "seq": r["seq"],
            "actor_type": r["actor_type"],
            "actor_id": str(r["actor_id"]) if r["actor_id"] else None,
            "action": r["action"],
            "object_type": r["object_type"],
            "object_id": str(r["object_id"]) if r["object_id"] else None,
            "payload": r["payload"],
        }
        if r["seq"] != i or r["prev_hash"] != prev or compute_hash(prev, body) != r["hash"]:
            return False, len(rows), f"break at seq {r['seq']}"
        prev = r["hash"]
    return True, len(rows), None
