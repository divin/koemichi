"""Audio conversion helpers used by the transcription service."""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def to_wav(audio_path: Path) -> Path:
    """Convert an audio file to a temporary 16 kHz, mono WAV file.

    Parameters
    ----------
    audio_path : Path
        Path to the source audio file.

    Returns
    -------
    Path
        Path to the converted WAV file. The caller is responsible for deleting it.

    Raises
    ------
    FileNotFoundError
        If ``ffmpeg`` or the source audio file cannot be found.
    subprocess.CalledProcessError
        If ``ffmpeg`` cannot convert the source file.
    subprocess.TimeoutExpired
        If conversion takes longer than 120 seconds.
    """
    descriptor, wav_name = tempfile.mkstemp(suffix=".wav")
    os.close(descriptor)
    wav_path = Path(wav_name)

    logger.debug("Converting %s to temporary WAV %s", audio_path, wav_path)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(audio_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "wav",
                str(wav_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except BaseException:
        wav_path.unlink(missing_ok=True)
        raise

    logger.debug("Converted %s to %s", audio_path, wav_path)
    return wav_path
