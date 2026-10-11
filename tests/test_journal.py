"""Tests for raw-transcript journal appends and retry safety."""

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

os.environ.setdefault("NOTIFICATION_MODE", "off")

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from koemichi.services.workflows import journal
from koemichi.shared.models.note import Note, NoteSource, NoteStatus


@pytest.fixture
def database_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    """Provide an isolated database for persisted journal transcript tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'journal.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(journal, "engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _note(transcript: str = "Today I walked to the bakery.") -> Note:
    """Build a persisted-looking journal note snapshot."""
    return Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=NoteStatus.ROUTED,
        lease_expires_at=datetime(2025, 1, 1, tzinfo=UTC),
        recorded_at_ms=1_735_689_600_000,
        transcript=transcript,
        intent="journal",
    )


def _not_found(path: str) -> httpx.HTTPStatusError:
    """Build the HTTP 404 raised by NoteDiscovery for a missing note."""
    request = httpx.Request("GET", f"http://notes.test/api/notes/{path}")
    response = httpx.Response(404, request=request)
    return httpx.HTTPStatusError("not found", request=request, response=response)


def test_journal_path_uses_recording_local_calendar_date() -> None:
    """Use the recording's local day in the year/month/date path."""
    recorded_at_ms = 1_735_689_600_000
    day = journal.local_recording_date(recorded_at_ms, ZoneInfo("America/Los_Angeles"))

    assert day == date(2024, 12, 31)
    assert journal.journal_path_for_date(day) == "Journal/2024/12/2024-12-31.md"


def test_journal_entry_marker_is_stable_and_content_keeps_raw_transcript() -> None:
    """Identify a persisted transcript without rewriting or altering its text."""
    note_id = uuid4()
    transcript = "I said 'maybe'\nthen changed my mind."
    rendered = journal.journal_append_content(note_id, transcript)

    assert journal.journal_entry_marker(note_id) in rendered
    assert rendered.endswith(transcript)
    assert "I said 'maybe'\nthen changed my mind." in rendered


@pytest.mark.asyncio
async def test_handle_journal_creates_date_heading_and_appends_raw_transcript(
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Persist the transcript first and create a dated note without an agent."""
    raw_transcript = "  Heute war ich beim Bäcker.\nDas Brot war gut.  "
    note = _note(raw_transcript)
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    calls: list[tuple[str, str]] = []
    remote: dict[str, str] = {}

    class FakeClient:
        def __init__(self, _: str) -> None:
            pass

        async def create_folder(self, path: str) -> dict[str, object]:
            calls.append(("folder", path))
            return {"success": True}

        async def create_or_update_note(
            self, path: str, content: str
        ) -> dict[str, object]:
            with Session(database_engine) as session:
                saved = session.get(Note, note.id)
                assert saved is not None
                assert saved.journal_path == path
                assert saved.journal_content == raw_transcript.strip()
            remote["content"] = content
            calls.append(("create", content))
            return {"success": True}

        async def get_note(
            self, path: str, *, include_backlinks: bool = True
        ) -> dict[str, str]:
            calls.append(("get", path))
            if "content" not in remote:
                raise _not_found(path)
            return {"path": path, "content": remote["content"]}

        async def append_to_note(
            self, path: str, content: str, *, add_timestamp: bool = False
        ) -> dict[str, object]:
            assert add_timestamp is False
            remote["content"] += content
            calls.append(("append", content))
            return {"success": True}

    monkeypatch.setattr(journal, "NoteDiscoveryClient", FakeClient)
    monkeypatch.setattr(journal, "NOTEDISCOVERY_API_URL", "http://notes.test")
    monkeypatch.setattr(journal, "TIMEZONE", ZoneInfo("UTC"))

    await journal.handle_journal(note)

    path = "Journal/2025/01/2025-01-01.md"
    expected_entry = journal.journal_append_content(note.id, raw_transcript.strip())
    assert note.journal_path == path
    assert note.journal_content == raw_transcript.strip()
    assert remote["content"] == f"# 2025-01-01{expected_entry}"
    assert calls == [
        ("folder", "Journal/2025/01"),
        ("get", path),
        ("create", "# 2025-01-01"),
        ("get", path),
        ("append", expected_entry),
    ]
    assert raw_transcript not in caplog.text


@pytest.mark.asyncio
async def test_existing_journal_is_appended_without_replacing_prior_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Append a recording to today's existing file and preserve prior entries."""
    note_id = uuid4()
    path = "Journal/2025/01/2025-01-01.md"
    previous = "# 2025-01-01\n\nYesterday's carry-over entry."
    remote = {"content": previous}
    appended: list[str] = []

    class ExistingJournal:
        async def get_note(
            self, note_path: str, *, include_backlinks: bool = True
        ) -> dict[str, str]:
            return {"content": remote["content"]}

        async def create_or_update_note(
            self, note_path: str, content: str
        ) -> dict[str, object]:
            raise AssertionError("an existing journal must not be replaced")

        async def append_to_note(
            self, note_path: str, content: str, *, add_timestamp: bool = False
        ) -> dict[str, object]:
            appended.append(content)
            remote["content"] += content
            return {"success": True}

        async def create_folder(self, folder_path: str) -> dict[str, object]:
            return {"success": True}

    transcript = "A new raw transcript."
    await journal.append_journal_entry(
        ExistingJournal(), note_id, path, date(2025, 1, 1), transcript
    )

    assert appended == [journal.journal_append_content(note_id, transcript)]
    assert remote["content"].startswith(previous)
    assert remote["content"].endswith(transcript)


@pytest.mark.asyncio
async def test_journal_retry_does_not_duplicate_successful_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout after an append is safe: retry finds its stable note marker."""
    note_id = uuid4()
    path = "Journal/2025/01/2025-01-01.md"
    remote = {"content": "# 2025-01-01"}
    append_calls = 0

    class ExistingJournal:
        async def get_note(
            self, note_path: str, *, include_backlinks: bool = True
        ) -> dict[str, str]:
            return {"content": remote["content"]}

        async def create_or_update_note(
            self, note_path: str, content: str
        ) -> dict[str, object]:
            raise AssertionError("the existing file should not be replaced")

        async def append_to_note(
            self, note_path: str, content: str, *, add_timestamp: bool = False
        ) -> dict[str, object]:
            nonlocal append_calls
            append_calls += 1
            remote["content"] += content
            if append_calls == 1:
                raise httpx.TimeoutException("response timed out")
            return {"success": True}

        async def create_folder(self, folder_path: str) -> dict[str, object]:
            return {"success": True}

    client = ExistingJournal()
    with pytest.raises(httpx.TimeoutException):
        await journal.append_journal_entry(
            client, note_id, path, date(2025, 1, 1), "Only one copy."
        )

    await journal.append_journal_entry(
        client, note_id, path, date(2025, 1, 1), "Only one copy."
    )

    assert append_calls == 1
    assert remote["content"].count(journal.journal_entry_marker(note_id)) == 1
    assert remote["content"].count("Only one copy.") == 1


def test_persist_journal_draft_requires_current_worker_lease(
    database_engine: Engine,
) -> None:
    """Reject transcript persistence if the worker lease is no longer current."""
    note = _note()
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    note.lease_expires_at = datetime(2025, 1, 2, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="lease may be stale"):
        journal.persist_journal_draft(
            note,
            "Original transcript",
            "Journal/2025/01/2025-01-01.md",
        )
