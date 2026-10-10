"""Tests for deterministic Python intent-handler selection."""

from uuid import uuid4

import pytest

from koemichi.services.router import execution
from koemichi.services.router.intents import Intent
from koemichi.shared.models.note import Note, NoteSource, NoteStatus


def _note(intent: Intent) -> Note:
    """Build a persisted-looking note for execution routing tests."""
    return Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=NoteStatus.ROUTED,
        recorded_at_ms=1_700_000_000_000,
        transcript="example transcript",
        intent=intent.value,
    )


@pytest.mark.asyncio
async def test_unregistered_intent_fails_without_external_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not hand unimplemented intents to n8n or another fallback."""
    monkeypatch.setattr(execution, "INTENT_HANDLERS", {})

    with pytest.raises(execution.UnimplementedIntentError, match="memo"):
        await execution.execute_intent(_note(Intent.MEMO))


@pytest.mark.asyncio
async def test_registered_intent_uses_python_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered native handler receives the persisted note."""
    note = _note(Intent.MEMO)
    handled: list[Note] = []

    async def fake_handler(routed_note: Note) -> None:
        handled.append(routed_note)

    monkeypatch.setattr(execution, "INTENT_HANDLERS", {Intent.MEMO: fake_handler})

    await execution.execute_intent(note)

    assert handled == [note]


def test_register_handler_rejects_duplicate_intent_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid silently replacing an intent workflow during module import."""

    async def handler(_: Note) -> None:
        pass

    monkeypatch.setattr(execution, "INTENT_HANDLERS", {})
    execution.register_intent_handler(Intent.MEMO, handler)

    with pytest.raises(ValueError, match="already registered"):
        execution.register_intent_handler(Intent.MEMO, handler)
