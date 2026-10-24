"""HMAC-SHA256 signed provider/agent webhooks with a replay window."""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Agros-Signature"
TIMESTAMP_HEADER = "X-Agros-Timestamp"


class SignatureError(Exception):
    pass


def sign(secret: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {SIGNATURE_HEADER: f"v1={mac}", TIMESTAMP_HEADER: ts}


def verify(
    secret: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> None:
    if not signature or not timestamp or not signature.startswith("v1="):
        raise SignatureError("missing signature")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("bad timestamp") from exc
    if abs((now if now is not None else time.time()) - ts) > tolerance_seconds:
        raise SignatureError("stale timestamp")
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    if not hmac.compare_digest(expected.hexdigest(), signature[3:]):
        raise SignatureError("bad signature")
