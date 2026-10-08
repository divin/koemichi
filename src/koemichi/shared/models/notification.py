"""Persistent notification delivery state and notification modes."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class NotificationMode(StrEnum):
    """Verbosity policy for user-facing Pushover notifications.

    Attributes
    ----------
    NORMAL : str
        Send major lifecycle events only.
    DEBUG : str
        Send lifecycle events and low-priority processing diagnostics.
    OFF : str
        Do not enqueue or deliver notifications.
    """

    NORMAL = "normal"
    DEBUG = "debug"
    OFF = "off"


class NotificationStatus(StrEnum):
    """Delivery state of an outbox event.

    Attributes
    ----------
    PENDING : str
        Waiting for delivery or a scheduled retry.
    SENDING : str
        Leased by the notification worker.
    SENT : str
        Accepted by the Pushover API.
    ERROR : str
        Delivery stopped after a permanent error or exhausted retries.
    """

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    ERROR = "error"


class NotificationOutbox(SQLModel, table=True):
    """Durable Pushover message awaiting delivery or retaining its result.

    Attributes
    ----------
    id : UUID
        Stable primary key for this outbox event.
    note_id : UUID or None
        Associated note identifier, or ``None`` for a rejected request that did
        not create a note.
    dedupe_key : str
        Unique event identity used to prevent duplicate enqueueing.
    title : str
        Pushover notification title.
    message : str
        Pushover message body.
    priority : int
        Pushover display priority from -2 through 2.
    status : NotificationStatus
        Current delivery state.
    created_at : datetime
        Time the event was added to the outbox.
    attempt_count : int
        Number of delivery attempts started.
    next_attempt_at : datetime or None
        Earliest time at which another attempt is allowed.
    lease_expires_at : datetime or None
        Expiration time of the active delivery claim.
    sent_at : datetime or None
        Time Pushover accepted the message.
    error_message : str or None
        Sanitized delivery failure summary; never contains credentials.
    """

    __tablename__: Any = "notification_outbox"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    note_id: UUID | None = Field(default=None, foreign_key="note.id", index=True)
    dedupe_key: str = Field(unique=True, index=True, max_length=255)
    title: str = Field(default="Koemichi", max_length=250)
    message: str = Field(max_length=1024)
    priority: int = Field(default=0, ge=-2, le=2)
    status: NotificationStatus = Field(default=NotificationStatus.PENDING, index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        index=True,
        sa_type=DateTime(timezone=True),
    )
    attempt_count: int = Field(default=0)
    next_attempt_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    lease_expires_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    sent_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    error_message: str | None = Field(default=None, max_length=255)
