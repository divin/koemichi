"""SQLite engine, sessions, and initial table creation."""

from collections.abc import Generator

from sqlalchemy.engine import Connection
from sqlmodel import Session, SQLModel, create_engine

from koemichi.shared.models.note import Note  # noqa: F401
from koemichi.shared.models.notification import NotificationOutbox  # noqa: F401
from koemichi.shared.settings import DATABASE_PATH

engine = create_engine(
    f"sqlite:///{DATABASE_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


def create_db_and_tables() -> None:
    """Create a fresh Koemichi schema and enable SQLite WAL mode."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with engine.connect() as connection:
        # Serialize schema initialization: Compose starts receiver and worker
        # together, and both initialize the same SQLite database.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            SQLModel.metadata.create_all(connection)
            _add_missing_note_columns(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")


def _add_missing_note_columns(connection: Connection) -> None:
    """Add workflow columns to existing SQLite databases under the schema lock."""
    existing = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(note)")}
    for name in (
        "memo_title",
        "memo_content",
        "memo_path",
        "journal_content",
        "journal_path",
    ):
        if name not in existing:
            connection.exec_driver_sql(f"ALTER TABLE note ADD COLUMN {name} TEXT")


def get_session() -> Generator[Session, None, None]:
    """Yield a database session and close it after the request."""
    with Session(engine) as session:
        yield session


def close_db() -> None:
    """Dispose the SQLite connection pool."""
    engine.dispose()
