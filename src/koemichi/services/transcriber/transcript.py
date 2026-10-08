"""Markdown transcript-file persistence."""

import logging
from pathlib import Path
from uuid import UUID

from koemichi.shared.models.note import NoteSource
from koemichi.shared.timezone import ts_to_local

logger = logging.getLogger(__name__)


def save_transcript(
    audio_path: Path,
    note_id: UUID,
    recorded_at_ms: int,
    source: NoteSource,
    transcript: str | None,
) -> Path:
    """Write transcript metadata and text to a Markdown sidecar file.

    Parameters
    ----------
    audio_path : Path
        Path to the recording. The Markdown file is written next to it.
    note_id : UUID
        Identifier of the persisted note.
    recorded_at_ms : int
        Recording timestamp in milliseconds since the Unix epoch.
    source : NoteSource
        Webhook source that received the recording.
    transcript : str or None
        Transcribed text, if available.

    Returns
    -------
    Path
        Path to the created Markdown file.

    Raises
    ------
    OSError
        If the Markdown sidecar cannot be written.
    """
    markdown_path = audio_path.with_suffix(".md")
    local_dt = ts_to_local(recorded_at_ms)
    text = transcript.strip() if transcript else ""
    text = text or "*No transcript available.*"

    content = (
        "---\n"
        f"noteId: {note_id}\n"
        f"recordedAt: {recorded_at_ms}\n"
        f"recorded: {local_dt.isoformat()}\n"
        f"source: {source.value}\n"
        f"audio: {audio_path.name}\n"
        "---\n\n"
        f"{text}\n"
    )
    markdown_path.write_text(content, encoding="utf-8")
    logger.info("Wrote transcript to %s", markdown_path)
    return markdown_path
