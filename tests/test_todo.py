"""Tests for todo extraction and deterministic Vikunja inbox writes."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("LLM_URL", "http://localhost:8080/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")
os.environ.setdefault("NOTIFICATION_MODE", "off")

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from koemichi.services.workflows import todo
from koemichi.shared.models.note import Note, NoteSource, NoteStatus
from koemichi.shared.notifications import todo_draft_notification


@pytest.fixture
def database_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    """Provide an isolated database for persisted todo draft tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'todo.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(todo, "engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _note(transcript: str = "Todo: buy milk at the corner shop by Friday.") -> Note:
    """Build a persisted todo note snapshot."""
    return Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=NoteStatus.ROUTED,
        lease_expires_at=datetime(2025, 1, 1, tzinfo=UTC),
        recorded_at_ms=1_735_689_600_000,
        transcript=transcript,
        intent="todo",
    )


def test_todo_prompt_requires_explicit_due_date_only() -> None:
    """Keep the specialist from inventing deadlines or task facts."""
    prompt = todo.TODO_SYSTEM_PROMPT.lower()

    assert "only when the transcript explicitly states" in prompt
    assert "never infer, compute, or guess a due date" in prompt
    assert "list_tasks" in prompt
    assert "add_task" in prompt


def test_task_draft_rejects_empty_or_extra_output() -> None:
    """Validate the three fields the todo specialist is allowed to return."""
    with pytest.raises(ValueError):
        todo.TaskDraft.model_validate({"title": "  "})
    with pytest.raises(ValueError):
        todo.TaskDraft.model_validate({"title": "Buy milk", "project_id": 7})


def test_task_draft_normalizes_and_validates_due_date() -> None:
    """Accept concrete ISO dates and reject vague or invalid deadlines."""
    draft = todo.TaskDraft.model_validate(
        {
            "title": "Buy milk",
            "description": "At the corner shop",
            "due_date": "2026-03-14",
        }
    )
    assert draft.due_date == "2026-03-14T00:00:00Z"

    with pytest.raises(ValueError, match="due_date"):
        todo.TaskDraft.model_validate({"title": "Buy milk", "due_date": "tomorrow"})


def test_task_title_key_normalizes_titles_for_duplicate_check() -> None:
    """Compare duplicate task titles case-insensitively and by collapsed spaces."""
    assert todo.task_title_key("  Buy   MILK ") == "buy milk"
    assert todo.task_title_key("Buy milk") == "buy milk"


def test_todo_draft_notification_is_debug_only_and_safe() -> None:
    """Todo progress notifications contain no transcript or task details."""
    note_id = uuid4()
    notification = todo_draft_notification(note_id)

    assert notification.event_key == f"{note_id}:todo-draft"
    assert notification.debug_only is True
    assert notification.priority == -1
    assert note_id.hex[:8] in notification.message


@pytest.mark.asyncio
async def test_task_title_exists_paginates_until_match() -> None:
    """Walk the inbox pages and stop as soon as a matching title appears."""

    class Inbox:
        def __init__(self) -> None:
            self.pages: list[dict[str, object]] = [
                {"items": [{"title": "Alpha"}], "page": 1, "total_pages": 2},
                {"items": [{"title": "Buy Milk"}], "page": 2, "total_pages": 2},
            ]

        async def list_tasks(self, *, page: int = 1, per_page: int | None = None):
            return self.pages[page - 1]

    inbox = Inbox()
    assert await todo.task_title_exists(inbox, "  buy  milk ") is True
    assert isinstance(inbox, Inbox)
    assert len(inbox.pages) == 2  # full walk is exercised in the second test


@pytest.mark.asyncio
async def test_task_title_exists_full_walk_without_match() -> None:
    """Verify the helper finishes when no page contains the title."""

    class Inbox:
        def __init__(self) -> None:
            self.requests: list[int] = []

        async def list_tasks(self, *, page: int = 1, per_page: int | None = None):
            self.requests.append(page)
            if page == 1:
                return {"items": [{"title": "Alpha"}], "page": 1, "total_pages": 3}
            return {"items": [], "page": page, "total_pages": 3}

    inbox = Inbox()
    assert await todo.task_title_exists(inbox, "buy milk") is False
    assert inbox.requests == [1, 2, 3]


@pytest.mark.asyncio
async def test_handle_todo_persists_draft_and_creates_task(
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Let the agent define the task, then persist before writing to Vikunja."""
    raw_transcript = "  Todo: buy milk at the corner shop  "
    note = _note(transcript=raw_transcript)
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    class FakeAgent:
        async def run(self, transcript: str) -> SimpleNamespace:
            assert transcript == raw_transcript.strip()
            return SimpleNamespace(
                output=todo.TaskDraft(
                    title="Buy milk",
                    description="At the corner shop",
                    due_date="2026-03-14",
                )
            )

    calls: list[tuple[object, ...]] = []

    class FakeClient:
        async def list_tasks(
            self, *, page: int = 1, per_page: int | None = None
        ) -> dict[str, object]:
            calls.append(("list", page))
            return {"items": [], "page": page, "total_pages": 1}

        async def add_task(
            self,
            title: str,
            *,
            description: str | None = None,
            due_date: str | None = None,
        ) -> dict[str, object]:
            calls.append(("add", title, description, due_date))
            return {"id": 99, "title": title}

    monkeypatch.setattr(todo, "_get_todo_agent", lambda: FakeAgent())  # type: ignore[assignment]
    monkeypatch.setattr(todo, "get_configured_client", lambda: FakeClient())
    monkeypatch.setattr(todo, "validate_todo_config", lambda: None)

    await todo.handle_todo(note)

    with Session(database_engine) as session:
        saved = session.get(Note, note.id)
    assert saved is not None
    assert saved.todo_title == "Buy milk"
    assert saved.todo_description == "At the corner shop"
    assert saved.todo_due_date == "2026-03-14T00:00:00Z"
    assert calls == [
        ("list", 1),
        ("add", "Buy milk", "At the corner shop", "2026-03-14T00:00:00Z"),
    ]
    assert raw_transcript not in caplog.text


@pytest.mark.asyncio
async def test_retry_reuses_persisted_draft_without_agent_or_create(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries reuse the persisted task and never create or extract again."""
    note = _note()
    note.todo_title = "Buy milk"
    note.todo_description = None
    note.todo_due_date = None
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    class FakeClient:
        async def list_tasks(
            self, *, page: int = 1, per_page: int | None = None
        ) -> dict[str, object]:
            return {
                "items": [{"title": "Buy milk"}],
                "page": 1,
                "total_pages": 1,
            }

        async def add_task(
            self,
            title: str,
            *,
            description: str | None = None,
            due_date: str | None = None,
        ) -> dict[str, object]:
            raise AssertionError("retry must not create an existing task")

    def unexpected_agent() -> object:
        raise AssertionError("retry must not re-run the todo specialist")

    monkeypatch.setattr(todo, "_get_todo_agent", unexpected_agent)
    monkeypatch.setattr(todo, "get_configured_client", lambda: FakeClient())
    monkeypatch.setattr(todo, "validate_todo_config", lambda: None)

    await todo.handle_todo(note)


@pytest.mark.asyncio
async def test_handle_todo_creates_when_persisted_task_is_missing(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft whose task never landed in Vikunja is created on the next retry."""
    note = _note()
    note.todo_title = "Pay rent"
    note.todo_description = "End of the month"
    note.todo_due_date = None
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)
    added: list[tuple[object, ...]] = []

    class FakeClient:
        async def list_tasks(
            self, *, page: int = 1, per_page: int | None = None
        ) -> dict[str, object]:
            return {"items": [], "page": 1, "total_pages": 1}

        async def add_task(
            self,
            title: str,
            *,
            description: str | None = None,
            due_date: str | None = None,
        ) -> dict[str, object]:
            added.append((title, description, due_date))
            return {"id": 7, "title": title}

    def unexpected_agent() -> object:
        raise AssertionError("retry must not re-run the todo specialist")

    monkeypatch.setattr(todo, "_get_todo_agent", unexpected_agent)
    monkeypatch.setattr(todo, "get_configured_client", lambda: FakeClient())
    monkeypatch.setattr(todo, "validate_todo_config", lambda: None)

    await todo.handle_todo(note)

    assert added == [("Pay rent", "End of the month", None)]


def test_persist_todo_draft_requires_current_worker_lease(
    database_engine: Engine,
) -> None:
    """Do not persist the extracted task after the worker lease has gone stale."""
    note = _note()
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    note.lease_expires_at = datetime(2025, 1, 2, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="lease may be stale"):
        todo.persist_todo_draft(note, todo.TaskDraft(title="Buy milk"))
