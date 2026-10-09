"""SQLite engine, sessions, and initial table creation."""

from collections.abc import Generator
from typing import Any, cast

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from koemichi.shared.models.note import Note  # noqa: F401
from koemichi.shared.models.notification import NotificationOutbox
from koemichi.shared.settings import DATABASE_PATH

DATABASE_URL = f"sqlite:///{DATABASE_PATH}"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


def _migrate_schema() -> None:
    """Apply small, versioned SQLite migrations while preserving existing notes."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS koemichi_schema_version "
            "(version INTEGER NOT NULL)"
        )
        version = connection.exec_driver_sql(
            "SELECT version FROM koemichi_schema_version LIMIT 1"
        ).scalar_one_or_none()
        if version is None:
            connection.exec_driver_sql(
                "INSERT INTO koemichi_schema_version (version) VALUES (0)"
            )
            version = 0

        if version < 1:
            columns = {
                column["name"] for column in inspect(connection).get_columns("note")
            }
            migrations = {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "DATETIME",
                "lease_expires_at": "DATETIME",
            }
            for column, sql_type in migrations.items():
                if column not in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE note ADD COLUMN {column} {sql_type}"
                    )

            # Earlier versions stored enum member names and used ROUTED to mean
            # the webhook had accepted the note.
            connection.execute(
                text("UPDATE note SET status = 'DISPATCHED' WHERE status = 'ROUTED'")
            )
            connection.exec_driver_sql("UPDATE koemichi_schema_version SET version = 1")

        if version < 2:
            columns = {
                column["name"] for column in inspect(connection).get_columns("note")
            }
            migrations = {
                "classification_method": "VARCHAR",
                "classification_confidence": "FLOAT",
            }
            for column, sql_type in migrations.items():
                if column not in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE note ADD COLUMN {column} {sql_type}"
                    )
            connection.exec_driver_sql("UPDATE koemichi_schema_version SET version = 2")

        if version < 3:
            table = cast(Any, NotificationOutbox).__table__
            preparer = connection.dialect.identifier_preparer
            column_names = [preparer.quote(column.name) for column in table.columns]
            columns_sql = ", ".join(column_names)
            tables = set(inspect(connection).get_table_names())

            if "notificationoutbox" in tables:
                connection.exec_driver_sql(
                    "INSERT OR IGNORE INTO notification_outbox "
                    f"({columns_sql}) SELECT {columns_sql} FROM notificationoutbox"
                )
                connection.exec_driver_sql("DROP TABLE notificationoutbox")
            else:
                notification_columns = inspect(connection).get_columns(
                    "notification_outbox"
                )
                note_id_column = next(
                    column
                    for column in notification_columns
                    if column["name"] == "note_id"
                )
                if not note_id_column["nullable"]:
                    legacy_table = "notification_outbox_v2"
                    connection.exec_driver_sql(
                        f"ALTER TABLE notification_outbox RENAME TO {legacy_table}"
                    )
                    for index in inspect(connection).get_indexes(legacy_table):
                        index_name = index["name"]
                        if index_name is not None:
                            quoted_index = preparer.quote(index_name)
                            connection.exec_driver_sql(
                                f"DROP INDEX IF EXISTS {quoted_index}"
                            )
                    table.create(connection)
                    connection.exec_driver_sql(
                        f"INSERT INTO notification_outbox ({columns_sql}) "
                        f"SELECT {columns_sql} FROM {legacy_table}"
                    )
                    connection.exec_driver_sql(f"DROP TABLE {legacy_table}")
            connection.exec_driver_sql("UPDATE koemichi_schema_version SET version = 3")

        if version < 4:
            connection.execute(
                text("UPDATE note SET intent = 'memo' WHERE intent = 'other'")
            )
            connection.exec_driver_sql("UPDATE koemichi_schema_version SET version = 4")


def create_db_and_tables() -> None:
    """Create the SQLite file, tables, and any required schema migrations."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _migrate_schema()
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")


def get_session() -> Generator[Session, None, None]:
    """Yield a database session and close it when the request is complete."""
    with Session(engine) as session:
        yield session


def close_db() -> None:
    """Dispose the database engine's connection pool."""
    engine.dispose()
