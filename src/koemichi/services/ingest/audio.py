"""Audio persistence utilities for the ingest webhooks."""

import logging
import shutil
from pathlib import Path
from uuid import UUID

from fastapi import UploadFile

from koemichi.shared.settings import ACTION_SAVE_DIR, MEMO_SAVE_DIR
from koemichi.shared.timezone import ts_to_local

logger = logging.getLogger(__name__)


def _save_audio(
    directory: Path, recorded_at: int, audio: UploadFile, note_id: UUID
) -> Path:
    """Write an uploaded recording beneath its destination directory.

    The date directory and filename timestamp are derived from ``recorded_at``
    using the configured local timezone. The note UUID prevents filename
    collisions and links the file to its database record.

    Parameters
    ----------
    directory : Path
        Root directory for this recording type.
    recorded_at : int
        Recording timestamp in milliseconds since the Unix epoch.
    audio : UploadFile
        Uploaded audio file to copy.
    note_id : UUID
        Identifier of the database record associated with this audio.

    Returns
    -------
    Path
        Path to the saved audio file.

    Raises
    ------
    OSError
        If the directory or file cannot be created or written.
    """
    local_dt = ts_to_local(recorded_at)
    folder = directory / f"{local_dt:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    recorded_time = f"{local_dt:%Y%m%d-%H%M%S}-{local_dt.microsecond // 1000:03d}"
    path = folder / f"{recorded_time}-{note_id.hex}.m4a"

    logger.debug("Streaming audio to %s", path)
    with path.open("xb") as f:
        shutil.copyfileobj(audio.file, f)

    logger.info("Saved audio (%d bytes) to %s", path.stat().st_size, path)

    return path


def save_memo_audio(recorded_at: int, audio: UploadFile, note_id: UUID) -> Path:
    """Save a memo recording under the configured memo directory.

    Parameters
    ----------
    recorded_at : int
        Recording timestamp in milliseconds since the Unix epoch.
    audio : UploadFile
        Uploaded memo audio.
    note_id : UUID
        Identifier of the database record associated with this audio.

    Returns
    -------
    Path
        Path to the saved memo audio file.

    Raises
    ------
    OSError
        If the memo directory or file cannot be created or written.
    """
    return _save_audio(MEMO_SAVE_DIR, recorded_at, audio, note_id)


def save_action_audio(recorded_at: int, audio: UploadFile, note_id: UUID) -> Path:
    """Save an action recording under the configured action directory.

    Parameters
    ----------
    recorded_at : int
        Recording timestamp in milliseconds since the Unix epoch.
    audio : UploadFile
        Uploaded action audio.
    note_id : UUID
        Identifier of the database record associated with this audio.

    Returns
    -------
    Path
        Path to the saved action audio file.

    Raises
    ------
    OSError
        If the action directory or file cannot be created or written.
    """
    return _save_audio(ACTION_SAVE_DIR, recorded_at, audio, note_id)


def is_same_size(path: Path, expected: int | None) -> bool:
    """Check a saved file's size and remove it if the size is incorrect.

    Parameters
    ----------
    path : Path
        Saved audio file to inspect.
    expected : int or None
        Expected size in bytes. If ``None``, size validation is skipped.

    Returns
    -------
    bool
        True if the size matches or validation was skipped; False if it
        mismatches. On mismatch, the file is deleted.

    Raises
    ------
    OSError
        If the file cannot be inspected or removed.
    """
    if expected is None:
        logger.warning("No expected size provided for %s, skipping size check", path)
        return True

    actual = path.stat().st_size
    if actual != expected:
        logger.warning(
            "Audio size mismatch: expected=%d got=%d, deleting %s",
            expected,
            actual,
            path,
        )
        path.unlink(missing_ok=True)
        return False

    return True
