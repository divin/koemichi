"""Tests for SQLite-backed note claiming, retries, and stage transitions."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("WEBHOOK_TOKEN", "unit-test-token")
os.environ.setdefault("STT_URL", "http://localhost:8080/v1/audio/transcriptions")
os.environ.setdefault("STT_MODEL_NAME", "test-model")
os.environ.setdefault("LLM_URL", "http://localhost:8080/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-llm")
os.environ.setdefault("NOTIFICATION_MODE", "normal")

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from koemichi.services.router.classify import (
    ClassificationMethod,
    ClassificationResult,
)
from koemichi.services.router.intents import Intent
from koemichi.services.worker import queue as worker_queue
from koemichi.services.worker import runner as worker_runner
from koemichi.shared.models.note import Note, NoteSource, NoteStatus
from koemichi.shared.models.notification import NotificationOutbox


@pytest.fixture
def database_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
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
    engine: Engine,
    *,
    status: NoteStatus = NoteStatus.RECEIVED,
    transcript: str | None = None,
    attempt_count: int = 0,
    lease_expires_at: datetime | None = None,
    next_attempt_at: datetime | None = None,
) -> Note:
    note = Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        recorded_at_ms=1_700_000_000_000,
        audio_key="memos/recording.m4a",
        transcript=transcript,
        status=status,
        attempt_count=attempt_count,
        lease_expires_at=lease_expires_at,
        next_attempt_at=next_attempt_at,
    )
    with Session(engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)
    return note


def _get_note(engine: Engine, note_id: object) -> Note:
    with Session(engine) as session:
        note = session.get(Note, note_id)
        assert note is not None
        return note


@pytest.mark.asyncio
async def test_worker_advances_one_durable_stage_at_a_time(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = _create_note(database_engine, transcript="todo buy milk")
    dispatched: list[tuple[object, str | None, Intent]] = []

    async def fake_transcribe_note(queued_note: Note) -> str:
        assert queued_note.id == note.id
        return queued_note.transcript or ""

    async def fake_classify(_: str | None) -> ClassificationResult:
        return ClassificationResult(Intent.TODO, ClassificationMethod.KEYWORD, None)

    async def fake_post_to_dispatch(
        note_id: object, transcript: str | None, intent: Intent
    ) -> None:
        dispatched.append((note_id, transcript, intent))

    monkeypatch.setattr(worker_runner, "transcribe_note", fake_transcribe_note)
    monkeypatch.setattr(worker_runner, "classify_with_details", fake_classify)
    monkeypatch.setattr(worker_runner, "post_to_dispatch", fake_post_to_dispatch)
    now = datetime(2025, 1, 1, tzinfo=UTC)

    assert await worker_runner.process_next_note(now) is True
    assert _get_note(database_engine, note.id).status == NoteStatus.TRANSCRIBED

    assert await worker_runner.process_next_note(now) is True
    classified = _get_note(database_engine, note.id)
    assert classified.status == NoteStatus.ROUTED
    assert classified.intent == Intent.TODO.value

    assert await worker_runner.process_next_note(now) is True
    dispatched_note = _get_note(database_engine, note.id)
    assert dispatched_note.status == NoteStatus.DISPATCHED
    assert dispatched_note.attempt_count == 0
    assert dispatched_note.lease_expires_at is None
    assert dispatched == [(note.id, "todo buy milk", Intent.TODO)]
    with Session(database_engine) as session:
        notifications = session.exec(
            select(NotificationOutbox).where(NotificationOutbox.note_id == note.id)
        ).all()
    assert {event.dedupe_key for event in notifications} == {
        f"{note.id}:transcribed",
        f"{note.id}:dispatched",
    }
    assert _get_note(database_engine, note.id).classification_method == "keyword"
    assert await worker_runner.process_next_note(now) is False


@pytest.mark.asyncio
async def test_transient_failure_is_scheduled_and_retried(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = _create_note(database_engine)
    attempts = 0

    async def sometimes_fails(_: Note) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary ASR failure")
        return "recovered transcript"

    monkeypatch.setattr(worker_runner, "transcribe_note", sometimes_fails)
    start = datetime(2025, 1, 1, tzinfo=UTC)

    assert await worker_runner.process_next_note(start) is True
    failed = _get_note(database_engine, note.id)
    assert failed.status == NoteStatus.RECEIVED
    assert failed.attempt_count == 1
    assert failed.next_attempt_at == start.replace(tzinfo=None) + timedelta(seconds=5)
    assert failed.lease_expires_at is None
    assert "RuntimeError" in (failed.error_message or "")
    assert "temporary ASR failure" not in (failed.error_message or "")

    assert await worker_runner.process_next_note(start + timedelta(seconds=4)) is False
    assert await worker_runner.process_next_note(start + timedelta(seconds=5)) is True

    recovered = _get_note(database_engine, note.id)
    assert recovered.status == NoteStatus.TRANSCRIBED
    assert recovered.transcript == "recovered transcript"
    assert recovered.attempt_count == 0
    assert recovered.next_attempt_at is None
    assert attempts == 2


@pytest.mark.asyncio
async def test_failure_becomes_terminal_after_attempt_limit(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = _create_note(database_engine)

    async def always_fails(_: Note) -> str:
        raise RuntimeError("ASR unavailable")

    monkeypatch.setattr(worker_runner, "transcribe_note", always_fails)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    times = [start, start + timedelta(seconds=5), start + timedelta(seconds=15)]

    for current_time in times:
        assert await worker_runner.process_next_note(current_time) is True

    failed = _get_note(database_engine, note.id)
    assert failed.status == NoteStatus.ERROR
    assert failed.attempt_count == worker_queue.MAX_ATTEMPTS
    assert failed.next_attempt_at is None
    assert failed.lease_expires_at is None
    with Session(database_engine) as session:
        notifications = session.exec(
            select(NotificationOutbox).where(NotificationOutbox.note_id == note.id)
        ).all()
    assert len(notifications) == 1
    assert notifications[0].priority == 1
    assert notifications[0].dedupe_key.endswith(":error:transcription:3")


@pytest.mark.asyncio
async def test_expired_transcription_lease_is_requeued(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    note = _create_note(
        database_engine,
        status=NoteStatus.TRANSCRIBING,
        attempt_count=1,
        lease_expires_at=now - timedelta(seconds=1),
    )

    async def unexpected_transcribe(_: Note) -> str:
        raise AssertionError("retry must wait for its backoff")

    monkeypatch.setattr(worker_runner, "transcribe_note", unexpected_transcribe)

    assert await worker_runner.process_next_note(now) is False
    recovered = _get_note(database_engine, note.id)
    assert recovered.status == NoteStatus.RECEIVED
    assert recovered.lease_expires_at is None
    assert recovered.next_attempt_at == now.replace(tzinfo=None) + timedelta(seconds=5)
    assert "interrupted" in (recovered.error_message or "")


@pytest.mark.asyncio
async def test_dispatch_retry_reuses_note_id_without_reclassifying(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = _create_note(
        database_engine,
        status=NoteStatus.ROUTED,
        transcript="memo about the roof",
    )
    note_id = note.id
    note.intent = Intent.MEMO.value
    with Session(database_engine) as session:
        session.add(note)
        session.commit()

    dispatch_attempts: list[tuple[object, str | None, Intent]] = []

    async def unexpected_classify(_: str | None) -> ClassificationResult:
        raise AssertionError("a saved intent should not be classified again")

    async def fail_once_then_succeed(
        dispatch_note_id: object, transcript: str | None, intent: Intent
    ) -> None:
        dispatch_attempts.append((dispatch_note_id, transcript, intent))
        if len(dispatch_attempts) == 1:
            raise RuntimeError("dispatch webhook unavailable")

    monkeypatch.setattr(worker_runner, "classify_with_details", unexpected_classify)
    monkeypatch.setattr(worker_runner, "post_to_dispatch", fail_once_then_succeed)
    now = datetime(2025, 1, 1, tzinfo=UTC)

    assert await worker_runner.process_next_note(now) is True
    failed = _get_note(database_engine, note_id)
    assert failed.status == NoteStatus.ROUTED
    assert failed.intent == Intent.MEMO.value
    assert failed.attempt_count == 1
    assert failed.next_attempt_at == now.replace(tzinfo=None) + timedelta(seconds=5)

    assert await worker_runner.process_next_note(now + timedelta(seconds=5)) is True
    dispatched = _get_note(database_engine, note_id)
    assert dispatched.status == NoteStatus.DISPATCHED
    assert dispatched.attempt_count == 0
    assert dispatch_attempts == [
        (note_id, "memo about the roof", Intent.MEMO),
        (note_id, "memo about the roof", Intent.MEMO),
    ]
