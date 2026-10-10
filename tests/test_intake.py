"""Tests for Koemichi's authenticated transcript receiver."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("TRANSCRIPT_INGEST_TOKEN", "unit-test-ingest-token")
os.environ.setdefault("LLM_URL", "http://localhost:8080/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")
os.environ.setdefault("NOTIFICATION_MODE", "off")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from koemichi.services.intake import main as intake_main
from koemichi.shared.db import get_session
from koemichi.shared.models.note import Note, NoteStatus


@pytest.fixture
def database_engine(tmp_path: Path) -> Iterator[Engine]:
    """Provide an isolated SQLite database for intake tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'intake.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def client(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a test client connected to the isolated database."""
    monkeypatch.setattr(
        intake_main, "TRANSCRIPT_INGEST_TOKEN", "unit-test-ingest-token"
    )
    original_overrides = intake_main.app.dependency_overrides.copy()

    def override_get_session() -> Iterator[Session]:
        """Yield a session connected to the test database."""
        with Session(database_engine) as session:
            yield session

    intake_main.app.dependency_overrides[get_session] = override_get_session
    test_client = TestClient(intake_main.app)
    try:
        yield test_client
    finally:
        test_client.close()
        intake_main.app.dependency_overrides.clear()
        intake_main.app.dependency_overrides.update(original_overrides)


def _payload(note_id: str | None = None) -> dict[str, object]:
    """Build a valid transcript payload, optionally with a fixed note ID."""
    return {
        "schema_version": 1,
        "note_id": note_id or str(uuid4()),
        "source": "memo",
        "recorded_at_ms": 1_700_000_000_000,
        "transcript": "  transcript text  ",
    }


def _notes(engine: Engine) -> list[Note]:
    """Return all notes persisted in the supplied database."""
    with Session(engine) as session:
        return list(session.exec(select(Note)).all())


def test_transcript_is_persisted_for_router_worker(
    client: TestClient, database_engine: Engine
) -> None:
    """Verify an accepted transcript is saved for the routing worker."""
    response = client.post(
        "/transcripts",
        headers={"Authorization": "Bearer unit-test-ingest-token"},
        json=_payload(),
    )
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    notes = _notes(database_engine)
    assert len(notes) == 1
    assert notes[0].status == NoteStatus.TRANSCRIBED
    assert notes[0].transcript == "transcript text"


def test_duplicate_payload_is_idempotent(
    client: TestClient, database_engine: Engine
) -> None:
    """Verify resubmitting an identical payload is idempotent."""
    payload = _payload()
    headers = {"Authorization": "Bearer unit-test-ingest-token"}
    first = client.post("/transcripts", headers=headers, json=payload)
    duplicate = client.post("/transcripts", headers=headers, json=payload)
    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate"}
    assert len(_notes(database_engine)) == 1


def test_conflicting_reuse_of_note_id_is_rejected(client: TestClient) -> None:
    """Verify a note ID cannot be reused for different transcript data."""
    payload = _payload()
    headers = {"Authorization": "Bearer unit-test-ingest-token"}
    assert client.post("/transcripts", headers=headers, json=payload).status_code == 200
    conflicting = {**payload, "transcript": "different transcript"}
    response = client.post("/transcripts", headers=headers, json=conflicting)
    assert response.status_code == 409


def test_receiver_accepts_requests_without_auth_when_token_is_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow unauthenticated intake when the optional token is not configured."""
    monkeypatch.setattr(intake_main, "TRANSCRIPT_INGEST_TOKEN", None)

    response = client.post("/transcripts", json=_payload())

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_missing_or_wrong_token_is_rejected(client: TestClient) -> None:
    """Verify the receiver rejects absent and invalid sender credentials."""
    response = client.post("/transcripts", json=_payload())
    assert response.status_code == 401
    response = client.post(
        "/transcripts",
        headers={"Authorization": "Bearer wrong-token"},
        json=_payload(),
    )
    assert response.status_code == 401


def test_unsupported_payload_version_is_rejected(client: TestClient) -> None:
    """Verify the receiver rejects unsupported transcript format versions."""
    payload = {**_payload(), "schema_version": 2}
    response = client.post(
        "/transcripts",
        headers={"Authorization": "Bearer unit-test-ingest-token"},
        json=payload,
    )
    assert response.status_code == 422


@pytest.mark.parametrize("removed_field", ["event", "client"])
def test_removed_fields_are_rejected(client: TestClient, removed_field: str) -> None:
    """Verify removed payload fields are rejected instead of ignored."""
    payload = {**_payload(), removed_field: "removed"}
    response = client.post(
        "/transcripts",
        headers={"Authorization": "Bearer unit-test-ingest-token"},
        json=payload,
    )
    assert response.status_code == 422
