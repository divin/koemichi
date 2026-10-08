"""HTTP client for the configured speech-to-text service."""

import asyncio
import logging
from pathlib import Path

import anyio
import httpx

from koemichi.shared.settings import STT_MODEL_NAME, STT_URL

from .audio import to_wav

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 120
_HEALTH_TIMEOUT_SECONDS = 10
_MAX_ATTEMPTS = 3


def make_health_url(stt_url: str) -> str | None:
    """Derive an OpenAI-compatible health URL from the transcription endpoint.

    ``https://host:8080/v1/audio/transcriptions`` becomes
    ``https://host:8080/v1/models``. If the URL layout is not recognised, the
    health check is skipped.

    Parameters
    ----------
    stt_url : str
        Configured speech-to-text endpoint URL.

    Returns
    -------
    str or None
        Derived models endpoint URL, or ``None`` if the endpoint suffix does
        not match the OpenAI-compatible transcription path.
    """
    suffix = "/v1/audio/transcriptions"
    if stt_url.endswith(suffix):
        return stt_url[: -len(suffix)] + "/v1/models"
    return None


async def check_stt_health() -> None:
    """Ping the STT service so failures surface when the worker starts.

    Transport errors raise so a misconfigured or unreachable ``STT_URL`` fails
    fast at startup. HTTP error responses are only logged, because a reachable
    server may simply not expose ``/v1/models``.

    Raises
    ------
    RuntimeError
        If the STT service cannot be reached.
    """
    health_url = make_health_url(STT_URL)
    if health_url is None:
        logger.warning(
            "Cannot derive STT health URL from %s; skipping health check", STT_URL
        )
        return

    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_SECONDS) as client:
            response = await client.get(health_url)
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"STT service at {health_url} is unreachable: {exc}"
        ) from exc

    if response.status_code >= 400:
        logger.warning(
            "STT health check %s returned %s; continuing",
            health_url,
            response.status_code,
        )
        return
    logger.info("STT health check OK: %s", health_url)


async def transcribe(audio_path: Path) -> str | None:
    """Convert a recording to WAV and request a transcript from the STT service.

    The temporary WAV file is removed whether the request succeeds or fails.
    Transient HTTP failures and server errors are retried with exponential backoff.

    Parameters
    ----------
    audio_path : Path
        Path to the source audio file.

    Returns
    -------
    str or None
        Transcript returned by the STT service, or ``None`` when its ``text``
        field is empty or null.

    Raises
    ------
    httpx.HTTPError
        If the STT request fails after retries, or fails with a client error.
    TypeError
        If the STT service returns an invalid response body.
    OSError
        If the source audio cannot be read or converted.
    subprocess.CalledProcessError
        If ``ffmpeg`` cannot convert the audio to WAV.
    """
    wav_path = await anyio.to_thread.run_sync(to_wav, audio_path)
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    with wav_path.open("rb") as wav_file:
                        response = await client.post(
                            STT_URL,
                            files={"file": (wav_path.name, wav_file, "audio/wav")},
                            data={"model": STT_MODEL_NAME},
                        )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise TypeError("STT response must be a JSON object")
                    text = payload.get("text")
                    if text is not None and not isinstance(text, str):
                        raise TypeError("STT response 'text' must be a string or null")

                    transcript = text.strip() if text else None
                    logger.info(
                        "Transcription completed for %s (%d characters)",
                        audio_path,
                        len(transcript or ""),
                    )
                    return transcript
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code < 500 or attempt + 1 == _MAX_ATTEMPTS:
                        raise
                except httpx.TransportError:
                    if attempt + 1 == _MAX_ATTEMPTS:
                        raise

                await asyncio.sleep(2**attempt)
    finally:
        wav_path.unlink(missing_ok=True)

    raise RuntimeError("STT request failed without returning a response")
