"""Pushover notification policy and durable SQLite outbox operations."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import or_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from koemichi.shared.db import engine
from koemichi.shared.models.note import NoteSource
from koemichi.shared.models.notification import (
    NotificationMode,
    NotificationOutbox,
    NotificationStatus,
)
from koemichi.shared.settings import (
    NOTIFICATION_MODE,
    PUSHOVER_API_TOKEN,
    PUSHOVER_USER_KEY,
)

logger = logging.getLogger(__name__)
columns = cast(Any, NotificationOutbox).__table__.c

OUTBOX_LEASE_SECONDS = 60
OUTBOX_MAX_ATTEMPTS = 3
OUTBOX_BASE_RETRY_SECONDS = 5


@dataclass(frozen=True)
class NotificationDraft:
    """Notification content to add in the same transaction as a note update.

    Attributes
    ----------
    event_key : str
        Stable, unique key for this note lifecycle event.
    message : str
        Message sent in normal and debug modes.
    priority : int
        Pushover priority for the regular message.
    debug_message : str or None
        Optional diagnostic version used only in debug mode.
    debug_priority : int or None
        Optional Pushover priority override for the debug message. Debug details
        default to low priority (-1) when no override is provided.
    debug_only : bool
        Whether the event should be suppressed outside debug mode.
    """

    event_key: str
    message: str
    priority: int = 0
    debug_message: str | None = None
    debug_priority: int | None = None
    debug_only: bool = False


def _short_id(note_id: UUID) -> str:
    """Return a short, non-secret identifier suitable for notification text."""
    return note_id.hex[:8]


def received_notification(note_id: UUID, source: NoteSource) -> NotificationDraft:
    """Build the normal-priority notification for a persisted voice note.

    Parameters
    ----------
    note_id : UUID
        Identifier assigned to the accepted note.
    source : NoteSource
        Webhook source that accepted the note.

    Returns
    -------
    NotificationDraft
        Received-event message and deduplication key.
    """
    return NotificationDraft(
        event_key=f"{note_id}:received",
        message=f"Voice note received ({source.value}) [{_short_id(note_id)}].",
    )


def oversized_upload_notification(
    actual_size_bytes: int, max_size_bytes: int, *, now: datetime | None = None
) -> NotificationDraft:
    """Build a rate-limited alert for an authenticated oversized upload.

    Parameters
    ----------
    actual_size_bytes : int
        Size of the uploaded audio file.
    max_size_bytes : int
        Maximum accepted audio size.
    now : datetime or None, optional
        Time used to form the deduplication window. Defaults to current UTC.

    Returns
    -------
    NotificationDraft
        Normal-priority alert with a stable per-hour event key.
    """
    current_time = _normalize_utc(now or datetime.now(UTC))
    hour_key = current_time.strftime("%Y%m%d%H")
    actual_mib = actual_size_bytes / (1024 * 1024)
    max_mib = max_size_bytes / (1024 * 1024)
    return NotificationDraft(
        event_key=f"oversized-upload:{hour_key}",
        message=(
            f"Oversized voice upload rejected ({actual_mib:.1f} MiB; "
            f"limit {max_mib:.0f} MiB)."
        ),
        debug_message=(
            f"Rejected an authenticated voice upload of {actual_size_bytes} bytes; "
            f"the configured limit is {max_size_bytes} bytes."
        ),
    )


def transcribed_notification(
    note_id: UUID, duration_seconds: float, provider: str
) -> NotificationDraft:
    """Build a transcription-complete notification without exposing the text.

    Parameters
    ----------
    note_id : UUID
        Identifier of the transcribed note.
    duration_seconds : float
        Elapsed time spent transcribing the note.
    provider : str
        Configured STT model name, included only in debug mode.

    Returns
    -------
    NotificationDraft
        Transcription event with a generic normal message and safe diagnostics.
    """
    short_id = _short_id(note_id)
    return NotificationDraft(
        event_key=f"{note_id}:transcribed",
        message=f"Voice note transcribed [{short_id}].",
        debug_message=(
            f"Voice note transcribed [{short_id}] in {duration_seconds:.1f}s "
            f"using {provider}."
        ),
    )


def dispatched_notification(
    note_id: UUID,
    intent: str,
    method: str | None,
    confidence: float | None,
) -> NotificationDraft:
    """Build the notification emitted after the webhook accepts a dispatch.

    Parameters
    ----------
    note_id : UUID
        Identifier of the dispatched note.
    intent : str
        Intent selected for the note.
    method : str or None
        Classification path, such as ``keyword`` or ``llm``.
    confidence : float or None
        Classifier confidence when the model supplied one.

    Returns
    -------
    NotificationDraft
        Dispatch event with optional classifier details for debug mode.
    """
    short_id = _short_id(note_id)
    confidence_text = f"{confidence:.2f}" if confidence is not None else "n/a"
    debug_message = (
        f"Voice note [{short_id}] dispatched as {intent}; "
        f"classifier={method or 'unknown'}, confidence={confidence_text}."
    )
    return NotificationDraft(
        event_key=f"{note_id}:dispatched",
        message=f"Voice note [{short_id}] routed as {intent}.",
        debug_message=debug_message,
    )


def stage_claim_notification(
    note_id: UUID, stage: str, attempt_count: int
) -> NotificationDraft:
    """Build a low-priority debug event for a claimed processing stage.

    Parameters
    ----------
    note_id : UUID
        Identifier of the note being processed.
    stage : str
        Pipeline stage acquired by the worker.
    attempt_count : int
        Attempt number associated with the claim.

    Returns
    -------
    NotificationDraft
        Debug-only claim event.
    """
    return NotificationDraft(
        event_key=f"{note_id}:claim:{stage}:{attempt_count}",
        message=(
            f"Processing note [{_short_id(note_id)}]: {stage}, attempt {attempt_count}."
        ),
        priority=-1,
        debug_only=True,
    )


def retry_notification(
    note_id: UUID, stage: str, attempt_count: int, error_type: str
) -> NotificationDraft:
    """Build a low-priority debug event for a scheduled processing retry.

    Parameters
    ----------
    note_id : UUID
        Identifier of the note being retried.
    stage : str
        Pipeline stage that failed.
    attempt_count : int
        Failed attempt number.
    error_type : str
        Exception class name; exception messages are deliberately excluded.

    Returns
    -------
    NotificationDraft
        Debug-only retry event.
    """
    return NotificationDraft(
        event_key=f"{note_id}:retry:{stage}:{attempt_count}",
        message=(
            f"Retrying note [{_short_id(note_id)}]: {stage}, "
            f"attempt {attempt_count} ({error_type})."
        ),
        priority=-1,
        debug_only=True,
    )


def recovery_notification(
    note_id: UUID, stage: str, attempt_count: int
) -> NotificationDraft:
    """Build a low-priority debug event when an interrupted claim is recovered.

    Parameters
    ----------
    note_id : UUID
        Identifier of the recovered note.
    stage : str
        Pipeline stage returned to its pending state.
    attempt_count : int
        Number of attempts made before recovery.

    Returns
    -------
    NotificationDraft
        Debug-only recovery event.
    """
    return NotificationDraft(
        event_key=f"{note_id}:recovered:{stage}:{attempt_count}",
        message=(
            f"Recovered interrupted note [{_short_id(note_id)}]: {stage}, "
            f"attempt {attempt_count}."
        ),
        priority=-1,
        debug_only=True,
    )


def failure_notification(
    note_id: UUID, stage: str, attempt_count: int, error_type: str
) -> NotificationDraft:
    """Build a high-priority notification for terminal note-processing errors.

    Parameters
    ----------
    note_id : UUID
        Identifier of the note that exhausted retries.
    stage : str
        Pipeline stage that failed permanently.
    attempt_count : int
        Number of attempts made for the stage.
    error_type : str
        Exception class name; exception messages are deliberately excluded.

    Returns
    -------
    NotificationDraft
        Terminal error event at Pushover priority 1.
    """
    short_id = _short_id(note_id)
    return NotificationDraft(
        event_key=f"{note_id}:error:{stage}:{attempt_count}",
        message=f"Voice note [{short_id}] failed during {stage}.",
        priority=1,
        debug_message=(
            f"Voice note [{short_id}] failed during {stage} after "
            f"{attempt_count} attempts ({error_type})."
        ),
        debug_priority=1,
    )


def enqueue_notification(
    session: Session, note_id: UUID | None, draft: NotificationDraft
) -> None:
    """Add an enabled notification to the caller's current transaction.

    Parameters
    ----------
    session : Session
        Open SQLModel session whose transaction also persists the note change.
    note_id : UUID or None
        Identifier of the note associated with the event, or ``None`` when the
        event concerns a rejected request that did not create a note.
    draft : NotificationDraft
        Message, priority, and stable event key to enqueue.

    Notes
    -----
    This function never commits. The note update and outbox insert therefore
    succeed or roll back together. Debug-only drafts are discarded in normal
    mode, and all drafts are discarded when notifications are disabled.
    """
    if NOTIFICATION_MODE is NotificationMode.OFF:
        return
    if draft.debug_only and NOTIFICATION_MODE is not NotificationMode.DEBUG:
        return

    message = draft.message
    priority = draft.priority
    if NOTIFICATION_MODE is NotificationMode.DEBUG and draft.debug_message is not None:
        message = draft.debug_message
        priority = draft.debug_priority if draft.debug_priority is not None else -1

    event = NotificationOutbox(
        note_id=note_id,
        dedupe_key=draft.event_key,
        title="Koemichi",
        message=message[:1024],
        priority=priority,
    )
    statement = (
        sqlite_insert(NotificationOutbox)
        .values(
            id=event.id,
            note_id=event.note_id,
            dedupe_key=event.dedupe_key,
            title=event.title,
            message=event.message,
            priority=event.priority,
            status=event.status,
            created_at=event.created_at,
            attempt_count=event.attempt_count,
            next_attempt_at=event.next_attempt_at,
            lease_expires_at=event.lease_expires_at,
            sent_at=event.sent_at,
            error_message=event.error_message,
        )
        .on_conflict_do_nothing(index_elements=[NotificationOutbox.dedupe_key])
    )
    session.exec(statement)


def validate_notification_config() -> None:
    """Require Pushover credentials when notification delivery is enabled.

    Raises
    ------
    RuntimeError
        If notifications are enabled but either credential is missing.
    """
    if NOTIFICATION_MODE is NotificationMode.OFF:
        return
    missing = [
        name
        for name, value in (
            ("PUSHOVER_API_TOKEN", PUSHOVER_API_TOKEN),
            ("PUSHOVER_USER_KEY", PUSHOVER_USER_KEY),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Pushover notifications are enabled but configuration is missing: "
            + ", ".join(missing)
        )


def _normalize_utc(value: datetime) -> datetime:
    """Convert a timestamp to naive UTC for comparisons in SQLite."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _retry_delay(attempt_count: int) -> int:
    """Return the bounded-exponential delivery retry delay in seconds."""
    return OUTBOX_BASE_RETRY_SECONDS * 2 ** max(0, attempt_count - 1)


def recover_expired_notifications(now: datetime) -> None:
    """Release expired delivery leases and schedule a retry if attempts remain.

    Parameters
    ----------
    now : datetime
        Current time as naive UTC or timezone-aware UTC.
    """
    now = _normalize_utc(now)
    with Session(engine) as session:
        expired = session.exec(
            select(NotificationOutbox).where(
                columns.status == NotificationStatus.SENDING,
                columns.lease_expires_at.is_not(None),
                columns.lease_expires_at <= now,
            )
        ).all()
        for event in expired:
            if event.attempt_count >= OUTBOX_MAX_ATTEMPTS:
                event.status = NotificationStatus.ERROR
                event.next_attempt_at = None
                event.error_message = "Delivery lease expired after final attempt"
            else:
                event.status = NotificationStatus.PENDING
                event.next_attempt_at = now + timedelta(
                    seconds=_retry_delay(event.attempt_count)
                )
                event.error_message = "Delivery interrupted; retry scheduled"
            event.lease_expires_at = None
            session.add(event)
        if expired:
            session.commit()


def claim_next_notification(now: datetime) -> NotificationOutbox | None:
    """Lease the oldest pending outbox event whose retry delay has elapsed.

    Parameters
    ----------
    now : datetime
        Current time as naive UTC or timezone-aware UTC.

    Returns
    -------
    NotificationOutbox or None
        Claimed event, or ``None`` when no notification is ready.
    """
    now = _normalize_utc(now)
    eligible = or_(columns.next_attempt_at.is_(None), columns.next_attempt_at <= now)
    with Session(engine) as session:
        event = session.exec(
            select(NotificationOutbox)
            .where(
                columns.status == NotificationStatus.PENDING,
                eligible,
                columns.lease_expires_at.is_(None),
            )
            .order_by(columns.created_at)
            .limit(1)
        ).first()
        if event is None:
            return None

        expires_at = now + timedelta(seconds=OUTBOX_LEASE_SECONDS)
        result = session.exec(
            update(NotificationOutbox)
            .where(
                columns.id == event.id,
                columns.status == NotificationStatus.PENDING,
                eligible,
                columns.lease_expires_at.is_(None),
            )
            .values(
                status=NotificationStatus.SENDING,
                attempt_count=columns.attempt_count + 1,
                lease_expires_at=expires_at,
                error_message=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            session.rollback()
            return None
        session.commit()
        session.refresh(event)
        return event


def complete_notification(event: NotificationOutbox, now: datetime) -> None:
    """Mark an event sent if this delivery attempt still owns its lease.

    Parameters
    ----------
    event : NotificationOutbox
        Claimed outbox event returned by :func:`claim_next_notification`.
    now : datetime
        Time at which Pushover accepted the event.
    """
    with Session(engine) as session:
        result = session.exec(
            update(NotificationOutbox)
            .where(
                columns.id == event.id,
                columns.status == NotificationStatus.SENDING,
                columns.lease_expires_at == event.lease_expires_at,
            )
            .values(
                status=NotificationStatus.SENT,
                sent_at=_normalize_utc(now),
                lease_expires_at=None,
                next_attempt_at=None,
                error_message=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            logger.warning("Discarding stale delivery success for event %s", event.id)
            session.rollback()
            return
        session.commit()


def fail_notification(
    event: NotificationOutbox,
    now: datetime,
    error_message: str,
    *,
    retryable: bool,
) -> None:
    """Retry or terminally fail a delivery attempt using sanitized details.

    Parameters
    ----------
    event : NotificationOutbox
        Claimed event returned by :func:`claim_next_notification`.
    now : datetime
        Time of the failed delivery attempt.
    error_message : str
        Sanitized diagnostic that does not contain credentials or response text.
    retryable : bool
        Whether the client identified this as a transient network/server error.
    """
    now = _normalize_utc(now)
    if retryable and event.attempt_count < OUTBOX_MAX_ATTEMPTS:
        status = NotificationStatus.PENDING
        next_attempt_at = now + timedelta(seconds=_retry_delay(event.attempt_count))
        stored_error = error_message[:255]
    else:
        status = NotificationStatus.ERROR
        next_attempt_at = None
        stored_error = error_message[:255]

    with Session(engine) as session:
        result = session.exec(
            update(NotificationOutbox)
            .where(
                columns.id == event.id,
                columns.status == NotificationStatus.SENDING,
                columns.lease_expires_at == event.lease_expires_at,
            )
            .values(
                status=status,
                next_attempt_at=next_attempt_at,
                lease_expires_at=None,
                error_message=stored_error,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            logger.warning("Discarding stale delivery failure for event %s", event.id)
            session.rollback()
            return
        session.commit()
