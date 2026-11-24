"""Deterministic, network-free voice provider for tests and the M0 demo.

A persona is a scripted list of participant utterances. The simulation runs the same
classifier + script engine as the live agent and emits the same signed webhook events.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .. import classify as clf
from ..script import advance, load_script, start
from .base import StartCallInput, StartedCall

PERSONAS: dict[str, list[str] | None] = {
    "interested": [
        "Yes, now is fine.",
        "Yes, I'm on dialysis three times a week.",
        "Yes I am.",
        "Sure, I'd like to learn more.",
        "Yes, that's fine.",
        "Afternoon works best.",
    ],
    "declined": ["Yeah, okay.", "Yes.", "Yes.", "No thanks, not interested."],
    "clinical_question": ["Sure.", "Should I change my potassium binder before dialysis?"],
    "not_eligible": ["Okay.", "No, I'm not on dialysis."],
    "language": ["Español, por favor."],
    "opt_out": ["Stop calling me."],
    "wrong_number": ["You have the wrong number."],
    "unclear": ["Yes.", "Hmm, the weather is nice.", "Purple."],
    "no_answer": None,
}


class FixtureProvider:
    name = "fixture"

    def start_call(self, inp: StartCallInput) -> StartedCall:
        return StartedCall(provider_call_id=f"fx-{inp.attempt_id}")


def _event(provider_call_id: str, etype: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "type": etype,
        "provider_call_id": provider_call_id,
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload": payload,
    }


def simulate(
    provider_call_id: str,
    script_id: str,
    persona: str,
    eligibility_flags: dict[str, Any],
) -> list[dict[str, Any]]:
    if persona not in PERSONAS:
        raise ValueError(f"unknown persona {persona}")
    utterances = PERSONAS[persona]
    if utterances is None:
        return [_event(provider_call_id, "call.failed", {"reason": "no_answer"})]
    script = load_script(script_id)
    state, _ = start(script)
    events = [_event(provider_call_id, "call.started", {})]
    for seq, utt in enumerate(utterances, start=1):
        if state.done or state.node is None:
            break
        node_id = state.node
        intent = clf.classify_rules(utt, script.allowed_intents(node_id))
        advance(script, state, intent, eligibility_flags)
        events.append(
            _event(
                provider_call_id, "turn.completed", {"seq": seq, "node": node_id, "intent": intent}
            )
        )
    turns = len(events) - 1
    reason = "completed" if state.done else "participant_hung_up"
    events.append(
        _event(
            provider_call_id,
            "call.ended",
            {"reason": reason, "duration_seconds": 20 * turns, "turn_count": turns},
        )
    )
    return events


Poster = Callable[[str, bytes, dict[str, str]], int]
