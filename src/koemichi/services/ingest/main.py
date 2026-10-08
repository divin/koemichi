"""FastAPI application and request handlers for the ingest service."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from sqlmodel import Session

from koemichi.shared.db import close_db, create_db_and_tables, get_session
from koemichi.shared.models.note import Note, NoteSource
from koemichi.shared.notifications import (
    enqueue_notification,
    oversized_upload_notification,
    received_notification,
)
from koemichi.shared.settings import AUDIO_STORAGE_ROOT

from .audio import is_same_size, save_memo_audio
from .token import verify_token

logger = logging.getLogger(__name__)
MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Manage resources for the FastAPI application lifecycle.

    Parameters
    ----------
    _ : FastAPI
        Application instance supplied by FastAPI; it is not used directly.

    Yields
    ------
    None
        Control to the application after initializing the database. The
        database engine is disposed when the application shuts down.
    """
    try:
        create_db_and_tables()
        yield
    finally:
        close_db()


app = FastAPI(
    title="Koemichi Ingest Service",
    description="Webhook service for ingesting voice memos from Pebble Index.",
    lifespan=lifespan,
)


def _persist_note(
    session: Session,
    *,
    note_id: UUID,
    source: NoteSource,
    recorded_at: int,
    transcription: str | None,
    audio_file: Path,
) -> None:
    """Persist note metadata and clean up audio if persistence fails.

    Parameters
    ----------
    session : Session
        Database session used to save the note.
    note_id : UUID
        Identifier assigned to the note and its audio file.
    source : NoteSource
        Webhook source that received the recording.
    recorded_at : int
        Recording timestamp in milliseconds since the Unix epoch.
    transcription : str or None
        Transcript supplied by the recording client, if available.
    audio_file : Path
        Path to the saved recording.

    Raises
    ------
    Exception
        Re-raises a database persistence failure after attempting to remove
        the associated audio file.
    """
    audio_key = audio_file.relative_to(AUDIO_STORAGE_ROOT).as_posix()
    note = Note(
        id=note_id,
        source=source,
        recorded_at_ms=recorded_at,
        audio_key=audio_key,
        transcript=transcription,
    )
    try:
        session.add(note)
        enqueue_notification(session, note_id, received_notification(note_id, source))
        session.commit()
    except Exception:
        session.rollback()
        if audio_file is not None:
            try:
                audio_file.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Failed to remove audio file %s after database write failed",
                    audio_file,
                )
        raise

    logger.info("Persisted %s note %s", source.value, note_id)


def _reject_oversized_audio(session: Session, audio: UploadFile) -> JSONResponse | None:
    """Reject audio above the configured limit and queue a rate-limited alert.

    Parameters
    ----------
    session : Session
        Database session used to persist the independent notification event.
    audio : UploadFile
        Uploaded audio whose parsed size is checked.

    Returns
    -------
    JSONResponse or None
        HTTP 413 response for oversized audio, or ``None`` when it is within
        the configured limit.
    """
    if audio.size is None or audio.size <= MAX_AUDIO_SIZE_BYTES:
        return None

    try:
        enqueue_notification(
            session,
            None,
            oversized_upload_notification(audio.size, MAX_AUDIO_SIZE_BYTES),
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not queue oversized-upload notification")

    logger.warning(
        "Rejecting audio upload of %d bytes; configured limit is %d bytes",
        audio.size,
        MAX_AUDIO_SIZE_BYTES,
    )
    return JSONResponse(
        status_code=413,
        content={"detail": "Audio exceeds the configured maximum size"},
    )


@app.post("/memo", dependencies=[Depends(verify_token)])
def receive_memo(
    audio: UploadFile | None = File(None),  # noqa: B008
    transcription: str | None = Form(None),
    recordedAt: int = Form(...),
    client: str = Form(...),
    authorization: str | None = Header(None),
    x_audio_size: int | None = Header(None),
    session: Session = Depends(get_session),  # noqa: B008
) -> JSONResponse:
    """Accept a memo webhook and persist its audio and note record.

    Parameters
    ----------
    audio : UploadFile or None
        Uploaded memo audio; required for a successful request.
    transcription : str or None
        Optional transcript supplied by the client.
    recordedAt : int
        Recording timestamp in milliseconds since the Unix epoch.
    client : str
        Identifier for the submitting client.
    authorization : str or None
        Authorization header. Authentication is enforced by ``verify_token``.
    x_audio_size : int or None
        Optional expected audio size in bytes.
    session : Session
        Database session used to persist the note.

    Returns
    -------
    JSONResponse
        202 when accepted, 400 when the audio size mismatches, or 422 when
        audio is missing.

    Raises
    ------
    HTTPException
        If the Bearer token is missing or invalid.
    OSError
        If the uploaded audio cannot be saved or inspected.
    """
    logger.info(
        "Received /memo from client=%r recordedAt=%d audio=%s",
        client,
        recordedAt,
        audio is not None,
    )

    note_id = uuid4()
    audio_file = None
    if audio is not None:
        oversized_response = _reject_oversized_audio(session, audio)
        if oversized_response is not None:
            return oversized_response
        audio_file = save_memo_audio(recordedAt, audio, note_id)
        logger.info(
            "Saved /memo audio to %s (%d bytes)", audio_file, audio_file.stat().st_size
        )
        if not is_same_size(audio_file, x_audio_size):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "detail": "Audio size mismatch"},
            )
    else:
        logger.warning("Rejecting /memo without audio")
        return JSONResponse(
            status_code=422,
            content={"status": "error", "detail": "Audio is required"},
        )

    _persist_note(
        session,
        note_id=note_id,
        source=NoteSource.MEMO,
        recorded_at=recordedAt,
        transcription=transcription,
        audio_file=audio_file,
    )
    return JSONResponse(status_code=202, content={"status": "accepted"})


@app.post("/action", dependencies=[Depends(verify_token)], deprecated=True)
def receive_action() -> None:
    """Reject use of the deprecated action webhook.

    Raises
    ------
    HTTPException
        Always with HTTP 410 after authentication succeeds.
    """
    raise HTTPException(status_code=410, detail="The /action endpoint is deprecated")
