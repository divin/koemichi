"""Coordinate note stages and poll SQLite for work."""

import asyncio
import logging
import time
from datetime import UTC, datetime

from koemichi.integrations.pushover import PushoverError, send_message
from koemichi.services.router.classify import classify_with_details
from koemichi.services.router.dispatch import post_to_dispatch, validate_config
from koemichi.services.router.intents import Intent
from koemichi.services.transcriber.client import check_stt_health
from koemichi.services.transcriber.processor import transcribe_note
from koemichi.shared.db import close_db, create_db_and_tables
from koemichi.shared.models.note import NoteStatus
from koemichi.shared.models.notification import NotificationMode
from koemichi.shared.notifications import (
    claim_next_notification,
    complete_notification,
    dispatched_notification,
    fail_notification,
    recover_expired_notifications,
    transcribed_notification,
    validate_notification_config,
)
from koemichi.shared.settings import NOTIFICATION_MODE, STT_MODEL_NAME

from .queue import (
    Stage,
    claim_next_note,
    complete_stage,
    normalize_utc,
    record_failure,
    recover_expired_claims,
)

logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 1


def _utc_now() -> datetime:
    """Return the current time as a naive UTC datetime for SQLite."""
    return datetime.now(UTC).replace(tzinfo=None)


async def process_next_note(now: datetime | None = None) -> bool:
    """Process one eligible stage and persist its result or retry state.

    Parameters
    ----------
    now : datetime or None, optional
        Time to use for lease and retry calculations. Defaults to current UTC.

    Returns
    -------
    bool
        ``True`` if a note stage was claimed, including when it failed;
        ``False`` if no work was eligible.
    """
    current_time = normalize_utc(now) if now is not None else _utc_now()
    recover_expired_claims(current_time)
    claim = claim_next_note(current_time)
    if claim is None:
        return False

    note = claim.note
    try:
        if claim.stage == Stage.TRANSCRIBE:
            started_at = time.perf_counter()
            transcript = await transcribe_note(note)
            duration = time.perf_counter() - started_at
            complete_stage(
                claim,
                notification=transcribed_notification(
                    note.id, duration, STT_MODEL_NAME
                ),
                status=NoteStatus.TRANSCRIBED,
                transcript=transcript,
            )
        elif claim.stage == Stage.CLASSIFY:
            result = await classify_with_details(note.transcript)
            complete_stage(
                claim,
                status=NoteStatus.ROUTED,
                intent=result.intent.value,
                classification_method=result.method.value,
                classification_confidence=result.confidence,
            )
        else:
            if note.intent is None:
                raise ValueError("Routed note has no intent")
            await post_to_dispatch(note.id, note.transcript, Intent(note.intent))
            complete_stage(
                claim,
                notification=dispatched_notification(
                    note.id,
                    note.intent,
                    note.classification_method,
                    note.classification_confidence,
                ),
                status=NoteStatus.DISPATCHED,
            )
    except Exception as exc:  # noqa: BLE001 - persist stage failures for retries
        failure_time = current_time if now is not None else _utc_now()
        record_failure(claim, failure_time, exc)
        logger.error(
            "%s failed for note %s (%s)",
            claim.stage.value,
            note.id,
            type(exc).__name__,
        )

    return True


async def process_next_notification(now: datetime | None = None) -> bool:
    """Deliver one ready outbox event without holding a database transaction.

    Parameters
    ----------
    now : datetime or None, optional
        Time to use for claim and retry calculations. Defaults to current UTC.

    Returns
    -------
    bool
        ``True`` if an event was claimed, or ``False`` if the outbox is idle.
    """
    current_time = normalize_utc(now) if now is not None else _utc_now()
    recover_expired_notifications(current_time)
    event = claim_next_notification(current_time)
    if event is None:
        return False

    try:
        await send_message(event.message, event.title, event.priority)
    except PushoverError as exc:
        failure_time = _utc_now() if now is None else current_time
        fail_notification(
            event,
            failure_time,
            str(exc),
            retryable=exc.retryable,
        )
        logger.error(
            "Pushover delivery failed for event %s (%s)",
            event.dedupe_key,
            str(exc),
        )
    else:
        sent_at = _utc_now() if now is None else current_time
        complete_notification(event, sent_at)
    return True


async def _run_pipeline() -> None:
    """Poll and process note stages independently of Pushover delivery."""
    while True:
        worked = await process_next_note()
        if not worked:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _run_notifications() -> None:
    """Poll and deliver notification outbox events until shutdown."""
    while True:
        worked = await process_next_notification()
        if not worked:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def run_worker() -> None:
    """Initialize dependencies and run the pipeline and notification loops.

    Raises
    ------
    RuntimeError
        If the STT service is unreachable, router settings are missing, or
        notifications are enabled without both Pushover credentials.
    """
    create_db_and_tables()
    await check_stt_health()
    validate_config()
    validate_notification_config()
    logger.info("SQLite note worker started (notifications=%s)", NOTIFICATION_MODE)
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_run_pipeline())
            if NOTIFICATION_MODE is not NotificationMode.OFF:
                tasks.create_task(_run_notifications())
    finally:
        close_db()


def main() -> None:
    """Configure logging and run the worker event loop."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
