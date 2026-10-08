"""SQLModel schema for persisted voice notes."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class NoteSource(StrEnum):
    """Webhook source that received a recording.

    Attributes
    ----------
    MEMO : str
        Memo recording source.
    ACTION : str
        Action recording source.
    """

    MEMO = "memo"
    ACTION = "action"


class NoteStatus(StrEnum):
    """Processing state of a note through the pipeline.

    Attributes
    ----------
    RECEIVED : str
        Note persisted and awaiting transcription.
    TRANSCRIBING : str
        A worker currently holds a transcription lease.
    TRANSCRIBED : str
        Transcription completed; classification is pending.
    ROUTING : str
        A worker currently holds a classification lease.
    ROUTED : str
        Classification completed; dispatch to n8n is pending.
    DISPATCHED : str
        n8n accepted the dispatch request.
    ERROR : str
        Processing stopped after exhausting retries.
    """

    RECEIVED = "received"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    ROUTING = "routing"
    ROUTED = "routed"
    DISPATCHED = "dispatched"
    ERROR = "error"


class Note(SQLModel, table=True):
    """Persisted recording metadata and its processing state.

    ``audio_key`` is relative to the shared storage root, so it remains valid
    when the host-side volume mount changes between environments.

    Attributes
    ----------
    id : UUID
        Stable primary key for the note.
    source : NoteSource
        Webhook that created the note.
    status : NoteStatus
        Current processing state.
    created_at : datetime
        Time the note row was created.
    recorded_at_ms : int
        Recording time in milliseconds since the Unix epoch.
    audio_key : str
        Relative path to the source audio file.
    transcript : str or None
        Transcript supplied by the client or speech-to-text service.
    intent : str or None
        Classified routing intent.
    classification_method : str or None
        Classification path used, such as ``keyword``, ``llm``, or ``empty``.
    classification_confidence : float or None
        LLM-reported confidence, when available.
    error_message : str or None
        Sanitized summary of the most recent processing failure.
    attempt_count : int
        Attempts made for the current processing stage.
    next_attempt_at : datetime or None
        Earliest time at which a failed stage can be retried.
    lease_expires_at : datetime or None
        Expiration time of the current worker claim.
    """

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    source: NoteSource
    status: NoteStatus = Field(default=NoteStatus.RECEIVED, index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        index=True,
        sa_type=DateTime(timezone=True),
    )
    recorded_at_ms: int
    audio_key: str
    transcript: str | None = None
    intent: str | None = None
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
