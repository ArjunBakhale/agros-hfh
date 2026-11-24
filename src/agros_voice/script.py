"""Deterministic outreach script engine.

Speech is only ever drawn from the approved script. A classifier (rules or a bounded LLM)
maps each participant utterance to one allowed intent; this engine alone decides the next
state, so dispositions can be recomputed server-side from the sequence of intents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

Phase = Literal[
    "not_started",
    "queued",
    "calling",
    "identity_or_availability_check",
    "screening",
    "interest_capture",
    "consent_handoff",
    "scheduling",
    "complete",
    "needs_human",
    "failed",
]

GLOBAL_INTENTS = (
    "clinical_question",
    "distress",
    "request_human",
    "eligibility_dispute",
    "language_other",
    "opt_out",
    "who_is_calling",
    "is_this_recorded",
    "unclear",
)

# Terminal outcome -> (final dialogue phase, contact status)
OUTCOMES: dict[str, tuple[Phase, str]] = {
    "scheduling_requested": ("complete", "scheduling_requested"),
    "declined": ("complete", "declined"),
    "callback_later": ("complete", "unreachable"),
    "wrong_number": ("complete", "closed"),
    "opted_out": ("complete", "declined"),
    "not_eligible": ("complete", "screened"),
    "needs_human": ("needs_human", "needs_human"),
    "language_unsupported": ("needs_human", "needs_human"),
    "unreachable": ("failed", "unreachable"),
    "no_response": ("needs_human", "needs_human"),
    "eligibility_mismatch": ("needs_human", "needs_human"),
}


@dataclass(frozen=True)
class Script:
    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def version(self) -> int:
        return int(self.raw["version"])

    @property
    def opening(self) -> str:
        return str(self.raw["opening"])

    def node(self, node_id: str) -> dict[str, Any]:
        return dict(self.raw["nodes"][node_id])

    def allowed_intents(self, node_id: str) -> list[str]:
        return list(self.node(node_id)["answers"].keys()) + list(GLOBAL_INTENTS)

    def closing(self, outcome: str) -> str:
        return str(self.raw["closing"].get(outcome, self.raw["closing"]["needs_human"]))


@lru_cache
def load_script(script_id: str) -> Script:
    if not script_id.replace("_", "").isalnum():
        raise ValueError("invalid script id")
    text = (resources.files("agros_voice.scripts") / f"{script_id}.json").read_text()
    return Script(json.loads(text))


def available_scripts() -> list[str]:
    pkg = resources.files("agros_voice.scripts")
    return sorted(p.name.removesuffix(".json") for p in pkg.iterdir() if p.name.endswith(".json"))


@dataclass
class DialogueState:
    node: str | None
    phase: Phase
    answers: dict[str, str] = field(default_factory=dict)
    outcome: str | None = None
    handoff: str | None = None
    reprompts: int = 0
    turns: int = 0

    @property
    def done(self) -> bool:
        return self.outcome is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "phase": self.phase,
            "answers": dict(self.answers),
            "outcome": self.outcome,
            "handoff": self.handoff,
            "turns": self.turns,
        }


@dataclass(frozen=True)
class Step:
    state: DialogueState
    say: str
    node: str | None
    intent: str


def start(script: Script) -> tuple[DialogueState, str]:
    first = script.raw["start"]
    phase = script.node(first)["phase"]
    return DialogueState(node=first, phase=phase), script.opening


def _end(script: Script, state: DialogueState, outcome: str, prefix: str = "") -> str:
    state.outcome = outcome
    state.phase = OUTCOMES[outcome][0]
    state.node = None
    return (prefix + " " + script.closing(outcome)).strip()


def advance(
    script: Script,
    state: DialogueState,
    intent: str,
    eligibility_flags: dict[str, Any] | None = None,
) -> Step:
    """Apply one classified participant turn. Unknown intents are treated as 'unclear'."""
    if state.done or state.node is None:
        raise ValueError("dialogue already complete")
    node_id = state.node
    node = script.node(node_id)
    allowed = script.allowed_intents(node_id)
    if intent not in allowed:
        intent = "unclear"
    state.turns += 1

    escalations: dict[str, str] = script.raw["escalations"]
    if intent in escalations:
        if intent == "language_other":
            state.answers["language_support"] = "other"
        return Step(state, _end(script, state, escalations[intent]), node_id, intent)

    if intent in ("who_is_calling", "is_this_recorded"):
        faq = {f["intent"]: f["answer"] for f in script.raw["faq"]}
        return Step(state, f"{faq[intent]} {node['prompt']}", node_id, intent)

    if intent == "unclear":
        if state.reprompts < int(script.raw.get("max_reprompts", 1)):
            state.reprompts += 1
            return Step(state, f"Sorry, I didn't catch that. {node['prompt']}", node_id, intent)
        return Step(state, _end(script, state, "no_response"), node_id, intent)

    state.reprompts = 0
    if key := node.get("answer_key"):
        state.answers[key] = intent

    flag = node.get("eligibility_flag")
    if flag and eligibility_flags and flag in eligibility_flags:
        expected = eligibility_flags[flag]
        if isinstance(expected, bool) and intent in ("yes", "no") and (intent == "yes") != expected:
            state.answers["eligibility_mismatch"] = flag
            return Step(state, _end(script, state, "eligibility_mismatch"), node_id, intent)

    transition = node["answers"][intent]
    if handoff := transition.get("handoff"):
        state.handoff = handoff
    if "end" in transition:
        return Step(state, _end(script, state, transition["end"]), node_id, intent)
    nxt = transition["next"]
    state.node = nxt
    state.phase = script.node(nxt)["phase"]
    prefix = "Okay." if intent in ("yes", "unsure") else "Thanks."
    return Step(state, f"{prefix} {script.node(nxt)['prompt']}", node_id, intent)


def replay(
    script: Script, intents: list[str], eligibility_flags: dict[str, Any] | None = None
) -> DialogueState:
    """Recompute a dialogue from its ordered intents (server-side source of truth)."""
    state, _ = start(script)
    for intent in intents:
        if state.done:
            break
        advance(script, state, intent, eligibility_flags)
    return state
