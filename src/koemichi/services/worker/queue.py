"""SQLite claims, leases, and retry state for pipeline notes."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import or_, update
from sqlmodel import Session, select

from koemichi.shared.db import engine
from koemichi.shared.models.note import Note, NoteStatus
from koemichi.shared.notifications import (
    NotificationDraft,
    enqueue_notification,
    failure_notification,
    recovery_notification,
    retry_notification,
    stage_claim_notification,
)

logger = logging.getLogger(__name__)
columns = cast(Any, Note).__table__.c

LEASE_SECONDS = 600
MAX_ATTEMPTS = 3
BASE_RETRY_SECONDS = 5


class Stage(StrEnum):
    """Routing stage associated with a claimed transcript."""

    CLASSIFY = "classification"
    DISPATCH = "dispatch"


@dataclass(frozen=True)
class ClaimedNote:
    """Note and lease metadata returned when work is claimed.

    Attributes
    ----------
    note : Note
        Snapshot of the note at claim time, including its lease expiration.
    stage : Stage
        Processing stage the worker owns.
    claimed_status : NoteStatus
        Status written to the database when the lease was acquired.
    """

    note: Note
    stage: Stage
    claimed_status: NoteStatus


def normalize_utc(value: datetime) -> datetime:
    """Normalize a time to naive UTC, as SQLite's driver returns timestamps."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _retry_delay(attempt_count: int) -> int:
    """Calculate the exponential retry delay for an attempt number.

    Parameters
    ----------
    attempt_count : int
        Number of attempts already made for the current stage.

    Returns
    -------
    int
        Delay in seconds before the next attempt.
    """
    return BASE_RETRY_SECONDS * 2 ** max(0, attempt_count - 1)


def _ready_status(stage: Stage) -> NoteStatus:
    """Return the persisted status that makes a stage eligible for work.

    Parameters
    ----------
    stage : Stage
        Pipeline stage to map.

    Returns
    -------
    NoteStatus
        Status representing pending work for ``stage``.
    """
    return {
        Stage.CLASSIFY: NoteStatus.TRANSCRIBED,
        Stage.DISPATCH: NoteStatus.ROUTED,
    }[stage]


def _running_status(stage: Stage) -> NoteStatus:
    """Return the status to persist while a stage is leased.

    Parameters
    ----------
    stage : Stage
        Pipeline stage to map.

    Returns
    -------
    NoteStatus
        Status representing an active claim for ``stage``.
    """
    return {
        Stage.CLASSIFY: NoteStatus.ROUTING,
        Stage.DISPATCH: NoteStatus.ROUTED,
    }[stage]


def recover_expired_claims(now: datetime) -> None:
    """Return interrupted claims to pending or mark exhausted notes as failed.

    Parameters
    ----------
    now : datetime
        Current time as naive UTC or timezone-aware UTC.
    """
    expired_lease = or_(
        columns.lease_expires_at.is_(None),
        columns.lease_expires_at <= now,
    )
    with Session(engine) as session:
        interrupted = session.exec(
            select(Note).where(
                columns.status == NoteStatus.ROUTING,
                expired_lease,
            )
        ).all()
        dispatches = session.exec(
            select(Note).where(
                columns.status == NoteStatus.ROUTED,
                columns.lease_expires_at.is_not(None),
                columns.lease_expires_at <= now,
            )
        ).all()

        for note in [*interrupted, *dispatches]:
            stage = (
                Stage.CLASSIFY if note.status == NoteStatus.ROUTING else Stage.DISPATCH
            )

            note.lease_expires_at = None
            if note.attempt_count >= MAX_ATTEMPTS:
                note.status = NoteStatus.ERROR
                note.next_attempt_at = None
                note.error_message = (
                    f"{stage.value} interrupted after {note.attempt_count} attempts"
                )
            else:
                note.status = _ready_status(stage)
                note.next_attempt_at = now + timedelta(
                    seconds=_retry_delay(note.attempt_count)
                )
                note.error_message = f"{stage.value} interrupted; retry scheduled"
            session.add(note)
            if note.status == NoteStatus.ERROR:
                draft = failure_notification(
                    note.id,
                    stage.value,
                    note.attempt_count,
                    "WorkerInterrupted",
                )
            else:
                draft = recovery_notification(note.id, stage.value, note.attempt_count)
            enqueue_notification(session, note.id, draft)

        if interrupted or dispatches:
            session.commit()


def claim_next_note(now: datetime) -> ClaimedNote | None:
    """Atomically lease the oldest note whose next stage is ready.

    Parameters
    ----------
    now : datetime
        Current time as naive UTC or timezone-aware UTC.

    Returns
    -------
    ClaimedNote or None
        Claim details, or ``None`` when no note is eligible.
    """
    eligible = or_(columns.next_attempt_at.is_(None), columns.next_attempt_at <= now)
    with Session(engine) as session:
        note = session.exec(
            select(Note)
            .where(
                columns.status.in_([NoteStatus.TRANSCRIBED, NoteStatus.ROUTED]),
                eligible,
                columns.lease_expires_at.is_(None),
            )
            .order_by(columns.created_at)
            .limit(1)
        ).first()
        if note is None:
            return None

        ready_status = note.status
        stage = (
            Stage.CLASSIFY if ready_status == NoteStatus.TRANSCRIBED else Stage.DISPATCH
        )
        claimed_status = _running_status(stage)
        lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        result = session.exec(
            update(Note)
            .where(
                columns.id == note.id,
                columns.status == ready_status,
                eligible,
                columns.lease_expires_at.is_(None),
            )
            .values(
                status=claimed_status,
                attempt_count=columns.attempt_count + 1,
                lease_expires_at=lease_expires_at,
                error_message=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            session.rollback()
            return None
        enqueue_notification(
            session,
            note.id,
            stage_claim_notification(note.id, stage.value, note.attempt_count + 1),
        )
        session.commit()
        session.refresh(note)
        return ClaimedNote(note=note, stage=stage, claimed_status=claimed_status)


def complete_stage(
    claim: ClaimedNote,
    *,
    notification: NotificationDraft | None = None,
    **changes: object,
) -> None:
    """Persist stage output only while this worker still owns the lease.

    Parameters
    ----------
    claim : ClaimedNote
        Claim whose status and lease must still match the database row.
    notification : NotificationDraft or None, optional
        Lifecycle event to enqueue atomically with the stage update.
    **changes : object
        Note fields to update with the completed stage's output and status.
    """
    with Session(engine) as session:
        result = session.exec(
            update(Note)
            .where(
                columns.id == claim.note.id,
                columns.status == claim.claimed_status,
                columns.lease_expires_at == claim.note.lease_expires_at,
            )
            .values(
                **changes,
                attempt_count=0,
                next_attempt_at=None,
                lease_expires_at=None,
                error_message=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            logger.warning(
                "Discarding stale %s result for note %s",
                claim.stage.value,
                claim.note.id,
            )
            session.rollback()
            return
        if notification is not None:
            enqueue_notification(session, claim.note.id, notification)
        session.commit()


def record_failure(
    claim: ClaimedNote,
    now: datetime,
    exc: Exception,
    *,
    retryable: bool = True,
) -> None:
    """Schedule a retry or mark the note terminal after its final attempt.

    Parameters
    ----------
    claim : ClaimedNote
        Claim associated with the failed stage.
    now : datetime
        Failure time as naive UTC or timezone-aware UTC.
    exc : Exception
        Exception raised while processing the stage. Only its type is stored.
    retryable : bool, optional
        Whether a transient failure should be retried. Permanent failures are
        marked terminal immediately.
    """
    attempt_count = claim.note.attempt_count
    if not retryable:
        status = NoteStatus.ERROR
        next_attempt_at = None
        error_message = f"{claim.stage.value} is not implemented ({type(exc).__name__})"
    elif attempt_count >= MAX_ATTEMPTS:
        status = NoteStatus.ERROR
        next_attempt_at = None
        error_message = (
            f"{claim.stage.value} failed after {attempt_count} attempts "
            f"({type(exc).__name__})"
        )
    else:
        status = _ready_status(claim.stage)
        next_attempt_at = now + timedelta(seconds=_retry_delay(attempt_count))
        error_message = (
            f"{claim.stage.value} failed ({type(exc).__name__}); "
            f"retry {attempt_count}/{MAX_ATTEMPTS} scheduled"
        )

    with Session(engine) as session:
        result = session.exec(
            update(Note)
            .where(
                columns.id == claim.note.id,
                columns.status == claim.claimed_status,
                columns.lease_expires_at == claim.note.lease_expires_at,
            )
            .values(
                status=status,
                next_attempt_at=next_attempt_at,
                lease_expires_at=None,
                error_message=error_message,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            logger.warning(
                "Discarding stale %s failure for note %s",
                claim.stage.value,
                claim.note.id,
            )
            session.rollback()
            return
        if status is NoteStatus.ERROR:
            draft = failure_notification(
                claim.note.id,
                claim.stage.value,
                attempt_count,
                type(exc).__name__,
            )
        else:
            draft = retry_notification(
                claim.note.id,
                claim.stage.value,
                attempt_count,
                type(exc).__name__,
            )
        enqueue_notification(session, claim.note.id, draft)
        session.commit()
