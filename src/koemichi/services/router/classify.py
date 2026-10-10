"""Intent classification: keyword fast path first, pydantic-ai LLM fallback."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from koemichi.shared.settings import LLM_MODEL_NAME, LLM_URL

from .intents import Intent, keyword_intent
from .prompts import build_system_prompt

logger = logging.getLogger(__name__)


class IntentDecision(BaseModel):
    """Validated result returned by the LLM classifier.

    pydantic-ai validates each model response against this schema, so an
    invalid response fails rather than silently producing a malformed intent.

    Attributes
    ----------
    intent : Intent
        Classified intent from the supported intent set.
    confidence : float
        Model-reported confidence between zero and one, inclusive.
    reason : str or None
        Optional explanation returned by the model.
    """

    intent: Intent
    confidence: Annotated[float, Field(ge=0, le=1)]
    reason: str | None = None


class ClassificationMethod(StrEnum):
    """Path used to classify a transcript."""

    KEYWORD = "keyword"
    LLM = "llm"
    EMPTY = "empty"


@dataclass(frozen=True)
class ClassificationResult:
    """Intent and safe diagnostics produced by the classifier.

    Attributes
    ----------
    intent : Intent
        Selected routing intent.
    method : ClassificationMethod
        Classification path used to produce the intent.
    confidence : float or None
        Confidence returned by the LLM, or ``None`` for deterministic paths.
    """

    intent: Intent
    method: ClassificationMethod
    confidence: float | None


_classifier: Agent[None, IntentDecision] | None = None


def validate_classifier_config() -> tuple[str, str]:
    """Validate and return configuration for the classifier's LLM fallback."""
    if not LLM_URL or not LLM_MODEL_NAME:
        missing = [
            name
            for name, value in (
                ("LLM_URL", LLM_URL),
                ("LLM_MODEL_NAME", LLM_MODEL_NAME),
            )
            if not value
        ]
        raise RuntimeError("Classifier config missing: " + ", ".join(missing))
    return LLM_URL, LLM_MODEL_NAME


def _get_classifier() -> Agent[None, IntentDecision]:
    """Create the intent agent on first use and return the cached instance.

    Returns
    -------
    Agent
        Pydantic AI agent configured for typed intent classification.

    Raises
    ------
    RuntimeError
        If the LLM URL or model name is not configured.
    """
    global _classifier

    if _classifier is not None:
        return _classifier

    model_url, model_name = validate_classifier_config()

    model = OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(base_url=model_url),
    )
    _classifier = Agent(
        model,
        output_type=IntentDecision,
        system_prompt=build_system_prompt(),
    )
    return _classifier


async def classify(transcript: str | None) -> Intent:
    """Classify a transcript and return its routing intent.

    Parameters
    ----------
    transcript : str or None
        Raw transcript text, or None when transcription produced nothing.

    Returns
    -------
    Intent
        The classified intent.

    Raises
    ------
    RuntimeError
        If ``LLM_URL`` or ``LLM_MODEL_NAME`` is missing when the LLM path runs.
    """
    result = await classify_with_details(transcript)
    return result.intent


async def classify_with_details(transcript: str | None) -> ClassificationResult:
    """Classify a transcript and retain the path and available confidence.

    The keyword fast path runs first; the LLM is only called when no keyword
    matches. Empty transcripts classify as ``memo`` without a model call.

    Parameters
    ----------
    transcript : str or None
        Raw transcript text, or None when transcription produced nothing.

    Returns
    -------
    ClassificationResult
        Intent plus classifier-path metadata for persistence and debug notices.

    Raises
    ------
    RuntimeError
        If ``LLM_URL`` or ``LLM_MODEL_NAME`` is missing when the LLM path runs.
    """
    text = transcript.strip() if transcript else ""

    fast = keyword_intent(text)
    if fast is not None:
        logger.info("Keyword fast path selected intent %s", fast.value)
        return ClassificationResult(fast, ClassificationMethod.KEYWORD, None)

    if not text:
        logger.info("Empty transcript classified as memo")
        return ClassificationResult(Intent.MEMO, ClassificationMethod.EMPTY, None)

    logger.info("No keyword match; calling LLM classifier")
    decision: IntentDecision = (await _get_classifier().run(text)).output
    logger.info(
        "LLM classified note as %s (confidence=%s)",
        decision.intent.value,
        decision.confidence,
    )
    return ClassificationResult(
        decision.intent,
        ClassificationMethod.LLM,
        decision.confidence,
    )
