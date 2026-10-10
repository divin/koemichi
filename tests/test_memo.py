"""Tests for deterministic memo generation and NoteDiscovery writes."""

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
) -> Note:
    """Build a persisted memo note snapshot."""
    return Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=status,
        lease_expires_at=lease,
        recorded_at_ms=1_735_689_600_000,
        transcript="A transcript about the bakery opening.",
        intent="memo",
    )


def test_memo_draft_schema_rejects_empty_output() -> None:
    """Require a useful generated title and body before any side effects."""
    with pytest.raises(ValueError):
        memo.MemoDraft.model_validate({"title": "", "content": ""})
    with pytest.raises(ValueError):
        memo.MemoDraft.model_validate(
            {"title": "Title", "content": "Body", "path": "Memo/unsafe.md"}
        )


def test_memo_draft_notification_is_debug_only_and_safe() -> None:
    """Memo progress notifications contain no transcript or generated content."""
    note_id = uuid4()
    notification = memo_draft_notification(note_id)

    assert notification.event_key == f"{note_id}:memo-draft"
    assert notification.debug_only is True
    assert notification.priority == -1
    assert note_id.hex[:8] in notification.message


def test_recording_date_uses_configured_timezone() -> None:
    """Convert UTC recording instants to the intended local calendar day."""
    timestamp_ms = int(datetime(2025, 1, 1, 0, 30, tzinfo=UTC).timestamp() * 1000)

    assert memo.local_recording_date(
        timestamp_ms, ZoneInfo("America/Los_Angeles")
    ) == date(2024, 12, 31)


def test_memo_path_slugifies_title_and_falls_back_when_slug_is_empty() -> None:
    """Build safe dated paths from agent titles, including Unicode-only titles."""
    timezone = ZoneInfo("UTC")
    assert memo.memo_note_path(1_735_689_600_000, "Café & Bakery!", timezone) == (
        "Memo/2025-01-01-cafe-bakery.md"
    )
    assert memo.slugify_title("東京") == "memo"


@pytest.mark.asyncio
async def test_path_selection_adds_numeric_suffixes() -> None:
    """Choose the first path not already present in NoteDiscovery."""

    class ExistingNotes:
        async def note_exists(self, note_path: str) -> bool:
            return note_path.endswith(".md") and "(2)" not in note_path

    path = await memo.choose_available_path(
        ExistingNotes(),
        1_735_689_600_000,
        "Bakery",
        ZoneInfo("UTC"),  # type: ignore[arg-type]
    )

    assert path == "Memo/2025-01-01-bakery (2).md"


@pytest.mark.asyncio
async def test_handle_memo_persists_draft_then_writes_deterministically(
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Persist the agent result before folder/note writes and avoid logging text."""
    note = _note()
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    class FakeAgent:
        async def run(self, transcript: str) -> SimpleNamespace:
            assert transcript == "A transcript about the bakery opening."
            return SimpleNamespace(
                output=memo.MemoDraft(
                    title="Bakery opening",
                    content="The bakery is opening next week.",
                )
            )

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

    with Session(database_engine) as session:
        saved = session.get(Note, note.id)
    assert saved is not None
    assert saved.memo_title == "Bakery opening"
    assert saved.memo_content == "The bakery is opening next week."
    assert saved.memo_path == "Memo/2025-01-01-bakery-opening.md"
    assert calls == [
        ("exists", "Memo/2025-01-01-bakery-opening.md"),
        ("folder", "Memo"),
        (
            "write",
            "Memo/2025-01-01-bakery-opening.md:The bakery is opening next week.",
        ),
    ]
    assert note.transcript is not None
    assert note.transcript not in caplog.text


@pytest.mark.asyncio
async def test_retry_reuses_persisted_draft_without_calling_agent(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry the same NoteDiscovery path and content without regenerating."""
    note = _note()
    note.memo_title = "Saved title"
    note.memo_content = "Saved memo body"
    note.memo_path = "Memo/2025-01-01-saved-title.md"
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
            assert path == "Memo/2025-01-01-saved-title.md"
            assert content == "Saved memo body"
            return {"success": True}

    def unexpected_agent() -> object:
        raise AssertionError("retry must not regenerate the memo")

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
            "Title",
            "Content",
            "Memo/2025-01-01-title.md",
        )
