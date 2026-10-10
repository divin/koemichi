"""SQLModel schema for transcript records routed by Koemichi."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class NoteSource(StrEnum):
    """Source label carried with a transcript payload."""

    MEMO = "memo"


class NoteStatus(StrEnum):
    """Processing state after transcription is complete."""

    TRANSCRIBED = "transcribed"
    ROUTING = "routing"
    ROUTED = "routed"
    DISPATCHED = "dispatched"
    ERROR = "error"


class Note(SQLModel, table=True):
    """Transcript data and classification/dispatch retry state."""

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    source: NoteSource

    status: NoteStatus = Field(default=NoteStatus.TRANSCRIBED, index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        index=True,
        sa_type=DateTime(timezone=True),
    )
    recorded_at_ms: int
    transcript: str | None = None
    intent: str | None = None
    memo_title: str | None = None
    memo_content: str | None = None
    memo_path: str | None = None
    journal_content: str | None = None
    journal_path: str | None = None
    classification_method: str | None = None
    classification_confidence: float | None = None
    error_message: str | None = None
    attempt_count: int = Field(default=0)
    next_attempt_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    lease_expires_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
