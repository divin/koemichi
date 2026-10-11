"""Tests for title-only memo generation and deterministic NoteDiscovery writes."""

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

os.environ.setdefault("LLM_URL", "http://localhost:8080/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")
os.environ.setdefault("NOTIFICATION_MODE", "off")

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from koemichi.services.workflows import memo
from koemichi.services.workflows.dates import local_recording_date
from koemichi.shared.models.note import Note, NoteSource, NoteStatus
from koemichi.shared.notifications import memo_draft_notification


@pytest.fixture
def database_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    """Provide an isolated database for persisted memo draft tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'memo.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(memo, "engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _note(
    *,
    status: NoteStatus = NoteStatus.ROUTED,
    lease: datetime | None = datetime(2025, 1, 1, tzinfo=UTC),
    transcript: str = "A transcript about the bakery opening.",
) -> Note:
    """Build a persisted memo note snapshot."""
    return Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=status,
        lease_expires_at=lease,
        recorded_at_ms=1_735_689_600_000,
        transcript=transcript,
        intent="memo",
    )


def test_memo_prompt_requests_only_a_short_descriptive_title() -> None:
    """Keep the specialist away from transcript rewriting or summarization."""
    prompt = memo.MEMO_SYSTEM_PROMPT.lower()

    assert "only a short, descriptive title" in prompt
    assert "do not summarize, rewrite, correct" in prompt
    assert "never translate" in prompt


def test_memo_title_schema_rejects_empty_or_extra_output() -> None:
    """Validate the one field the title specialist is allowed to return."""
    with pytest.raises(ValueError):
        memo.MemoTitle.model_validate({"title": "  "})
    with pytest.raises(ValueError):
        memo.MemoTitle.model_validate({"title": "Bakery", "content": "rewritten"})


def test_recording_datetime_and_pretty_title_use_local_zero_padded_time() -> None:
    """Include local date and zero-padded recording time in the display title."""
    timestamp_ms = int(datetime(2025, 1, 1, 0, 5, tzinfo=UTC).timestamp() * 1000)
    timezone = ZoneInfo("America/Los_Angeles")

    assert local_recording_date(timestamp_ms, timezone) == date(2024, 12, 31)
    assert memo.pretty_memo_title(timestamp_ms, "Bakery opening", timezone) == (
        "2024-12-31 16:05 - Bakery opening"
    )


def test_memo_path_uses_readable_title_and_sanitizes_path_separators() -> None:
    """Keep names human-readable while preventing titles from adding path parts."""
    assert (
        memo.memo_note_path(1_735_689_600_000, "Bakery / Café", ZoneInfo("UTC"))
        == "Memo/2025-01-01 00:00 - Bakery Café.md"
    )
    assert memo.safe_descriptive_title("東京") == "東京"


@pytest.mark.asyncio
async def test_path_selection_adds_numeric_suffixes() -> None:
    """Choose the first readable timestamped path not already in the vault."""

    class ExistingNotes:
        async def note_exists(self, note_path: str) -> bool:
            return note_path.endswith(".md") and "(2)" not in note_path

    path = await memo.choose_available_path(
        ExistingNotes(),
        1_735_689_600_000,
        "Bakery",
        ZoneInfo("UTC"),
    )

    assert path == "Memo/2025-01-01 00:00 - Bakery (2).md"


def test_memo_draft_notification_is_debug_only_and_safe() -> None:
    """Memo progress notifications contain no transcript or generated content."""
    note_id = uuid4()
    notification = memo_draft_notification(note_id)

    assert notification.event_key == f"{note_id}:memo-draft"
    assert notification.debug_only is True
    assert notification.priority == -1
    assert note_id.hex[:8] in notification.message


@pytest.mark.asyncio
async def test_handle_memo_persists_title_and_raw_transcript_before_writing(
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Use the LLM only for a title and save the original transcript unchanged."""
    raw_transcript = "  We open on Friday.\nI said, 'maybe at 8?'  "
    note = _note(transcript=raw_transcript)
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    class FakeAgent:
        async def run(self, transcript: str) -> SimpleNamespace:
            assert transcript == raw_transcript.strip()
            return SimpleNamespace(output=memo.MemoTitle(title="Bakery opening"))

    calls: list[tuple[str, str | None]] = []

    class FakeClient:
        def __init__(self, _: str) -> None:
            pass

        async def note_exists(self, path: str) -> bool:
            calls.append(("exists", path))
            return False

        async def create_folder(self, path: str) -> dict[str, object]:
            calls.append(("folder", path))
            return {"success": True}

        async def create_or_update_note(
            self, path: str, content: str
        ) -> dict[str, object]:
            with Session(database_engine) as session:
                saved = session.get(Note, note.id)
                assert saved is not None
                assert saved.memo_path == path
                assert saved.memo_content == content
            calls.append(("write", f"{path}:{content}"))
            return {"success": True}

    monkeypatch.setattr(memo, "_get_memo_agent", lambda: FakeAgent())  # type: ignore[assignment]
    monkeypatch.setattr(memo, "NoteDiscoveryClient", FakeClient)
    monkeypatch.setattr(memo, "NOTEDISCOVERY_API_URL", "http://notes.test")
    monkeypatch.setattr(memo, "TIMEZONE", ZoneInfo("UTC"))

    await memo.handle_memo(note)

    pretty_title = "2025-01-01 00:00 - Bakery opening"
    expected_path = f"Memo/{pretty_title}.md"
    expected_content = f"# {pretty_title}\n\n{raw_transcript.strip()}"
    with Session(database_engine) as session:
        saved = session.get(Note, note.id)
    assert saved is not None
    assert saved.memo_title == pretty_title
    assert saved.memo_content == expected_content
    assert saved.memo_path == expected_path
    assert calls == [
        ("exists", expected_path),
        ("folder", "Memo"),
        ("write", f"{expected_path}:{expected_content}"),
    ]
    assert raw_transcript not in caplog.text


@pytest.mark.asyncio
async def test_retry_reuses_persisted_memo_without_calling_agent(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries write the exact persisted title, content, and destination."""
    note = _note()
    note.memo_title = "2025-01-01 00:00 - Saved title"
    note.memo_content = f"# {note.memo_title}\n\nSaved raw transcript"
    note.memo_path = f"Memo/{note.memo_title}.md"
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    class FakeClient:
        def __init__(self, _: str) -> None:
            pass

        async def create_folder(self, path: str) -> dict[str, object]:
            assert path == "Memo"
            return {"success": True}

        async def create_or_update_note(
            self, path: str, content: str
        ) -> dict[str, object]:
            assert path == note.memo_path
            assert content == note.memo_content
            return {"success": True}

    def unexpected_agent() -> object:
        raise AssertionError("retry must not regenerate the memo title")

    monkeypatch.setattr(memo, "_get_memo_agent", unexpected_agent)
    monkeypatch.setattr(memo, "NoteDiscoveryClient", FakeClient)
    monkeypatch.setattr(memo, "NOTEDISCOVERY_API_URL", "http://notes.test")

    await memo.handle_memo(note)


def test_persist_memo_draft_requires_current_worker_lease(
    database_engine: Engine,
) -> None:
    """Do not persist generated output after the worker lease has gone stale."""
    note = _note()
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    note.lease_expires_at = datetime(2025, 1, 2, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="lease may be stale"):
        memo.persist_memo_draft(
            note,
            "2025-01-01 00:00 - Title",
            "# Title\n\nTranscript",
            "Memo/2025-01-01 00:00 - Title.md",
        )
