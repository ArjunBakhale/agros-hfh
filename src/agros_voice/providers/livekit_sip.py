"""Live provider: LiveKit Cloud room + explicit agent dispatch; the agent dials out over a
LiveKit SIP outbound trunk (Twilio Elastic SIP) and reports signed events back."""

from __future__ import annotations

import asyncio
import json

from ..config import get_settings
from .base import ProviderUnavailable, StartCallInput, StartedCall


def room_name_for(attempt_id: str) -> str:
    return f"agros-call-{attempt_id}"


class LiveKitSipProvider:
    name = "livekit"

    def start_call(self, inp: StartCallInput) -> StartedCall:
        s = get_settings()
        if not s.livekit_configured():
            raise ProviderUnavailable("LiveKit is not configured (LIVEKIT_* and SIP trunk id)")
        if not s.voice_live_calls_approved:
            raise ProviderUnavailable("VOICE_LIVE_CALLS_APPROVED is false")
        if inp.phone_e164 not in s.live_allowlist():
            raise ProviderUnavailable("destination is not on VOICE_LIVE_CALL_ALLOWLIST")
        room = room_name_for(inp.attempt_id)
        metadata = json.dumps(
            {
                "attempt_id": inp.attempt_id,
                "phone_e164": inp.phone_e164,
                "script_id": inp.script_id,
                "eligibility_flags": inp.eligibility_flags,
            }
        )
        asyncio.run(self._dispatch(room, metadata))
        return StartedCall(provider_call_id=room)

    async def _dispatch(self, room: str, metadata: str) -> None:
        from livekit import api

        s = get_settings()
        lk = api.LiveKitAPI(s.livekit_url, s.livekit_api_key, s.livekit_api_secret)
        try:
            await lk.room.create_room(
                api.CreateRoomRequest(name=room, empty_timeout=120, max_participants=3)
            )
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=s.livekit_agent_name, room=room, metadata=metadata
                )
            )
        finally:
            await lk.aclose()
