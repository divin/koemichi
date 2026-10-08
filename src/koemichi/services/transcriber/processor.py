"""Callable transcription stage used by the SQLite polling worker."""

from pathlib import Path
from uuid import UUID

from koemichi.shared.models.note import Note
from koemichi.shared.settings import AUDIO_STORAGE_ROOT

from .client import transcribe
from .transcript import save_transcript


def audio_path_for_key(audio_key: str) -> Path:
    """Resolve a stored audio key and ensure it stays within storage.

    Parameters
    ----------
    audio_key : str
        Relative key stored on the note row.

    Returns
    -------
    Path
        Absolute path to an existing audio file.

    Raises
    ------
    ValueError
        If the resolved path escapes the configured storage root.
    FileNotFoundError
        If the key points to a file that does not exist.
    """
    storage_root = AUDIO_STORAGE_ROOT.resolve()
    audio_path = (storage_root / audio_key).resolve()
    if not audio_path.is_relative_to(storage_root):
        raise ValueError("Audio key escapes storage root")
    if not audio_path.is_file():
        raise FileNotFoundError("Stored audio file is missing")
    return audio_path


async def transcribe_note(note: Note) -> str | None:
    """Transcribe a note and write its Markdown sidecar.

    A transcript supplied by the recording client is preferred to an ASR call.
    Note status and retry metadata are owned by the polling worker.

    Parameters
    ----------
    note : Note
        Persisted note whose transcript should be created or reused.

    Returns
    -------
    str or None
        Normalized transcript text, or ``None`` when no text is available.

    Raises
    ------
    ValueError
        If the audio key resolves outside the configured storage directory.
    FileNotFoundError
        If the referenced audio file is missing.
    OSError
        If audio conversion or the transcript sidecar write fails.
    """
    audio_path = audio_path_for_key(note.audio_key)
    transcript = note.transcript.strip() if note.transcript else None
    if transcript is None:
        transcript = await transcribe(audio_path)

    save_transcript(
        audio_path,
        note_id=UUID(str(note.id)),
        recorded_at_ms=note.recorded_at_ms,
        source=note.source,
        transcript=transcript,
    )
    return transcript
