"""n8n dispatch client and router configuration validation."""

import logging
from uuid import UUID

import httpx

from koemichi.shared.settings import LLM_MODEL_NAME, LLM_URL, N8N_WEBHOOK_URL

from .intents import Intent

logger = logging.getLogger(__name__)

_DISPATCH_TIMEOUT_SECONDS = 30


def validate_config() -> None:
    """Validate the configuration required by classification and dispatch.

    Raises
    ------
    RuntimeError
        If any required LLM or n8n setting is missing.
    """
    missing = [
        name
        for name, value in (
            ("LLM_URL", LLM_URL),
            ("LLM_MODEL_NAME", LLM_MODEL_NAME),
            ("N8N_WEBHOOK_URL", N8N_WEBHOOK_URL),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Router config missing: " + ", ".join(missing))


async def post_to_dispatch(
    note_id: UUID, transcript: str | None, intent: Intent
) -> None:
    """POST the routing seam payload ``{note_id, transcript, intent}`` to n8n.

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
        If ``N8N_WEBHOOK_URL`` is not configured.
    """
    if not N8N_WEBHOOK_URL:
        raise RuntimeError("N8N_WEBHOOK_URL is not configured")

    payload = {
        "note_id": str(note_id),
        "transcript": transcript,
        "intent": intent.value,
    }
    logger.info("Dispatching note %s with intent %s to n8n", note_id, intent.value)
    async with httpx.AsyncClient(timeout=_DISPATCH_TIMEOUT_SECONDS) as client:
        response = await client.post(N8N_WEBHOOK_URL, json=payload)
    response.raise_for_status()
