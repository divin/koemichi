"""Tests for the router service: intent registry, classification, and dispatch."""

import json
import os
from uuid import uuid4

# Settings are read when the application modules are imported.
os.environ.setdefault("WEBHOOK_TOKEN", "unit-test-token")
os.environ.setdefault("STT_URL", "http://localhost:8080/v1/audio/transcriptions")
os.environ.setdefault("STT_MODEL_NAME", "test-model")
os.environ.setdefault("LLM_URL", "http://localhost:8080/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-llm")

import httpx
import pytest

from koemichi.services.router import classify as router_classify
from koemichi.services.router import dispatch as router_dispatch
from koemichi.services.router import prompts as router_prompts
from koemichi.services.router.classify import (
    ClassificationMethod,
    IntentDecision,
)
from koemichi.services.router.intents import INTENT_KEYWORDS, Intent, keyword_intent


class FakeAgentRunResult:
    """Stands in for ``pydantic-ai.AgentRunResult``; ``output`` is typed."""

    def __init__(self, output: IntentDecision) -> None:
        self.output = output


class FakeClassifier:
    """Stands in for the pydantic-ai Agent, recording calls and returning output."""

    def __init__(self, decision: IntentDecision) -> None:
        self.decision = decision
        self.calls: list[str] = []

    async def run(self, transcript: str) -> FakeAgentRunResult:
        self.calls.append(transcript)
        return FakeAgentRunResult(self.decision)


def test_keyword_fast_path_matches_first_phrase() -> None:
    assert keyword_intent("Journal entry for today") is Intent.JOURNAL
    assert keyword_intent("todo buy milk") is Intent.TODO
    assert keyword_intent("Memo: keys on the shelf") is Intent.MEMO
    assert keyword_intent("research sea level rise") is Intent.RESEARCH
    assert keyword_intent("look up the train timetable") is Intent.RESEARCH


def test_keyword_fast_path_is_case_and_whitespace_insensitive() -> None:
    assert keyword_intent("  TODO buy milk  ") is Intent.TODO
    assert keyword_intent("Journalistic style notes") is None


def test_keyword_fast_path_returns_none_without_match() -> None:
    assert keyword_intent("I am feeling tired today") is None
    assert keyword_intent("") is None
    assert keyword_intent(None) is None


def test_every_intent_except_other_has_keywords() -> None:
    for intent in Intent:
        if intent is not Intent.OTHER:
            assert INTENT_KEYWORDS[intent], f"Intent {intent} needs keywords"


def test_prompt_lists_every_intent() -> None:
    prompt = router_prompts.build_system_prompt()
    for intent in Intent:
        assert intent.value in prompt


@pytest.mark.asyncio
async def test_classify_skips_llm_on_keyword_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_get() -> object:
        raise AssertionError("LLM should not run when a keyword matches")

    monkeypatch.setattr(router_classify, "_get_classifier", unexpected_get)  # type: ignore[assignment]

    assert await router_classify.classify("todo buy bread") is Intent.TODO


@pytest.mark.asyncio
async def test_classify_returns_other_for_empty_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_get() -> object:
        raise AssertionError("LLM should not run on an empty transcript")

    monkeypatch.setattr(router_classify, "_get_classifier", unexpected_get)  # type: ignore[assignment]

    assert await router_classify.classify(None) is Intent.OTHER
    assert await router_classify.classify("  ") is Intent.OTHER


@pytest.mark.asyncio
async def test_classify_uses_typed_llm_output_as_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classifier = FakeClassifier(IntentDecision(intent=Intent.RESEARCH, confidence=0.9))
    monkeypatch.setattr(router_classify, "_get_classifier", lambda: classifier)  # type: ignore[assignment]

    assert (
        await router_classify.classify("what is the capital of New Zealand")
        is Intent.RESEARCH
    )
    assert classifier.calls == ["what is the capital of New Zealand"]


@pytest.mark.asyncio
async def test_classify_with_details_reports_keyword_method() -> None:
    result = await router_classify.classify_with_details("todo buy bread")

    assert result.intent is Intent.TODO
    assert result.method is ClassificationMethod.KEYWORD
    assert result.confidence is None


@pytest.mark.asyncio
async def test_classify_with_details_keeps_llm_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
async def test_classify_returns_other_when_llm_chooses_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classifier = FakeClassifier(IntentDecision(intent=Intent.OTHER, confidence=0.5))
    monkeypatch.setattr(router_classify, "_get_classifier", lambda: classifier)  # type: ignore[assignment]

    assert await router_classify.classify("the cat is sleeping") is Intent.OTHER


@pytest.mark.asyncio
async def test_classify_raises_when_llm_config_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router_classify, "LLM_URL", None)
    monkeypatch.setattr(router_classify, "LLM_MODEL_NAME", None)
    monkeypatch.setattr(router_classify, "_classifier", None)

    with pytest.raises(RuntimeError, match="LLM_URL and LLM_MODEL_NAME"):
        await router_classify.classify("no keywords here at all")


def test_intent_decision_schema_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        IntentDecision.model_validate({"intent": "not-an-intent", "confidence": 0.9})
    with pytest.raises(ValueError):
        IntentDecision.model_validate({"intent": "todo", "confidence": 1.5})


def test_intent_decision_schema_accepts_every_intent_member() -> None:
    for intent in Intent:
        decision = IntentDecision.model_validate(
            {"intent": intent.value, "confidence": 0.8}
        )
        assert decision.intent is intent


def test_validate_config_reports_missing_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router_dispatch, "LLM_URL", None)
    monkeypatch.setattr(router_dispatch, "LLM_MODEL_NAME", None)
    monkeypatch.setattr(router_dispatch, "N8N_WEBHOOK_URL", None)

    with pytest.raises(RuntimeError, match="LLM_URL, LLM_MODEL_NAME, N8N_WEBHOOK_URL"):
        router_dispatch.validate_config()


@pytest.mark.asyncio
async def test_post_to_dispatch_requires_configured_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router_dispatch, "N8N_WEBHOOK_URL", None)

    with pytest.raises(RuntimeError, match="N8N_WEBHOOK_URL is not configured"):
        await router_dispatch.post_to_dispatch(uuid4(), "text", Intent.MEMO)


@pytest.mark.asyncio
async def test_post_to_dispatch_posts_seam_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        router_dispatch.httpx,
        "AsyncClient",
        lambda *, timeout: async_client(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    note_id = uuid4()
    monkeypatch.setattr(
        router_dispatch, "N8N_WEBHOOK_URL", "http://n8n.test/webhook/dispatch"
    )

    await router_dispatch.post_to_dispatch(note_id, "transcript text", Intent.JOURNAL)

    assert len(requested) == 1
    assert json.loads(requested[0].content) == {
        "note_id": str(note_id),
        "transcript": "transcript text",
        "intent": "journal",
    }
