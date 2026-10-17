"""Deterministic, append-only NoteDiscovery journal workflow."""

import logging
from datetime import date
from typing import Any, Protocol, cast
from uuid import UUID

import httpx
from sqlalchemy import update
from sqlmodel import Session

from koemichi.integrations.notediscovery import JsonResponse, NoteDiscoveryClient
from koemichi.services.workflows.dates import local_recording_date
from koemichi.shared.db import engine
from koemichi.shared.models.note import Note, NoteStatus
from koemichi.shared.notifications import (
    enqueue_notification,
    journal_draft_notification,
)
from koemichi.shared.settings import (
    NOTEDISCOVERY_API_URL,
    NOTEDISCOVERY_JOURNAL_FOLDER,
    TIMEZONE,
)

logger = logging.getLogger(__name__)
columns = cast(Any, Note).__table__.c
JOURNAL_FOLDER = NOTEDISCOVERY_JOURNAL_FOLDER


class JournalNoteClient(Protocol):
    """NoteDiscovery operations used by the deterministic journal workflow."""

    async def create_folder(self, folder_path: str) -> JsonResponse:
        """Ensure a vault folder exists."""

    async def create_or_update_note(self, note_path: str, content: str) -> JsonResponse:
        """Create the initial dated journal note if it does not exist."""

    async def get_note(
        self, note_path: str, *, include_backlinks: bool = True
    ) -> JsonResponse:
        """Read the current journal note."""

    async def append_to_note(
        self, note_path: str, content: str, *, add_timestamp: bool = False
    ) -> JsonResponse:
        """Append an entry to the journal note."""


def validate_journal_config() -> str:
    """Validate and return NoteDiscovery's base URL for the journal workflow."""
    if not NOTEDISCOVERY_API_URL:
        raise RuntimeError("Journal workflow config missing: NOTEDISCOVERY_API_URL")
    return NOTEDISCOVERY_API_URL


def journal_path_for_date(day: date) -> str:
    """Build the standard year/month/date path for a journal entry."""
    return f"{JOURNAL_FOLDER}/{day:%Y}/{day:%m}/{day:%Y-%m-%d}.md"


def journal_entry_marker(note_id: UUID) -> str:
    """Return the hidden, stable marker used to make appends retry-safe."""
    return f"<!-- koemichi-entry:{note_id} -->"


def journal_append_content(note_id: UUID, transcript: str) -> str:
    """Format a raw transcript as one idempotently identifiable Markdown entry."""
    return f"\n\n{journal_entry_marker(note_id)}\n{transcript.strip()}"


def persist_journal_draft(note: Note, content: str, path: str) -> None:
    """Persist transcript and destination only while the worker owns the lease."""
    with Session(engine) as session:
        result = session.exec(
            update(Note)
            .where(
                columns.id == note.id,
                columns.status == NoteStatus.ROUTED,
                columns.lease_expires_at == note.lease_expires_at,
                columns.journal_content.is_(None),
                columns.journal_path.is_(None),
            )
            .values(journal_content=content, journal_path=path)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise RuntimeError(
                "Journal draft was not persisted; worker lease may be stale"
            )
        enqueue_notification(session, note.id, journal_draft_notification(note.id))
        session.commit()


async def append_journal_entry(
    client: JournalNoteClient,
    note_id: UUID,
    note_path: str,
    day: date,
    transcript: str,
) -> None:
    """Create the dated note if needed and append one retry-safe transcript."""
    try:
        payload = await client.get_note(note_path, include_backlinks=False)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        await client.create_or_update_note(note_path, f"# {day:%Y-%m-%d}")
        payload = await client.get_note(note_path, include_backlinks=False)

    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise TypeError("NoteDiscovery returned a journal note without text content")

    if journal_entry_marker(note_id) in payload["content"]:
        logger.info("Journal entry already present for note %s", note_id)
        return

    await client.append_to_note(
        note_path,
        journal_append_content(note_id, transcript),
        add_timestamp=False,
    )


async def handle_journal(note: Note) -> None:
    """Append the original transcript to its recording-day journal file."""
    if not note.transcript or not note.transcript.strip():
        raise ValueError("Cannot create a journal entry from an empty transcript")
    api_url = validate_journal_config()
    client = NoteDiscoveryClient(api_url)
    logger.info("Journal workflow started for note %s", note.id)

    if note.journal_content is None and note.journal_path is None:
        transcript = note.transcript.strip()
        day = local_recording_date(note.recorded_at_ms, TIMEZONE)
        path = journal_path_for_date(day)
        persist_journal_draft(note, transcript, path)
        note.journal_content = transcript
        note.journal_path = path
        logger.info("Journal transcript and destination persisted for note %s", note.id)
    elif not note.journal_content or not note.journal_path:
        raise RuntimeError("Persisted journal draft is incomplete")

    assert note.journal_content is not None
    assert note.journal_path is not None
    folder_path = note.journal_path.rsplit("/", maxsplit=1)[0]
    await client.create_folder(folder_path)
    logger.info("Journal folders ensured for note %s", note.id)
    await append_journal_entry(
        client,
        note.id,
        note.journal_path,
        local_recording_date(note.recorded_at_ms, TIMEZONE),
        note.journal_content,
    )
    logger.info("Journal entry saved to NoteDiscovery for note %s", note.id)
