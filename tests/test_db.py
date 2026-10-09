"""Tests for preserving and migrating the on-disk SQLite schema."""

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("WEBHOOK_TOKEN", "unit-test-token")
os.environ.setdefault("STT_URL", "http://localhost:8080/v1/audio/transcriptions")
os.environ.setdefault("STT_MODEL_NAME", "test-model")
os.environ.setdefault("NOTIFICATION_MODE", "normal")

from sqlalchemy import inspect
from sqlmodel import Session, SQLModel, create_engine, select

from koemichi.shared import db
from koemichi.shared.models.note import Note, NoteSource, NoteStatus
from koemichi.shared.models.notification import NotificationOutbox


def test_processing_migration_adds_fields_and_preserves_legacy_delivery_state(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "koemichi.db"
    engine = create_engine(f"sqlite:///{database_path}")
    note_id = uuid4()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE note (
                id CHAR(32) PRIMARY KEY,
                source VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                created_at DATETIME NOT NULL,
                recorded_at_ms INTEGER NOT NULL,
                audio_key VARCHAR NOT NULL,
                transcript VARCHAR,
                intent VARCHAR,
                error_message VARCHAR
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO note
                (id, source, status, created_at, recorded_at_ms, audio_key, transcript)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note_id.hex,
                "MEMO",
                "ROUTED",
                datetime.now(UTC).replace(tzinfo=None).isoformat(),
                1_700_000_000_000,
                "memos/old.m4a",
                "old transcript",
            ),
        )

    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DATABASE_PATH", database_path)
    try:
        db.create_db_and_tables()

        with Session(engine) as session:
            migrated = session.exec(select(Note).where(Note.id == note_id)).one()
            assert migrated.status == NoteStatus.DISPATCHED
            assert migrated.attempt_count == 0
            assert migrated.next_attempt_at is None
            assert migrated.lease_expires_at is None
            assert migrated.classification_method is None
            assert migrated.classification_confidence is None
            assert session.exec(select(NotificationOutbox)).all() == []

            pending = Note(
                id=uuid4(),
                source=NoteSource.MEMO,
                status=NoteStatus.ROUTED,
                recorded_at_ms=1_700_000_000_001,
                audio_key="memos/new.m4a",
            )
            session.add(pending)
            session.commit()
            pending_id = pending.id

        # Startup migrations are versioned and must not reinterpret new rows.
        db.create_db_and_tables()
        with Session(engine) as session:
            pending = session.get(Note, pending_id)
            assert pending is not None
            assert pending.status == NoteStatus.ROUTED
    finally:
        engine.dispose()


def test_removed_other_intent_is_migrated_to_memo(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "intent-migration.db"
    engine = create_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(engine)
    note = Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=NoteStatus.ROUTED,
        recorded_at_ms=1_700_000_000_000,
        audio_key="memos/queued.m4a",
        intent="other",
    )
    with Session(engine) as session:
        session.add(note)
        session.commit()
        note_id = note.id

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE koemichi_schema_version (version INTEGER NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO koemichi_schema_version (version) VALUES (3)"
        )

    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DATABASE_PATH", database_path)
    try:
        db.create_db_and_tables()

        with Session(engine) as session:
            migrated = session.get(Note, note_id)
        assert migrated is not None
        assert migrated.intent == "memo"
        assert migrated.status == NoteStatus.ROUTED
    finally:
        engine.dispose()


def test_outbox_migration_allows_events_without_a_note(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "outbox-migration.db"
    engine = create_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(engine)
    note = Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        recorded_at_ms=1_700_000_000_000,
        audio_key="memos/legacy.m4a",
    )
    event_id = uuid4()
    created_at = datetime.now(UTC).replace(tzinfo=None)
    note_id = note.id
    with Session(engine) as session:
        session.add(note)
        session.commit()

    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE notification_outbox")
        connection.exec_driver_sql(
            """
            CREATE TABLE notificationoutbox (
                id CHAR(32) NOT NULL PRIMARY KEY,
                note_id CHAR(32) NOT NULL,
                dedupe_key VARCHAR(255) NOT NULL UNIQUE,
                title VARCHAR(250) NOT NULL,
                message VARCHAR(1024) NOT NULL,
                priority INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                created_at DATETIME NOT NULL,
                attempt_count INTEGER NOT NULL,
                next_attempt_at DATETIME,
                lease_expires_at DATETIME,
                sent_at DATETIME,
                error_message VARCHAR(255),
                FOREIGN KEY(note_id) REFERENCES note (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO notificationoutbox (
                id, note_id, dedupe_key, title, message, priority, status,
                created_at, attempt_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id.hex,
                note_id.hex,
                "legacy-event",
                "Koemichi",
                "Legacy notification",
                0,
                "PENDING",
                created_at.isoformat(),
                0,
            ),
        )
        connection.exec_driver_sql(
            "CREATE TABLE koemichi_schema_version (version INTEGER NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO koemichi_schema_version (version) VALUES (2)"
        )

    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "DATABASE_PATH", database_path)
    try:
        db.create_db_and_tables()

        with Session(engine) as session:
            migrated_event = session.get(NotificationOutbox, event_id)
        assert migrated_event is not None
        assert migrated_event.note_id == note_id
        assert migrated_event.message == "Legacy notification"
        note_id_column = next(
            column
            for column in inspect(engine).get_columns("notification_outbox")
            if column["name"] == "note_id"
        )
        assert note_id_column["nullable"] is True
        assert "notificationoutbox" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_pending_notes_and_notifications_survive_database_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "data" / "koemichi.db"
    database_path.parent.mkdir()
    first_engine = create_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(first_engine)

    note = Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=NoteStatus.RECEIVED,
        recorded_at_ms=1_700_000_000_000,
        audio_key="memos/pending.m4a",
    )
    event = NotificationOutbox(
        note_id=note.id,
        dedupe_key=f"{note.id}:received",
        title="Koemichi",
        message="Voice note received.",
    )
    with Session(first_engine) as session:
        session.add(note)
        session.add(event)
        session.commit()
        note_id = note.id
        event_id = event.id
    first_engine.dispose()

    reopened_engine = create_engine(f"sqlite:///{database_path}")
    try:
        with Session(reopened_engine) as session:
            persisted_note = session.get(Note, note_id)
            persisted_event = session.get(NotificationOutbox, event_id)

        assert persisted_note is not None
        assert persisted_note.status == NoteStatus.RECEIVED
        assert persisted_note.audio_key == "memos/pending.m4a"
        assert persisted_event is not None
        assert persisted_event.status.value == "pending"
        assert persisted_event.note_id == note_id
    finally:
        reopened_engine.dispose()
