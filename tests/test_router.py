"""Tests for the router service: intent registry, classification, and dispatch."""

import os

# Settings are read when the application modules are imported.
os.environ.setdefault("LLM_URL", "http://localhost:8080/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-llm")

import pytest

from koemichi.services.router import classify as router_classify
from koemichi.services.router import prompts as router_prompts
from koemichi.services.router.classify import (
    ClassificationMethod,
    IntentDecision,
)
from koemichi.services.router.intents import INTENT_KEYWORDS, Intent, keyword_intent


class FakeAgentRunResult:
    """Stands in for ``pydantic-ai.AgentRunResult``; ``output`` is typed."""

    def __init__(self, output: IntentDecision) -> None:
        """Store the typed output returned by the fake agent."""
        self.output = output


class FakeClassifier:
    """Stands in for the pydantic-ai Agent, recording calls and returning output."""

    def __init__(self, decision: IntentDecision) -> None:
        """Initialize a fake classifier with its response decision."""
        self.decision = decision
        self.calls: list[str] = []

    async def run(self, transcript: str) -> FakeAgentRunResult:
        """Record the input and return the configured fake result."""
        self.calls.append(transcript)
        return FakeAgentRunResult(self.decision)


def test_keyword_fast_path_matches_first_phrase() -> None:
    """Verify opening keyword phrases map to their configured intents."""
    assert keyword_intent("Journal entry for today") is Intent.JOURNAL
    assert keyword_intent("todo buy milk") is Intent.TODO
    assert keyword_intent("Memo: keys on the shelf") is Intent.MEMO
    assert keyword_intent("research sea level rise") is Intent.RESEARCH
    assert keyword_intent("look up the train timetable") is Intent.RESEARCH


def test_keyword_fast_path_is_case_and_whitespace_insensitive() -> None:
    """Verify keyword matching ignores surrounding spaces and case."""
    assert keyword_intent("  TODO buy milk  ") is Intent.TODO
    assert keyword_intent("Journalistic style notes") is None


def test_keyword_fast_path_returns_none_without_match() -> None:
    """Verify unmatched and empty transcripts have no keyword intent."""
    assert keyword_intent("I am feeling tired today") is None
    assert keyword_intent("") is None
    assert keyword_intent(None) is None


def test_every_intent_has_keywords() -> None:
    """Verify every supported intent has at least one keyword."""
    for intent in Intent:
        assert INTENT_KEYWORDS[intent], f"Intent {intent} needs keywords"


def test_prompt_lists_every_intent_and_uses_memo_as_fallback() -> None:
    """Verify the classifier prompt lists intents and the memo fallback."""
    prompt = router_prompts.build_system_prompt()
    for intent in Intent:
        assert intent.value in prompt
    assert "Choose `memo` for a general note" in prompt
    assert "`other`" not in prompt


@pytest.mark.asyncio
async def test_classify_skips_llm_on_keyword_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a keyword match avoids calling the language model."""

    def unexpected_get() -> object:
        """Fail if classification requests the model for a keyword match."""
        raise AssertionError("LLM should not run when a keyword matches")

    monkeypatch.setattr(router_classify, "_get_classifier", unexpected_get)  # type: ignore[assignment]

    assert await router_classify.classify("todo buy bread") is Intent.TODO


@pytest.mark.asyncio
async def test_classify_returns_memo_for_empty_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify an empty transcript receives memo without calling the model."""

    def unexpected_get() -> object:
        """Fail if classification requests the model for empty input."""
        raise AssertionError("LLM should not run on an empty transcript")

    monkeypatch.setattr(router_classify, "_get_classifier", unexpected_get)  # type: ignore[assignment]

    assert await router_classify.classify(None) is Intent.MEMO
    assert await router_classify.classify("  ") is Intent.MEMO


@pytest.mark.asyncio
async def test_classify_uses_typed_llm_output_as_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify typed model output provides the classification fallback."""
    classifier = FakeClassifier(IntentDecision(intent=Intent.RESEARCH, confidence=0.9))
    monkeypatch.setattr(router_classify, "_get_classifier", lambda: classifier)  # type: ignore[assignment]

    assert (
        await router_classify.classify("what is the capital of New Zealand")
        is Intent.RESEARCH
    )
    assert classifier.calls == ["what is the capital of New Zealand"]


@pytest.mark.asyncio
async def test_classify_with_details_reports_keyword_method() -> None:
    """Verify detailed classification records a keyword match."""
    result = await router_classify.classify_with_details("todo buy bread")

    assert result.intent is Intent.TODO
    assert result.method is ClassificationMethod.KEYWORD
    assert result.confidence is None


@pytest.mark.asyncio
async def test_classify_with_details_keeps_llm_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify detailed model classification retains its confidence."""
    classifier = FakeClassifier(IntentDecision(intent=Intent.RESEARCH, confidence=0.87))
    monkeypatch.setattr(
        router_classify,
        "_get_classifier",
        lambda: classifier,  # type: ignore[assignment]
    )

    result = await router_classify.classify_with_details("how do solar panels work")

    assert result.intent is Intent.RESEARCH
    assert result.method is ClassificationMethod.LLM
    assert result.confidence == 0.87


@pytest.mark.asyncio
async def test_classify_uses_memo_when_no_specific_intent_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the model can choose memo when no specific intent fits."""
    classifier = FakeClassifier(IntentDecision(intent=Intent.MEMO, confidence=0.5))
    monkeypatch.setattr(router_classify, "_get_classifier", lambda: classifier)  # type: ignore[assignment]

    assert await router_classify.classify("the cat is sleeping") is Intent.MEMO


@pytest.mark.asyncio
async def test_classify_raises_when_llm_config_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify model classification fails clearly when settings are missing."""
    monkeypatch.setattr(router_classify, "LLM_URL", None)
    monkeypatch.setattr(router_classify, "LLM_MODEL_NAME", None)
    monkeypatch.setattr(router_classify, "_classifier", None)

    with pytest.raises(RuntimeError, match="LLM_URL, LLM_MODEL_NAME"):
        await router_classify.classify("no keywords here at all")


def test_intent_decision_schema_rejects_invalid_values() -> None:
    """Verify invalid intents and confidence values fail schema validation."""
    with pytest.raises(ValueError):
        IntentDecision.model_validate({"intent": "not-an-intent", "confidence": 0.9})
    with pytest.raises(ValueError):
        IntentDecision.model_validate({"intent": "other", "confidence": 0.5})
    with pytest.raises(ValueError):
        IntentDecision.model_validate({"intent": "todo", "confidence": 1.5})


def test_intent_decision_schema_accepts_every_intent_member() -> None:
    """Verify the decision schema accepts every supported intent."""
    for intent in Intent:
        decision = IntentDecision.model_validate(
            {"intent": intent.value, "confidence": 0.8}
        )
        assert decision.intent is intent
