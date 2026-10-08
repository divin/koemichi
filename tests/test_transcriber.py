"""Tests for audio conversion and transcription task processing."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("WEBHOOK_TOKEN", "unit-test-token")
os.environ.setdefault("STT_URL", "http://localhost:8080/v1/audio/transcriptions")
os.environ.setdefault("STT_MODEL_NAME", "test-model")

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from koemichi.services.transcriber import audio as transcriber_audio
from koemichi.services.transcriber import client as transcriber_client
from koemichi.services.transcriber import processor as transcriber_processor
from koemichi.shared.models.note import Note, NoteSource, NoteStatus


@pytest.fixture
def database_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Engine, Path]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'transcriber.db'}")
    SQLModel.metadata.create_all(engine)
    audio_root = tmp_path / "audio"
    monkeypatch.setattr(transcriber_processor, "AUDIO_STORAGE_ROOT", audio_root)
    try:
        yield engine, audio_root
    finally:
        engine.dispose()


def _create_note(
    engine: Engine, audio_root: Path, transcript: str | None = None
) -> Note:
    audio_path = audio_root / "memos" / "recording.m4a"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"source audio")
    note = Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        recorded_at_ms=1_700_000_000_000,
        audio_key="memos/recording.m4a",
        transcript=transcript,
    )
    with Session(engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)
    return note


def _get_note(engine: Engine, note_id: object) -> Note:
    with Session(engine) as session:
        note = session.get(Note, note_id)
        assert note is not None
        return note


def test_to_wav_invokes_ffmpeg_and_returns_temporary_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "recording.m4a"
    source.write_bytes(b"source audio")
    command: list[str] = []

    def fake_run(args: list[str], **kwargs: object) -> None:
        command.extend(args)
        Path(args[-1]).write_bytes(b"wav audio")

    monkeypatch.setattr(transcriber_audio.subprocess, "run", fake_run)

    wav_path = transcriber_audio.to_wav(source)
    try:
        assert wav_path.suffix == ".wav"
        assert wav_path.read_bytes() == b"wav audio"
        assert command[0] == "ffmpeg"
        assert "16000" in command
        assert "1" in command
    finally:
        wav_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_transcribe_uploads_wav_with_configured_model_and_cleans_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav_path = tmp_path / "converted.wav"
    wav_path.write_bytes(b"wav bytes")
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, json={"text": "  recognized words  "})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        transcriber_client.httpx,
        "AsyncClient",
        lambda *, timeout: async_client(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    monkeypatch.setattr(transcriber_client, "STT_URL", "https://stt.test/transcribe")
    monkeypatch.setattr(transcriber_client, "STT_MODEL_NAME", "test-asr-model")
    monkeypatch.setattr(transcriber_client, "to_wav", lambda _: wav_path)

    transcript = await transcriber_client.transcribe(tmp_path / "source.m4a")

    assert transcript == "recognized words"
    assert len(requested) == 1
    request = requested[0]
    assert str(request.url) == "https://stt.test/transcribe"
    body = await request.aread()
    assert b"test-asr-model" in body
    assert b"audio/wav" in body
    assert b"wav bytes" in body
    assert not wav_path.exists()


@pytest.mark.asyncio
async def test_transcribe_retries_server_errors_and_cleans_wav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav_path = tmp_path / "converted.wav"
    wav_path.write_bytes(b"wav bytes")
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"text": "recovered"})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        transcriber_client.httpx,
        "AsyncClient",
        lambda *, timeout: async_client(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(transcriber_client.asyncio, "sleep", no_wait)
    monkeypatch.setattr(transcriber_client, "to_wav", lambda _: wav_path)

    assert await transcriber_client.transcribe(tmp_path / "source.m4a") == "recovered"
    assert attempts == 2
    assert not wav_path.exists()


@pytest.mark.asyncio
async def test_transcribe_note_saves_transcript_sidecar(
    database_engine: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, audio_root = database_engine
    note = _create_note(engine, audio_root)
    requested_path: Path | None = None

    async def fake_transcribe(path: Path) -> str:
        nonlocal requested_path
        requested_path = path
        return "A spoken note"

    monkeypatch.setattr(transcriber_processor, "transcribe", fake_transcribe)

    transcript = await transcriber_processor.transcribe_note(note)

    audio_path = audio_root / note.audio_key
    assert requested_path == audio_path
    assert transcript == "A spoken note"
    markdown_path = audio_path.with_suffix(".md")
    assert markdown_path.is_file()
    content = markdown_path.read_text(encoding="utf-8")
    assert f"noteId: {note.id}" in content
    assert "source: memo" in content
    assert "audio: recording.m4a" in content
    assert "A spoken note" in content


@pytest.mark.asyncio
async def test_transcribe_note_prefers_device_transcript(
    database_engine: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, audio_root = database_engine
    note = _create_note(engine, audio_root, transcript="  Pebble transcript  ")

    async def unexpected_transcribe(_: Path) -> str:
        raise AssertionError("STT should not run when a transcript exists")

    monkeypatch.setattr(transcriber_processor, "transcribe", unexpected_transcribe)

    transcript = await transcriber_processor.transcribe_note(note)

    assert transcript == "Pebble transcript"
    markdown_path = (audio_root / note.audio_key).with_suffix(".md")
    assert markdown_path.is_file()
    assert "Pebble transcript" in markdown_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_transcribe_note_propagates_failures_to_worker(
    database_engine: tuple[Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, audio_root = database_engine
    note = _create_note(engine, audio_root)

    async def failed_transcribe(_: Path) -> str:
        raise RuntimeError("STT is unavailable")

    monkeypatch.setattr(transcriber_processor, "transcribe", failed_transcribe)

    with pytest.raises(RuntimeError, match="STT is unavailable"):
        await transcriber_processor.transcribe_note(note)

    assert _get_note(engine, note.id).status == NoteStatus.RECEIVED
    assert not (audio_root / note.audio_key).with_suffix(".md").exists()


def test_make_health_url_derives_models_endpoint() -> None:
    url = transcriber_client.make_health_url(
        "http://audio-cpp:8080/v1/audio/transcriptions"
    )
    assert url == "http://audio-cpp:8080/v1/models"


def test_make_health_url_returns_none_for_unknown_layout() -> None:
    assert transcriber_client.make_health_url("http://example.test/transcribe") is None


@pytest.mark.asyncio
async def test_check_stt_health_ok_when_service_responds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": []})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        transcriber_client.httpx,
        "AsyncClient",
        lambda *, timeout: async_client(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    monkeypatch.setattr(
        transcriber_client,
        "STT_URL",
        "http://audio-cpp:8080/v1/audio/transcriptions",
    )

    await transcriber_client.check_stt_health()


@pytest.mark.asyncio
async def test_check_stt_health_raises_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        transcriber_client.httpx,
        "AsyncClient",
        lambda *, timeout: async_client(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    monkeypatch.setattr(
        transcriber_client,
        "STT_URL",
        "http://audio-cpp:8080/v1/audio/transcriptions",
    )

    with pytest.raises(RuntimeError, match="unreachable"):
        await transcriber_client.check_stt_health()


@pytest.mark.asyncio
async def test_check_stt_health_warns_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        transcriber_client.httpx,
        "AsyncClient",
        lambda *, timeout: async_client(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    monkeypatch.setattr(
        transcriber_client,
        "STT_URL",
        "http://audio-cpp:8080/v1/audio/transcriptions",
    )

    # A reachable server without /v1/models must not crash the worker.
    await transcriber_client.check_stt_health()


@pytest.mark.asyncio
async def test_check_stt_health_skips_when_url_cannot_be_derived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected_client(*, timeout: float) -> object:
        nonlocal called
        called = True
        raise AssertionError("AsyncClient should not be created")

    monkeypatch.setattr(transcriber_client.httpx, "AsyncClient", unexpected_client)
    monkeypatch.setattr(transcriber_client, "STT_URL", "http://example.test/transcribe")

    await transcriber_client.check_stt_health()
    assert not called
