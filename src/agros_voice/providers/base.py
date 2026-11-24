from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ProviderUnavailable(Exception):
    """Provider is not configured/approved. The API surfaces this as 503 with guidance."""


@dataclass(frozen=True)
class StartCallInput:
    attempt_id: str
    phone_e164: str
    script_id: str
    eligibility_flags: dict[str, Any]
    fixture_persona: str | None = None


@dataclass(frozen=True)
class StartedCall:
    provider_call_id: str


class VoiceProvider(Protocol):
    name: str

    def start_call(self, inp: StartCallInput) -> StartedCall: ...


def get_provider(name: str) -> VoiceProvider:
    if name == "fixture":
        from .fixture import FixtureProvider

        return FixtureProvider()
    if name == "livekit":
        from .livekit_sip import LiveKitSipProvider

        return LiveKitSipProvider()
    raise ProviderUnavailable(f"unknown provider {name}")
