"""Tests for deterministic journal generation and NoteDiscovery writes."""

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

from koemichi.services.workflows import journal
from koemichi.shared.models.note import Note, NoteSource, NoteStatus


@pytest.fixture
def database_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    """Provide an isolated database for persisted journal draft tests."""
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


def _note() -> Note:
    """Build a persisted-looking journal note snapshot."""
    return Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=NoteStatus.ROUTED,
        lease_expires_at=datetime(2025, 1, 1, tzinfo=UTC),
        recorded_at_ms=1_735_689_600_000,
        transcript="Today I walked to the bakery and enjoyed the sunny weather.",
        intent="journal",
    )


def test_journal_draft_schema_rejects_empty_or_extra_output() -> None:
    """Require a non-empty edited body and prevent agent-chosen file paths."""
    with pytest.raises(ValueError):
        journal.JournalDraft.model_validate({"content": "  "})
    with pytest.raises(ValueError):
        journal.JournalDraft.model_validate(
            {"content": "A journal entry.", "path": "Journal/unsafe.md"}
        )


def test_journal_path_has_zero_padded_year_month_and_date() -> None:
    """Use the agreed deterministic `Journal/YYYY/MM/YYYY-MM-DD.md` layout."""
    assert journal.journal_path_for_date(date(2025, 1, 2)) == (
        "Journal/2025/01/2025-01-02.md"
    )


@pytest.mark.asyncio
async def test_journal_uses_yesterday_unless_it_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefer yesterday; if already present, select today instead."""
    monkeypatch.setattr(journal, "TIMEZONE", ZoneInfo("UTC"))
    yesterday_path = "Journal/2024/12/2024-12-31.md"
    today_path = "Journal/2025/01/2025-01-01.md"

    class ExistingNotes:
        def __init__(self, existing: set[str]) -> None:
            self.existing = existing

        async def note_exists(self, note_path: str) -> bool:
            return note_path in self.existing

    yesterday, path = await journal.choose_journal_path(
        ExistingNotes(set()), 1_735_689_600_000
    )
    assert path == yesterday_path
    assert yesterday.isoformat() == "2024-12-31"

    today, path = await journal.choose_journal_path(
        ExistingNotes({yesterday_path}), 1_735_689_600_000
    )
    assert path == today_path
    assert today.isoformat() == "2025-01-01"


@pytest.mark.asyncio
async def test_journal_collision_creates_numbered_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If today's entry exists too, use a numeric suffix rather than append."""
    monkeypatch.setattr(journal, "TIMEZONE", ZoneInfo("UTC"))
    existing = {
        "Journal/2024/12/2024-12-31.md",
        "Journal/2025/01/2025-01-01.md",
        "Journal/2025/01/2025-01-01 (1).md",
    }

    class ExistingNotes:
        async def note_exists(self, note_path: str) -> bool:
            return note_path in existing

    _, path = await journal.choose_journal_path(ExistingNotes(), 1_735_689_600_000)

    assert path == "Journal/2025/01/2025-01-01 (2).md"


@pytest.mark.asyncio
async def test_handle_journal_persists_content_before_creating_note(
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Add the date heading in code and persist output before API writes."""
    note = _note()
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    class FakeAgent:
        async def run(self, transcript: str) -> SimpleNamespace:
            assert transcript == note.transcript
            return SimpleNamespace(
                output=journal.JournalDraft(content="I enjoyed the sunshine today.")
            )

    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, _: str) -> None:
            pass

        async def note_exists(self, note_path: str) -> bool:
            assert note_path == "Journal/2024/12/2024-12-31.md"
            return False

        async def create_folder(self, folder_path: str) -> dict[str, object]:
            calls.append(("folder", folder_path))
            return {"success": True}

        async def create_or_update_note(
            self, note_path: str, content: str
        ) -> dict[str, object]:
            with Session(database_engine) as session:
                saved = session.get(Note, note.id)
                assert saved is not None
                assert saved.journal_path == note_path
                assert saved.journal_content == content
            calls.append(("write", f"{note_path}:{content}"))
            return {"success": True}

    monkeypatch.setattr(journal, "_get_journal_agent", lambda: FakeAgent())  # type: ignore[assignment]
    monkeypatch.setattr(journal, "NoteDiscoveryClient", FakeClient)
    monkeypatch.setattr(journal, "NOTEDISCOVERY_API_URL", "http://notes.test")
    monkeypatch.setattr(journal, "TIMEZONE", ZoneInfo("UTC"))

    await journal.handle_journal(note)

    with Session(database_engine) as session:
        saved = session.get(Note, note.id)
    assert saved is not None
    assert saved.journal_path == "Journal/2024/12/2024-12-31.md"
    assert saved.journal_content == "# 2024-12-31\n\nI enjoyed the sunshine today."
    assert calls == [
        ("folder", "Journal/2024/12"),
        (
            "write",
            "Journal/2024/12/2024-12-31.md:# 2024-12-31\n\nI enjoyed the sunshine today.",
        ),
    ]
    assert note.transcript is not None
    assert note.transcript not in caplog.text


@pytest.mark.asyncio
async def test_journal_retry_reuses_persisted_draft(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not regenerate content or choose another path on retry."""
    note = _note()
    note.journal_content = "# 2024-12-31\n\nSaved journal entry."
    note.journal_path = "Journal/2024/12/2024-12-31.md"
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    class FakeClient:
        def __init__(self, _: str) -> None:
            pass

        async def create_folder(self, folder_path: str) -> dict[str, object]:
            assert folder_path == "Journal/2024/12"
            return {"success": True}

        async def create_or_update_note(
            self, note_path: str, content: str
        ) -> dict[str, object]:
            assert note_path == "Journal/2024/12/2024-12-31.md"
            assert content == "# 2024-12-31\n\nSaved journal entry."
            return {"success": True}

    def unexpected_agent() -> object:
        raise AssertionError("journal retry must reuse the persisted draft")

    monkeypatch.setattr(journal, "_get_journal_agent", unexpected_agent)
    monkeypatch.setattr(journal, "NoteDiscoveryClient", FakeClient)
    monkeypatch.setattr(journal, "NOTEDISCOVERY_API_URL", "http://notes.test")

    await journal.handle_journal(note)


def test_persist_journal_draft_requires_current_worker_lease(
    database_engine: Engine,
) -> None:
    """Reject generated output if a worker no longer owns the processing lease."""
    note = _note()
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    note.lease_expires_at = datetime(2025, 1, 2, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="lease may be stale"):
        journal.persist_journal_draft(
            note,
            "# 2024-12-31\n\nEntry",
            "Journal/2024/12/2024-12-31.md",
        )
