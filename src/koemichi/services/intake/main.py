"""Authenticated receiver for completed transcript payloads."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session

from koemichi.shared.db import close_db, create_db_and_tables, get_session
from koemichi.shared.models.note import Note, NoteSource, NoteStatus
from koemichi.shared.notifications import enqueue_notification, received_notification
from koemichi.shared.settings import TRANSCRIPT_INGEST_TOKEN

bearer = HTTPBearer(auto_error=False)


class TranscriptPayload(BaseModel):
    """Versioned transcript payload accepted for routing."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]

    note_id: UUID
    source: NoteSource

    recorded_at_ms: int
    transcript: str | None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Create the fresh schema on startup and close the pool at shutdown."""
    create_db_and_tables()
    try:
        yield
    finally:
        close_db()


app = FastAPI(
    title="Koemichi transcript receiver",
    description="Internal API accepting completed transcript payloads.",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def health_check() -> dict[str, str]:
    """Respond to service connectivity checks."""
    return {"status": "ok"}


def verify_transcript_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
) -> None:
    """Authenticate the upstream transcript sender."""
    if not TRANSCRIPT_INGEST_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Transcript receiver authentication is not configured",
        )
    if credentials is None or not compare_digest(
        credentials.credentials, TRANSCRIPT_INGEST_TOKEN
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.post("/transcripts", dependencies=[Depends(verify_transcript_token)])
def receive_transcript(
    payload: TranscriptPayload,
    session: Session = Depends(get_session),  # noqa: B008
) -> dict[str, str]:
    """Durably enqueue a transcript for classification, idempotently by note ID."""
    transcript = payload.transcript.strip() if payload.transcript else None
    existing = session.get(Note, payload.note_id)
    if existing is not None:
        if (
            existing.source != payload.source
            or existing.recorded_at_ms != payload.recorded_at_ms
            or existing.transcript != transcript
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="note_id already exists with different transcript data",
            )
        return {"status": "duplicate"}

    note = Note(
        id=payload.note_id,
        source=payload.source,
        status=NoteStatus.TRANSCRIBED,
        recorded_at_ms=payload.recorded_at_ms,
        transcript=transcript,
    )
    session.add(note)
    try:
        session.flush()
        enqueue_notification(
            session,
            payload.note_id,
            received_notification(payload.note_id, payload.source),
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return {"status": "accepted"}
