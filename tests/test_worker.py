"""Tests for classification and dispatch stages in the routing worker."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("TRANSCRIPT_INGEST_TOKEN", "unit-test-ingest-token")
os.environ.setdefault("LLM_URL", "http://localhost:8080/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")
os.environ.setdefault("NOTIFICATION_MODE", "off")

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from koemichi.services.router.classify import (
    ClassificationMethod,
    ClassificationResult,
)
from koemichi.services.router.intents import Intent
from koemichi.services.worker import queue as worker_queue
from koemichi.services.worker import runner as worker_runner
from koemichi.shared.models.note import Note, NoteSource, NoteStatus


@pytest.fixture
def database_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    """Provide an isolated database and redirect the worker queue to it."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'worker.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(worker_queue, "engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _create_note(
    engine: Engine, *, status: NoteStatus = NoteStatus.TRANSCRIBED
) -> Note:
    """Create and persist a note for a routing-worker test."""
    note = Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=status,
        recorded_at_ms=1_700_000_000_000,
        transcript="todo buy milk",
    )
    with Session(engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)
    return note


def _get_note(engine: Engine, note_id: object) -> Note:
    """Return the persisted note with the requested identifier."""
    with Session(engine) as session:
        note = session.get(Note, note_id)
        assert note is not None
        return note


@pytest.mark.asyncio
async def test_worker_classifies_then_dispatches(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the worker classifies a transcript before dispatching it."""
    note = _create_note(database_engine)
    dispatched: list[tuple[object, str | None, Intent]] = []

    async def fake_classify(_: str | None) -> ClassificationResult:
        """Return a deterministic intent classification for the test."""
        return ClassificationResult(Intent.TODO, ClassificationMethod.KEYWORD, None)

    async def fake_execute(note: Note) -> None:
        """Record the saved note routed to its intent executor."""
        assert note.intent is not None
        dispatched.append((note.id, note.transcript, Intent(note.intent)))

    monkeypatch.setattr(worker_runner, "classify_with_details", fake_classify)
    monkeypatch.setattr(worker_runner, "execute_intent", fake_execute)
    now = datetime(2025, 1, 1, tzinfo=UTC)

    assert await worker_runner.process_next_note(now) is True
    classified = _get_note(database_engine, note.id)
    assert classified.status == NoteStatus.ROUTED
    assert classified.intent == Intent.TODO.value
    assert classified.classification_method == "keyword"

    assert await worker_runner.process_next_note(now) is True
    dispatched_note = _get_note(database_engine, note.id)
    assert dispatched_note.status == NoteStatus.DISPATCHED
    assert dispatched_note.attempt_count == 0
    assert dispatched == [(note.id, "todo buy milk", Intent.TODO)]
    assert await worker_runner.process_next_note(now) is False


@pytest.mark.asyncio
async def test_unimplemented_intent_fails_without_retry(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unimplemented intents fail clearly instead of going to a fallback."""
    note = _create_note(database_engine, status=NoteStatus.ROUTED)
    note.intent = Intent.RESEARCH.value
    note_id = note.id
    with Session(database_engine) as session:
        session.add(note)
        session.commit()

    async def unsupported(_: Note) -> None:
        raise worker_runner.UnimplementedIntentError("No Python handler")

    monkeypatch.setattr(worker_runner, "execute_intent", unsupported)
    now = datetime(2025, 1, 1, tzinfo=UTC)

    assert await worker_runner.process_next_note(now) is True
    failed = _get_note(database_engine, note_id)
    assert failed.status is NoteStatus.ERROR
    assert failed.attempt_count == 1
    assert failed.next_attempt_at is None
    assert "not implemented" in (failed.error_message or "")
    assert await worker_runner.process_next_note(now) is False


@pytest.mark.asyncio
async def test_dispatch_retry_does_not_reclassify(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify dispatch retries reuse the saved intent without reclassification."""
    note = _create_note(database_engine, status=NoteStatus.ROUTED)
    note.intent = Intent.MEMO.value
    note_id = note.id
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
    attempts = 0

    async def unexpected_classify(_: str | None) -> ClassificationResult:
        """Fail if a dispatch retry unexpectedly repeats classification."""
        raise AssertionError("saved intent must not be classified again")

    async def fail_once(*_: object) -> None:
        """Fail the first dispatch attempt and accept the next one."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary dispatch failure")

    monkeypatch.setattr(worker_runner, "classify_with_details", unexpected_classify)
    monkeypatch.setattr(worker_runner, "execute_intent", fail_once)
    start = datetime(2025, 1, 1, tzinfo=UTC)

    assert await worker_runner.process_next_note(start) is True
    failed = _get_note(database_engine, note_id)
    assert failed.status == NoteStatus.ROUTED
    assert failed.attempt_count == 1
    assert failed.next_attempt_at == start.replace(tzinfo=None) + timedelta(seconds=5)

    assert await worker_runner.process_next_note(start + timedelta(seconds=5)) is True
    assert _get_note(database_engine, note_id).status == NoteStatus.DISPATCHED
    assert attempts == 2
