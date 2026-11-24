"""Map one participant utterance to exactly one allowed intent.

`rules` is deterministic (tests, fixture mode, LLM fallback). `bedrock` runs a small
LangGraph graph: Bedrock classifies under a closed label set, the result is validated,
and anything invalid or failing falls back to the rules classifier. The model never
produces speech.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from .config import get_settings

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "distress",
        re.compile(
            r"\b(suicid|kill myself|emergency|can't breathe|chest pain|scared|crying)", re.I
        ),
    ),
    (
        "opt_out",
        re.compile(
            r"\b(stop calling|don't call|do not call|remove me|take me off|unsubscribe)", re.I
        ),
    ),
    (
        "request_human",
        re.compile(
            r"\b(real person|human|representative|operator|speak to (a|some)one|talk to (a|some)one)",
            re.I,
        ),
    ),
    ("language_other", re.compile(r"\b(espa[nñ]ol|spanish|arabic|العربية|no english|habla)", re.I)),
    (
        "wrong_number",
        re.compile(r"\b(wrong number|who is this for|no one by that|doesn't live here)", re.I),
    ),
    (
        "eligibility_dispute",
        re.compile(r"\b(why (am i|was i) (on|picked)|how did you get|not supposed to)", re.I),
    ),
    (
        "clinical_question",
        re.compile(
            r"\b(should i|is it safe|my (doctor|medication|meds|potassium|labs|kidney function)|side effect|dose|symptom|pain)",
            re.I,
        ),
    ),
    ("is_this_recorded", re.compile(r"\b(record(ed|ing)?)\b", re.I)),
    ("who_is_calling", re.compile(r"\b(who (is|are) (this|you)|who's calling)", re.I)),
    ("morning", re.compile(r"\bmorning", re.I)),
    ("afternoon", re.compile(r"\b(afternoon|midday|lunch)", re.I)),
    ("evening", re.compile(r"\b(evening|night|after work)", re.I)),
    ("any", re.compile(r"\b(any ?time|whenever|doesn't matter|any of those)", re.I)),
    ("unsure", re.compile(r"\b(not sure|unsure|maybe|i don't know|don't remember)", re.I)),
    (
        "no",
        re.compile(
            r"^\s*(no|nope|nah|not really|not interested|not now|no thanks|i'm not|i am not)\b",
            re.I,
        ),
    ),
    (
        "yes",
        re.compile(
            r"^\s*(yes|yeah|yep|yup|sure|okay|ok|alright|of course|go ahead|that's fine|sounds good|i am|i'm|uh-huh|correct|definitely)\b",
            re.I,
        ),
    ),
]


def classify_rules(utterance: str, allowed: list[str]) -> str:
    text = utterance.strip()
    if not text:
        return "unclear"
    for intent, pat in _PATTERNS:
        if intent in allowed and pat.search(text):
            return intent
    return "unclear"


class _GraphState(TypedDict, total=False):
    utterance: str
    prompt: str
    allowed: list[str]
    raw: str
    intent: str


def _build_graph(model_id: str, region: str) -> Any:
    from langchain_aws import ChatBedrockConverse
    from langgraph.graph import END, START, StateGraph

    llm = ChatBedrockConverse(model=model_id, region_name=region, temperature=0, max_tokens=8)

    def llm_node(state: _GraphState) -> _GraphState:
        system = (
            "You label one phone-call reply. Output exactly one label from the list and nothing "
            "else. Use clinical_question for any medical/medication/symptom question, distress "
            "for any sign of crisis, request_human if they ask for a person, unclear otherwise. "
            f"Labels: {', '.join(state['allowed'])}"
        )
        msg = llm.invoke(
            [
                ("system", system),
                (
                    "human",
                    f"Agent asked: {state['prompt']}\nParticipant said: {state['utterance']}",
                ),
            ]
        )
        return {"raw": str(msg.content).strip().lower()}

    def validate_node(state: _GraphState) -> _GraphState:
        raw = re.sub(r"[^a-z_]", "", state.get("raw", ""))
        if raw in state["allowed"]:
            return {"intent": raw}
        return {"intent": classify_rules(state["utterance"], state["allowed"])}

    g = StateGraph(_GraphState)
    g.add_node("llm", llm_node)
    g.add_node("validate", validate_node)
    g.add_edge(START, "llm")
    g.add_edge("llm", "validate")
    g.add_edge("validate", END)
    return g.compile()


_graph: Any = None


def classify(utterance: str, allowed: list[str], prompt: str = "") -> str:
    # Safety-critical intents are always caught deterministically first.
    hard = classify_rules(utterance, [i for i in ("distress", "opt_out") if i in allowed])
    if hard != "unclear":
        return hard
    settings = get_settings()
    if settings.classifier_mode != "bedrock":
        return classify_rules(utterance, allowed)
    global _graph
    try:
        if _graph is None:
            _graph = _build_graph(settings.bedrock_model_id, settings.aws_region)
        out = _graph.invoke({"utterance": utterance, "prompt": prompt, "allowed": allowed})
        intent = str(out.get("intent", "unclear"))
        return intent if intent in allowed else "unclear"
    except Exception:  # noqa: BLE001 - model outage must not break the call
        return classify_rules(utterance, allowed)
