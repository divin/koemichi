"""HTTP webhook dispatch client and router configuration validation."""

import logging
from uuid import UUID

import httpx

from koemichi.shared.settings import (
    DISPATCH_WEBHOOK_AUTH_HEADER,
    DISPATCH_WEBHOOK_AUTH_VALUE,
    DISPATCH_WEBHOOK_URL,
    LLM_MODEL_NAME,
    LLM_URL,
)

from .intents import Intent

logger = logging.getLogger(__name__)

_DISPATCH_TIMEOUT_SECONDS = 30


def validate_config() -> None:
    """Validate the configuration required by classification and dispatch.

    Raises
    ------
    RuntimeError
        If any required LLM or dispatch webhook setting is missing.
    """
    missing = [
        name
        for name, value in (
            ("LLM_URL", LLM_URL),
            ("LLM_MODEL_NAME", LLM_MODEL_NAME),
            ("DISPATCH_WEBHOOK_URL", DISPATCH_WEBHOOK_URL),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Router config missing: " + ", ".join(missing))
    if bool(DISPATCH_WEBHOOK_AUTH_HEADER) != bool(DISPATCH_WEBHOOK_AUTH_VALUE):
        raise RuntimeError(
            "DISPATCH_WEBHOOK_AUTH_HEADER and DISPATCH_WEBHOOK_AUTH_VALUE "
            "must be set together"
        )


async def post_to_dispatch(
    note_id: UUID, transcript: str | None, intent: Intent
) -> None:
    """POST the routing payload to the configured webhook.

    The JSON body contains ``note_id``, ``transcript``, and ``intent``.

    Parameters
    ----------
    note_id : UUID
        Stable identifier of the note, used by downstream workflows for
        deduplication where available.
    transcript : str or None
        Transcribed note content, if available.
    intent : Intent
        Classification selected for the note.

    Raises
    ------
    httpx.HTTPError
        If the dispatch request fails.
    RuntimeError
        If ``DISPATCH_WEBHOOK_URL`` is not configured.
    """
    if not DISPATCH_WEBHOOK_URL:
        raise RuntimeError("DISPATCH_WEBHOOK_URL is not configured")

    payload = {
        "note_id": str(note_id),
        "transcript": transcript,
        "intent": intent.value,
    }
    headers: dict[str, str] = {}
    if DISPATCH_WEBHOOK_AUTH_HEADER and DISPATCH_WEBHOOK_AUTH_VALUE:
        headers[DISPATCH_WEBHOOK_AUTH_HEADER] = DISPATCH_WEBHOOK_AUTH_VALUE

    logger.info(
        "Dispatching note %s with intent %s to configured webhook",
        note_id,
        intent.value,
    )
    async with httpx.AsyncClient(timeout=_DISPATCH_TIMEOUT_SECONDS) as client:
        response = await client.post(
            DISPATCH_WEBHOOK_URL, json=payload, headers=headers
        )
    response.raise_for_status()
