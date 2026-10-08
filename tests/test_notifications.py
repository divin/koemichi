"""Tests for notification policy, outbox retries, and Pushover API handling."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

os.environ.setdefault("WEBHOOK_TOKEN", "unit-test-token")
os.environ.setdefault("STT_URL", "http://localhost:8080/v1/audio/transcriptions")
os.environ.setdefault("STT_MODEL_NAME", "test-model")
os.environ.setdefault("NOTIFICATION_MODE", "normal")

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from koemichi.integrations import pushover
from koemichi.services.worker import queue as worker_queue
from koemichi.services.worker import runner as worker_runner
from koemichi.shared import notifications
from koemichi.shared.models.note import Note, NoteSource
from koemichi.shared.models.notification import (
    NotificationMode,
    NotificationOutbox,
    NotificationStatus,
)


@pytest.fixture
def database_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    """Provide an isolated SQLite database for outbox tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'notifications.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(notifications, "engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _create_note(engine: Engine) -> Note:
    """Insert and return a note associated with test notifications."""
    note = Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        recorded_at_ms=1_700_000_000_000,
        audio_key="memos/notification-test.m4a",
    )
    with Session(engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)
    return note


def _outbox_events(engine: Engine, note_id: object) -> list[NotificationOutbox]:
    """Return every notification event associated with a note."""
    with Session(engine) as session:
        return list(
            session.exec(
                select(NotificationOutbox).where(NotificationOutbox.note_id == note_id)
            ).all()
        )


def test_notification_policy_filters_debug_events(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = _create_note(database_engine)
    received = notifications.received_notification(note.id, note.source)
    claim = notifications.stage_claim_notification(note.id, "transcription", 1)

    monkeypatch.setattr(notifications, "NOTIFICATION_MODE", NotificationMode.NORMAL)
    with Session(database_engine) as session:
        notifications.enqueue_notification(session, note.id, received)
        notifications.enqueue_notification(session, note.id, claim)
        session.commit()

    normal_events = _outbox_events(database_engine, note.id)
    assert len(normal_events) == 1
    assert normal_events[0].priority == 0

    monkeypatch.setattr(notifications, "NOTIFICATION_MODE", NotificationMode.DEBUG)
    with Session(database_engine) as session:
        notifications.enqueue_notification(session, note.id, claim)
        session.commit()

    debug_events = _outbox_events(database_engine, note.id)
    assert len(debug_events) == 2
    assert any(event.priority == -1 for event in debug_events)
    assert any("transcription" in event.message for event in debug_events)


def test_debug_detail_messages_use_low_priority_and_errors_stay_urgent(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    normal_note = _create_note(database_engine)
    debug_note = _create_note(database_engine)
    failed_note = _create_note(database_engine)

    monkeypatch.setattr(notifications, "NOTIFICATION_MODE", NotificationMode.NORMAL)
    with Session(database_engine) as session:
        notifications.enqueue_notification(
            session,
            normal_note.id,
            notifications.transcribed_notification(normal_note.id, 2.5, "test-model"),
        )
        session.commit()

    normal_event = _outbox_events(database_engine, normal_note.id)[0]
    assert normal_event.priority == 0
    assert normal_event.message == f"Voice note transcribed [{normal_note.id.hex[:8]}]."

    monkeypatch.setattr(notifications, "NOTIFICATION_MODE", NotificationMode.DEBUG)
    with Session(database_engine) as session:
        notifications.enqueue_notification(
            session,
            debug_note.id,
            notifications.transcribed_notification(debug_note.id, 2.5, "test-model"),
        )
        notifications.enqueue_notification(
            session,
            failed_note.id,
            notifications.failure_notification(
                failed_note.id, "dispatch", 3, "RuntimeError"
            ),
        )
        session.commit()

    debug_event = _outbox_events(database_engine, debug_note.id)[0]
    assert debug_event.priority == -1
    assert "in 2.5s using test-model" in debug_event.message

    failure_event = _outbox_events(database_engine, failed_note.id)[0]
    assert failure_event.priority == 1
    assert "after 3 attempts (RuntimeError)" in failure_event.message


def test_off_mode_does_not_enqueue_notifications(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = _create_note(database_engine)
    monkeypatch.setattr(notifications, "NOTIFICATION_MODE", NotificationMode.OFF)

    with Session(database_engine) as session:
        notifications.enqueue_notification(
            session, note.id, notifications.received_notification(note.id, note.source)
        )
        session.commit()

    assert _outbox_events(database_engine, note.id) == []


def test_transient_delivery_failure_retries_and_permanent_failure_stops(
    database_engine: Engine,
) -> None:
    note = _create_note(database_engine)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    with Session(database_engine) as session:
        notifications.enqueue_notification(
            session, note.id, notifications.received_notification(note.id, note.source)
        )
        session.commit()

    first = notifications.claim_next_notification(start)
    assert first is not None
    assert first.status == NotificationStatus.SENDING
    assert first.attempt_count == 1
    notifications.fail_notification(
        first,
        start + timedelta(seconds=1),
        "Pushover server error (HTTP 503)",
        retryable=True,
    )
    scheduled = _outbox_events(database_engine, note.id)[0]
    assert scheduled.status == NotificationStatus.PENDING
    assert scheduled.next_attempt_at == start.replace(tzinfo=None) + timedelta(
        seconds=6
    )
    assert notifications.claim_next_notification(start + timedelta(seconds=5)) is None

    second = notifications.claim_next_notification(start + timedelta(seconds=6))
    assert second is not None
    assert second.attempt_count == 2
    notifications.fail_notification(
        second,
        start + timedelta(seconds=6),
        "Pushover rejected the request (HTTP 400)",
        retryable=False,
    )
    failed = _outbox_events(database_engine, note.id)[0]
    assert failed.status == NotificationStatus.ERROR
    assert failed.next_attempt_at is None
    assert failed.lease_expires_at is None


def test_expired_notification_claim_is_recovered(
    database_engine: Engine,
) -> None:
    note = _create_note(database_engine)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    with Session(database_engine) as session:
        notifications.enqueue_notification(
            session, note.id, notifications.received_notification(note.id, note.source)
        )
        session.commit()

    claimed = notifications.claim_next_notification(start)
    assert claimed is not None
    notifications.recover_expired_notifications(
        start + timedelta(seconds=notifications.OUTBOX_LEASE_SECONDS + 1)
    )

    recovered = _outbox_events(database_engine, note.id)[0]
    assert recovered.status == NotificationStatus.PENDING
    assert recovered.lease_expires_at is None
    assert recovered.next_attempt_at == start.replace(tzinfo=None) + timedelta(
        seconds=notifications.OUTBOX_LEASE_SECONDS
        + 1
        + notifications.OUTBOX_BASE_RETRY_SECONDS
    )


@pytest.mark.asyncio
async def test_delivery_failure_does_not_block_note_processing(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = _create_note(database_engine)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    with Session(database_engine) as session:
        notifications.enqueue_notification(
            session, note.id, notifications.received_notification(note.id, note.source)
        )
        session.commit()
    monkeypatch.setattr(worker_queue, "engine", database_engine)

    async def fail_send(_: str, __: str, ___: int) -> None:
        raise pushover.PushoverError("Pushover server error (HTTP 503)", retryable=True)

    monkeypatch.setattr(worker_runner, "send_message", fail_send)
    assert await worker_runner.process_next_notification(start) is True
    failed_attempt = _outbox_events(database_engine, note.id)[0]
    assert failed_attempt.status == NotificationStatus.PENDING
    assert failed_attempt.next_attempt_at == start.replace(tzinfo=None) + timedelta(
        seconds=notifications.OUTBOX_BASE_RETRY_SECONDS
    )

    async def fake_transcribe(_: Note) -> str:
        return "transcript"

    monkeypatch.setattr(worker_runner, "transcribe_note", fake_transcribe)
    assert await worker_runner.process_next_note(start) is True
    with Session(database_engine) as session:
        persisted_note = session.get(Note, note.id)
    assert persisted_note is not None
    assert persisted_note.transcript == "transcript"

    async def succeed_send(_: str, __: str, ___: int) -> None:
        return None

    monkeypatch.setattr(worker_runner, "send_message", succeed_send)
    assert (
        await worker_runner.process_next_notification(
            start + timedelta(seconds=notifications.OUTBOX_BASE_RETRY_SECONDS)
        )
        is True
    )
    received = next(
        event
        for event in _outbox_events(database_engine, note.id)
        if event.dedupe_key.endswith(":received")
    )
    assert received.status == NotificationStatus.SENT


def test_validate_notification_config_requires_both_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notifications, "NOTIFICATION_MODE", NotificationMode.NORMAL)
    monkeypatch.setattr(notifications, "PUSHOVER_API_TOKEN", "app-token")
    monkeypatch.setattr(notifications, "PUSHOVER_USER_KEY", None)

    with pytest.raises(RuntimeError, match="PUSHOVER_USER_KEY"):
        notifications.validate_notification_config()

    monkeypatch.setattr(notifications, "NOTIFICATION_MODE", NotificationMode.OFF)
    notifications.validate_notification_config()


@pytest.mark.asyncio
async def test_pushover_sends_form_data_and_checks_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": 1, "request": "request-id"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(pushover, "PUSHOVER_API_TOKEN", "application-token")
    monkeypatch.setattr(pushover, "PUSHOVER_USER_KEY", "user-key")
    monkeypatch.setattr(
        pushover.httpx,
        "AsyncClient",
        lambda *, timeout: client_type(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )

    await pushover.send_message("Voice note received", "Koemichi", 0)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url == pushover.PUSHOVER_MESSAGE_URL
    form = parse_qs(requests[0].content.decode())
    assert form == {
        "token": ["application-token"],
        "user": ["user-key"],
        "message": ["Voice note received"],
        "title": ["Koemichi"],
        "priority": ["0"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "retryable"),
    [
        (400, {"status": 0, "errors": ["invalid token"]}, False),
        (429, {"status": 0, "errors": ["rate limit"]}, False),
        (503, {"status": 0}, True),
        (200, {"status": 0, "errors": ["rejected"]}, False),
    ],
)
async def test_pushover_classifies_api_failures(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    body: dict[str, object],
    retryable: bool,
) -> None:
    client_type = httpx.AsyncClient
    monkeypatch.setattr(pushover, "PUSHOVER_API_TOKEN", "application-token")
    monkeypatch.setattr(pushover, "PUSHOVER_USER_KEY", "user-key")
    monkeypatch.setattr(
        pushover.httpx,
        "AsyncClient",
        lambda *, timeout: client_type(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status_code, json=body)
            ),
            timeout=timeout,
        ),
    )

    with pytest.raises(pushover.PushoverError) as error:
        await pushover.send_message("hello", "Koemichi", 0)
    assert error.value.retryable is retryable
    assert "application-token" not in str(error.value)
    assert "user-key" not in str(error.value)
