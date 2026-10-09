"""Tests for the ingest webhook endpoints."""

import os
from collections.abc import Iterator
from pathlib import Path

# Settings are read when the application modules are imported.
os.environ.setdefault("WEBHOOK_TOKEN", "unit-test-token")
os.environ.setdefault("STT_URL", "http://localhost:8080/v1/audio/transcriptions")
os.environ.setdefault("STT_MODEL_NAME", "test-model")
os.environ.setdefault("NOTIFICATION_MODE", "normal")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from koemichi.services.ingest import audio as ingest_audio
from koemichi.services.ingest import main as ingest_main
from koemichi.shared.db import get_session
from koemichi.shared.models.note import Note, NoteSource, NoteStatus
from koemichi.shared.models.notification import NotificationOutbox, NotificationStatus
from koemichi.shared.settings import WEBHOOK_TOKEN


@pytest.fixture
def database_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "audio"
    monkeypatch.setattr(ingest_main, "AUDIO_STORAGE_ROOT", root)
    monkeypatch.setattr(ingest_audio, "MEMO_SAVE_DIR", root / "memos")
    monkeypatch.setattr(ingest_audio, "ACTION_SAVE_DIR", root / "actions")
    return root


@pytest.fixture
def client(database_engine: Engine) -> Iterator[TestClient]:
    original_overrides = ingest_main.app.dependency_overrides.copy()

    def override_get_session() -> Iterator[Session]:
        with Session(database_engine) as session:
            yield session

    ingest_main.app.dependency_overrides[get_session] = override_get_session
    # Avoid the app lifespan here; it uses the application's default database.
    test_client = TestClient(ingest_main.app)
    try:
        yield test_client
    finally:
        test_client.close()
        ingest_main.app.dependency_overrides.clear()
        ingest_main.app.dependency_overrides.update(original_overrides)


def _headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {WEBHOOK_TOKEN}", **extra}


def _notes(engine: Engine) -> list[Note]:
    with Session(engine) as session:
        return list(session.exec(select(Note)).all())


def test_root_returns_ok_for_connectivity_checks(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_audio_is_saved_and_note_is_persisted(
    client: TestClient,
    database_engine: Engine,
    storage_root: Path,
) -> None:
    payload = b"audio test payload"
    recorded_at = 1_700_000_000_000
    response = client.post(
        "/memo",
        headers=_headers(**{"X-Audio-Size": str(len(payload))}),
        data={
            "recordedAt": str(recorded_at),
            "client": "unit-test-client",
            "transcription": "A test transcript",
        },
        files={"audio": ("recording.m4a", payload, "audio/mp4")},
    )

    assert response.status_code == 202, response.text
    notes = _notes(database_engine)
    assert len(notes) == 1
    note = notes[0]
    assert note.source == NoteSource.MEMO
    assert note.status == NoteStatus.RECEIVED
    assert note.recorded_at_ms == recorded_at
    assert note.transcript == "A test transcript"
    assert note.audio_key.startswith("memos/")
    with Session(database_engine) as session:
        notifications = session.exec(select(NotificationOutbox)).all()
    assert len(notifications) == 1
    assert notifications[0].note_id == note.id
    assert notifications[0].status == NotificationStatus.PENDING
    assert notifications[0].priority == 0

    audio_path = storage_root / note.audio_key
    assert audio_path.is_file()
    assert audio_path.read_bytes() == payload
    assert note.id.hex in audio_path.name


@pytest.mark.parametrize("endpoint", ["/memo", "/action"])
def test_invalid_bearer_token_is_rejected_without_persisting_data(
    client: TestClient,
    database_engine: Engine,
    storage_root: Path,
    endpoint: str,
) -> None:
    response = client.post(
        endpoint,
        headers={"Authorization": "Bearer wrong-token"},
        data={"recordedAt": "1700000000000", "client": "unit-test-client"},
        files={"audio": ("recording.m4a", b"audio", "audio/mp4")},
    )

    assert response.status_code == 401
    assert _notes(database_engine) == []
    with Session(database_engine) as session:
        assert session.exec(select(NotificationOutbox)).all() == []
    assert list(storage_root.rglob("*.m4a")) == []


def test_action_endpoint_is_deprecated_without_processing_upload(
    client: TestClient,
    database_engine: Engine,
    storage_root: Path,
) -> None:
    response = client.post(
        "/action",
        headers=_headers(),
        data={"recordedAt": "1700000000000", "client": "unit-test-client"},
        files={"audio": ("recording.m4a", b"audio", "audio/mp4")},
    )

    assert response.status_code == 410
    assert response.json() == {"detail": "The /action endpoint is deprecated"}
    assert _notes(database_engine) == []
    with Session(database_engine) as session:
        assert session.exec(select(NotificationOutbox)).all() == []
    assert list(storage_root.rglob("*.m4a")) == []


def test_missing_audio_is_rejected_without_persisting_a_note(
    client: TestClient,
    database_engine: Engine,
) -> None:
    response = client.post(
        "/memo",
        headers=_headers(),
        data={"recordedAt": "1700000000000", "client": "unit-test-client"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "Audio is required"
    assert _notes(database_engine) == []


def test_oversized_audio_returns_413_and_queues_notification(
    client: TestClient,
    database_engine: Engine,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest_main, "MAX_AUDIO_SIZE_BYTES", 4)
    payload = b"audio is too large"
    response = client.post(
        "/memo",
        headers=_headers(),
        data={"recordedAt": "1700000000000", "client": "unit-test-client"},
        files={"audio": ("recording.m4a", payload, "audio/mp4")},
    )

    assert response.status_code == 413
    assert "configured maximum size" in response.json()["detail"]
    assert _notes(database_engine) == []
    assert list(storage_root.rglob("*.m4a")) == []
    with Session(database_engine) as session:
        notifications = session.exec(select(NotificationOutbox)).all()
    assert len(notifications) == 1
    assert notifications[0].note_id is None
    assert notifications[0].priority == 0
    assert notifications[0].dedupe_key.startswith("oversized-upload:")


def test_audio_size_mismatch_is_rejected_and_file_removed(
    client: TestClient,
    database_engine: Engine,
    storage_root: Path,
) -> None:
    payload = b"audio test payload"
    response = client.post(
        "/memo",
        headers=_headers(**{"X-Audio-Size": "1"}),
        data={"recordedAt": "1700000000000", "client": "unit-test-client"},
        files={"audio": ("recording.m4a", payload, "audio/mp4")},
    )

    assert response.status_code == 400, response.text
    assert _notes(database_engine) == []
    assert list(storage_root.rglob("*.m4a")) == []
