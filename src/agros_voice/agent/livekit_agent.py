"""LiveKit voice agent: Twilio PSTN (via LiveKit SIP outbound trunk) -> Deepgram STT ->
classifier (LangGraph/Bedrock or rules) -> deterministic script -> Cartesia TTS.

The agent never generates free-form speech: every utterance comes from the approved script.
No audio or transcript is persisted; only classified intents are reported via signed webhook.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from .. import classify as clf
from .. import webhooks
from ..config import get_settings
from ..script import DialogueState, Script, advance, load_script, start

log = logging.getLogger("agros_voice.agent")


class EventReporter:
    def __init__(self, provider_call_id: str) -> None:
        s = get_settings()
        self.call_id = provider_call_id
        self.secret = s.voice_webhook_secret
        self.url = s.webhook_base_url.rstrip("/") + "/api/voice-enrollment/webhooks/livekit"
        self.turns = 0
        self.client = httpx.AsyncClient(timeout=10)

    async def send(self, etype: str, payload: dict[str, Any]) -> None:
        body = json.dumps(
            {
                "event_id": str(uuid.uuid4()),
                "type": etype,
                "provider_call_id": self.call_id,
                "occurred_at": datetime.now(UTC).isoformat(),
                "payload": payload,
            }
        ).encode()
        for attempt in range(3):
            try:
                r = await self.client.post(
                    self.url,
                    content=body,
                    headers={
                        **webhooks.sign(self.secret, body),
                        "Content-Type": "application/json",
                    },
                )
                if r.status_code < 500:
                    return
            except httpx.HTTPError:
                log.warning("webhook delivery error (attempt %d)", attempt + 1)
            await asyncio.sleep(1 + attempt)


def _build_agent_class() -> Any:
    from livekit.agents import Agent, StopResponse
    from livekit.agents.llm import ChatContext, ChatMessage

    class ScriptedOutreachAgent(Agent):  # type: ignore[misc]
        def __init__(
            self,
            script: Script,
            flags: dict[str, Any],
            reporter: EventReporter,
            on_done: asyncio.Event,
        ) -> None:
            super().__init__(instructions="Scripted outreach agent. Never improvise.")
            self.script = script
            self.flags = flags
            self.reporter = reporter
            self.on_done = on_done
            self.state: DialogueState
            self.state, self.opening = start(script)

        async def on_enter(self) -> None:
            await self.session.say(self.opening, allow_interruptions=True)

        async def on_user_turn_completed(
            self, turn_ctx: ChatContext, new_message: ChatMessage
        ) -> None:
            text = new_message.text_content or ""
            if self.state.done or self.state.node is None:
                raise StopResponse()
            node_id = self.state.node
            intent = await asyncio.to_thread(
                clf.classify,
                text,
                self.script.allowed_intents(node_id),
                self.script.node(node_id)["prompt"],
            )
            step = advance(self.script, self.state, intent, self.flags)
            self.reporter.turns += 1
            await self.reporter.send(
                "turn.completed",
                {"seq": self.reporter.turns, "node": node_id, "intent": step.intent},
            )
            handle = self.session.say(step.say, allow_interruptions=not self.state.done)
            if self.state.done:
                await handle
                self.on_done.set()
            raise StopResponse()

    return ScriptedOutreachAgent


async def entrypoint(ctx: Any) -> None:
    from livekit import api, rtc
    from livekit.agents import AgentSession
    from livekit.plugins import cartesia, deepgram, silero

    s = get_settings()
    meta = json.loads(ctx.job.metadata or "{}")
    phone = meta["phone_e164"]
    if phone not in s.live_allowlist() or not s.voice_live_calls_approved:
        log.error("refusing to dial: destination not allowlisted or live calls not approved")
        return
    reporter = EventReporter(ctx.room.name)
    await ctx.connect()
    script = load_script(meta["script_id"])
    done = asyncio.Event()
    agent_cls = _build_agent_class()
    agent = agent_cls(script, meta.get("eligibility_flags") or {}, reporter, done)
    session: Any = AgentSession(
        stt=deepgram.STT(model="nova-3", language="en-US", api_key=s.deepgram_api_key or ""),
        tts=cartesia.TTS(model="sonic-2", voice=s.cartesia_voice_id, api_key=s.cartesia_api_key),
        vad=silero.VAD.load(),
    )
    started_at = datetime.now(UTC)
    try:
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=s.livekit_sip_trunk_id,
                sip_call_to=phone,
                participant_identity="participant",
                wait_until_answered=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - busy / no answer / rejected
        log.warning("dial failed: %s", type(exc).__name__)
        await reporter.send("call.failed", {"reason": "no_answer"})
        ctx.shutdown()
        return
    await reporter.send("call.started", {})
    hung_up = asyncio.Event()

    @ctx.room.on("participant_disconnected")
    def _left(p: rtc.RemoteParticipant) -> None:
        if p.identity == "participant":
            hung_up.set()

    await session.start(room=ctx.room, agent=agent)
    waiters = [asyncio.create_task(done.wait()), asyncio.create_task(hung_up.wait())]
    await asyncio.wait(
        waiters, timeout=s.voice_call_timeout_minutes * 60, return_when=asyncio.FIRST_COMPLETED
    )
    for w in waiters:
        w.cancel()
    duration = int((datetime.now(UTC) - started_at).total_seconds())
    await reporter.send(
        "call.ended",
        {
            "reason": "completed" if done.is_set() else "participant_hung_up",
            "duration_seconds": duration,
            "turn_count": reporter.turns,
        },
    )
    await asyncio.sleep(1)
    try:
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
    except Exception:  # noqa: BLE001
        log.info("room already closed")
    ctx.shutdown()


def run(argv: list[str]) -> None:
    import sys

    from livekit.agents import WorkerOptions, cli

    s = get_settings()
    sys.argv = ["agros-voice-agent", *argv]
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=s.livekit_agent_name,
            ws_url=s.livekit_url,
            api_key=s.livekit_api_key,
            api_secret=s.livekit_api_secret,
        )
    )
