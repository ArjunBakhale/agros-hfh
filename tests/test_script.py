import pytest

from agros_voice.classify import classify_rules
from agros_voice.providers.fixture import PERSONAS, simulate
from agros_voice.script import advance, load_script, replay, start

S = load_script("hfh_dialysis_education_demo")
FLAGS = {"adult": True, "on_or_preparing_for_dialysis": True}


@pytest.mark.parametrize(
    ("intents", "outcome", "phase"),
    [
        (["yes", "yes", "yes", "yes", "yes", "afternoon"], "scheduling_requested", "complete"),
        (["no"], "callback_later", "complete"),
        (["wrong_number"], "wrong_number", "complete"),
        (["yes", "no"], "not_eligible", "complete"),
        (["yes", "yes", "no"], "not_eligible", "complete"),
        (["yes", "yes", "yes", "no"], "declined", "complete"),
        (["yes", "yes", "yes", "yes", "no"], "declined", "complete"),
        (["yes", "clinical_question"], "needs_human", "needs_human"),
        (["distress"], "needs_human", "needs_human"),
        (["request_human"], "needs_human", "needs_human"),
        (["language_other"], "language_unsupported", "needs_human"),
        (["yes", "yes", "opt_out"], "opted_out", "complete"),
        (["yes", "unclear", "unclear"], "no_response", "needs_human"),
    ],
)
def test_paths(intents: list[str], outcome: str, phase: str) -> None:
    st = replay(S, intents)
    assert st.outcome == outcome
    assert st.phase == phase


def test_reprompt_then_recover() -> None:
    st = replay(S, ["yes", "unclear", "yes", "yes", "yes", "yes", "morning"])
    assert st.outcome == "scheduling_requested"
    assert st.answers["preferred_window"] == "morning"
    assert st.handoff == "coordinator_followup"


def test_faq_does_not_advance() -> None:
    st, _ = start(S)
    step = advance(S, st, "is_this_recorded")
    assert st.node == "identity_or_availability_check"
    assert "not recorded" in step.say


def test_unknown_intent_is_unclear() -> None:
    st, _ = start(S)
    assert advance(S, st, "make_me_a_sandwich").intent == "unclear"


def test_eligibility_mismatch_escalates() -> None:
    st = replay(S, ["yes", "no"], {"on_or_preparing_for_dialysis": True})
    assert st.outcome == "eligibility_mismatch"
    assert st.phase == "needs_human"


def test_complete_dialogue_rejects_more_turns() -> None:
    st = replay(S, ["no"])
    with pytest.raises(ValueError):
        advance(S, st, "yes")


def test_all_speech_comes_from_script() -> None:
    texts = {S.opening, *S.raw["closing"].values(), *(n["prompt"] for n in S.raw["nodes"].values())}
    st, _ = start(S)
    for intent in ["is_this_recorded", "yes", "unclear", "yes", "yes", "yes", "yes", "any"]:
        say = advance(S, st, intent).say
        assert any(t in say for t in texts)


@pytest.mark.parametrize(
    ("utt", "allowed", "expected"),
    [
        ("Yes, sure", ["yes", "no"], "yes"),
        ("nope", ["yes", "no"], "no"),
        (
            "Should I stop my blood pressure medication?",
            ["yes", "no", "clinical_question"],
            "clinical_question",
        ),
        ("I want to talk to a real person", ["yes", "request_human"], "request_human"),
        ("stop calling me", ["yes", "opt_out"], "opt_out"),
        ("", ["yes"], "unclear"),
        ("evening please", ["morning", "evening"], "evening"),
    ],
)
def test_rules_classifier(utt: str, allowed: list[str], expected: str) -> None:
    assert classify_rules(utt, allowed) == expected


def test_bedrock_failure_falls_back(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from agros_voice import classify

    settings.classifier_mode = "bedrock"

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("no creds")

    monkeypatch.setattr(classify, "_graph", None)
    monkeypatch.setattr(classify, "_build_graph", boom)
    assert classify.classify("yes please", ["yes", "no"]) == "yes"
    assert classify.classify("I'm scared, chest pain", ["yes", "distress"]) == "distress"


@pytest.mark.parametrize("persona", [p for p, u in PERSONAS.items() if u is not None])
def test_fixture_personas_terminate(persona: str) -> None:
    events = simulate("fx-x", S.id, persona, FLAGS)
    assert events[-1]["type"] == "call.ended"
    turns = [e for e in events if e["type"] == "turn.completed"]
    assert events[-1]["payload"]["turn_count"] == len(turns)
    assert all("text" not in e["payload"] for e in events)
