"""Route classified notes to registered Python intent handlers."""

import logging
from collections.abc import Awaitable, Callable

from koemichi.services.router.intents import Intent
from koemichi.shared.models.note import Note

logger = logging.getLogger(__name__)
IntentHandler = Callable[[Note], Awaitable[None]]

# Native handlers are registered by intent-specific workflow modules.
INTENT_HANDLERS: dict[Intent, IntentHandler] = {}


class UnimplementedIntentError(RuntimeError):
    """Raised when a classified intent has no Python handler yet."""


def register_intent_handler(intent: Intent, handler: IntentHandler) -> None:
    """Register one native handler for an intent.

    Parameters
    ----------
    intent : Intent
        Intent routed to the handler.
    handler : IntentHandler
        Async implementation that performs deterministic workflow steps and
        invokes any specialist agent needed for this intent.

    Raises
    ------
    ValueError
        If a handler is already registered for ``intent``.
    """
    if intent in INTENT_HANDLERS:
        raise ValueError(f"An intent handler is already registered for {intent.value}")
    INTENT_HANDLERS[intent] = handler


async def execute_intent(note: Note) -> None:
    """Run the registered Python handler or fail if the intent is unimplemented."""
    if note.intent is None:
        raise ValueError("Routed note has no intent")
    intent = Intent(note.intent)
    handler = INTENT_HANDLERS.get(intent)
    if handler is None:
        raise UnimplementedIntentError(
            f"No Python handler is registered for intent {intent.value}"
        )

    logger.info(
        "Running Python handler for note %s with intent %s", note.id, intent.value
    )
    await handler(note)
    logger.info(
        "Python handler completed for note %s with intent %s", note.id, intent.value
    )
