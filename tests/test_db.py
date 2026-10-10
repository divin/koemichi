"""Tests for Koemichi's fresh transcript-routing SQLite schema."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("TRANSCRIPT_INGEST_TOKEN", "unit-test-ingest-token")

from sqlalchemy import inspect
from sqlmodel import Session, select

from koemichi.shared import db
from koemichi.shared.models.note import Note, NoteSource, NoteStatus
from koemichi.shared.models.notification import NotificationOutbox


def test_concurrent_process_startup_initializes_shared_schema_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Serialize receiver/worker schema initialization on the shared SQLite DB."""
    database_path = tmp_path / "shared.db"
    engine = db.create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DATABASE_PATH", database_path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: db.create_db_and_tables(), range(2)))
        columns = {column["name"] for column in inspect(engine).get_columns("note")}
        assert {"memo_title", "journal_content", "journal_path"}.issubset(columns)
    finally:
        engine.dispose()


def test_existing_schema_gets_memo_workflow_columns(
    tmp_path: Path, monkeypatch
) -> None:
    """Add memo draft columns without losing existing persisted notes."""
    database_path = tmp_path / "legacy.db"
    engine = db.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE note (
                id CHAR(32) PRIMARY KEY,
                source VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                created_at DATETIME NOT NULL,
                recorded_at_ms BIGINT NOT NULL,
                transcript VARCHAR,
                intent VARCHAR,
                classification_method VARCHAR,
                classification_confidence FLOAT,
                error_message VARCHAR,
                attempt_count INTEGER NOT NULL,
                next_attempt_at DATETIME,
                lease_expires_at DATETIME
            )
            """
        )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DATABASE_PATH", database_path)
    try:
        db.create_db_and_tables()
        columns = {column["name"] for column in inspect(engine).get_columns("note")}
        assert {
            "memo_title",
            "memo_content",
            "memo_path",
            "journal_content",
            "journal_path",
        }.issubset(columns)
    finally:
        engine.dispose()


def test_fresh_schema_persists_transcript_and_outbox(
    tmp_path: Path, monkeypatch
) -> None:
    """Verify the fresh schema stores notes and notification records."""
    database_path = tmp_path / "koemichi.db"
    engine = db.create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DATABASE_PATH", database_path)
    try:
        db.create_db_and_tables()
        assert {"note", "notification_outbox"}.issubset(
            inspect(engine).get_table_names()
        )
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            )

        note = Note(
            id=uuid4(),
            source=NoteSource.MEMO,
            status=NoteStatus.TRANSCRIBED,
            recorded_at_ms=1_700_000_000_000,
            transcript="todo buy milk",
        )
        event = NotificationOutbox(
            note_id=note.id,
            dedupe_key=f"{note.id}:received",
            title="Koemichi",
            message="Transcript accepted.",
        )
        note_id = note.id
        with Session(engine) as session:
            session.add(note)
            session.add(event)
            session.commit()

        with Session(engine) as session:
            persisted = session.exec(select(Note).where(Note.id == note_id)).one()
            persisted_event = session.exec(select(NotificationOutbox)).one()
        assert persisted.status == NoteStatus.TRANSCRIBED

        assert persisted.transcript == "todo buy milk"
        assert persisted_event.note_id == note_id
    finally:
        engine.dispose()
