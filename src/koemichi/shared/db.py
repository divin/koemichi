"""SQLite engine, sessions, and initial table creation."""

import logging
import time
from collections.abc import Generator

from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine

from koemichi.shared.models.note import Note  # noqa: F401
from koemichi.shared.models.notification import NotificationOutbox  # noqa: F401
from koemichi.shared.settings import DATABASE_PATH

logger = logging.getLogger(__name__)

engine = create_engine(
    f"sqlite:///{DATABASE_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)

# ``PRAGMA journal_mode=WAL`` requires an exclusive lock and returns
# SQLITE_BUSY immediately -- ignoring the busy timeout -- while another
# connection holds a schema lock. Compose starts the receiver and worker
# together, and both call ``create_db_and_tables()`` on the same database,
# so the WAL switch retries until the peer's initialization commits.
WAL_RETRY_ATTEMPTS = 100
WAL_RETRY_DELAY_SECONDS = 0.05


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

    _enable_write_ahead_logging()


def _enable_write_ahead_logging() -> None:
    """Switch SQLite to WAL mode, retrying while a peer holds the schema lock.

    ``PRAGMA journal_mode=WAL`` needs an exclusive lock; if another process
    or thread is mid-initialization inside ``BEGIN IMMEDIATE`` it returns
    ``SQLITE_BUSY`` right away (the busy timeout does not apply). Retry with
    a short delay so concurrent Compose startup converges, and fail fast on
    any lock-independent error.
    """
    last_error: OperationalError | None = None
    for attempt in range(WAL_RETRY_ATTEMPTS):
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            return
        except OperationalError as exc:
            if "locked" not in str(exc):
                raise
            last_error = exc
            if attempt + 1 < WAL_RETRY_ATTEMPTS:
                time.sleep(WAL_RETRY_DELAY_SECONDS)
    logger.error(
        "SQLite WAL mode could not be enabled after %s attempts", WAL_RETRY_ATTEMPTS
    )
    raise RuntimeError("SQLite WAL mode could not be enabled") from last_error


def _add_missing_note_columns(connection: Connection) -> None:
    """Add workflow columns to existing SQLite databases under the schema lock."""
    existing = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(note)")}
    for name in (
        "memo_title",
        "memo_content",
        "memo_path",
        "journal_content",
        "journal_path",
        "todo_title",
        "todo_description",
        "todo_due_date",
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
