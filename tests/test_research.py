"""Tests for topic extraction, research drafting, and NoteDiscovery writes."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

os.environ.setdefault("LLM_URL", "http://localhost:8080/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")
os.environ.setdefault("NOTIFICATION_MODE", "off")
os.environ.setdefault("SAGASHI_MCP_URL", "http://localhost:9092/mcp")

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from koemichi.services.workflows import research
from koemichi.shared.models.note import Note, NoteSource, NoteStatus
from koemichi.shared.notifications import research_draft_notification


@pytest.fixture
def database_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    """Provide an isolated database for persisted research draft tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'research.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(research, "engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _note(
    *,
    status: NoteStatus = NoteStatus.ROUTED,
    lease: datetime | None = datetime(2025, 1, 1, tzinfo=UTC),
    transcript: str = "research the history of the Berlin U-Bahn",
) -> Note:
    """Build a persisted research note snapshot."""
    return Note(
        id=uuid4(),
        source=NoteSource.MEMO,
        status=status,
        lease_expires_at=lease,
        recorded_at_ms=1_735_689_600_000,
        transcript=transcript,
        intent="research",
    )


def test_research_keywords_come_from_the_intent_registry() -> None:
    """The keyword fast path remains the single source of truth."""
    from koemichi.services.router.intents import INTENT_KEYWORDS, Intent

    assert set(research.RESEARCH_KEYWORDS) == set(INTENT_KEYWORDS[Intent.RESEARCH])
    # Longest first so multi-word phrases win.
    assert research.RESEARCH_KEYWORDS == tuple(
        sorted(research.RESEARCH_KEYWORDS, key=len, reverse=True)
    )


def test_topic_extraction_strips_the_keyword_deterministically() -> None:
    """Keep the topic verbatim after the matched keyword phrase."""
    assert (
        research.extract_research_topic("research the history of the Berlin U-Bahn")
        == "the history of the Berlin U-Bahn"
    )
    assert (
        research.extract_research_topic("Look up: best sourdough hydration")
        == "best sourdough hydration"
    )
    assert (
        research.extract_research_topic("SEARCH  waschmaschine test 2025")
        == "waschmaschine test 2025"
    )


def test_topic_extraction_preserves_case_and_inner_spacing() -> None:
    """Only the keyword prefix is removed; everything else stays raw."""
    transcript = "Recherche: Bundestagswahl 2025 Ergebnisse"
    assert research.extract_research_topic(transcript) == (
        "Bundestagswahl 2025 Ergebnisse"
    )


@pytest.mark.parametrize("transcript", ["research", "look up", "search:", None, "  "])
def test_topic_extraction_rejects_missing_topics(transcript: str | None) -> None:
    """A bare keyword or empty transcript cannot be researched."""
    with pytest.raises(ValueError):
        research.extract_research_topic(transcript)


def test_topic_extraction_rejects_non_research_transcripts() -> None:
    """Transcripts without a research keyword prefix are refused."""
    with pytest.raises(ValueError):
        research.extract_research_topic("Remember the bakery meeting")


def test_research_result_schema_requires_sources() -> None:
    """A summary without sources or with extra fields is rejected."""
    assert (
        research.ResearchResult(
            title="Berlin U-Bahn",
            summary="A summary.",
            sources=[
                {"title": "Wikipedia", "url": "https://en.wikipedia.org/wiki/U-Bahn"}
            ],
        )
        .sources[0]
        .title
        == "Wikipedia"
    )
    with pytest.raises(ValueError):
        research.ResearchResult.model_validate(
            {"title": "Berlin U-Bahn", "summary": "A summary.", "sources": []}
        )
    with pytest.raises(ValueError):
        research.ResearchResult.model_validate(
            {
                "title": "Berlin U-Bahn",
                "summary": "A summary.",
                "sources": [{"title": "x", "url": "https://example.com"}],
                "extra": "field",
            }
        )


def test_research_note_path_uses_configured_folder_and_pretty_title() -> None:
    """Place research summaries under the configured folder, memo-style."""
    assert (
        research.research_note_path(1_735_689_600_000, "Berlin U-Bahn", ZoneInfo("UTC"))
        == "Research/2025-01-01 00:00 - Berlin U-Bahn.md"
    )


def test_research_content_contains_question_summary_and_sources() -> None:
    """Render question, summary, and one source line per source."""
    content = research.render_research_content(
        "2025-01-01 00:00 - Berlin U-Bahn",
        "the history of the Berlin U-Bahn",
        "The U-Bahn opened in 1902.",
        [research.ResearchSource(title="Wikipedia", url="https://example.com/u")],
    )
    assert content.startswith("# 2025-01-01 00:00 - Berlin U-Bahn")
    assert "**Research question:** the history of the Berlin U-Bahn" in content
    assert "- [Wikipedia](https://example.com/u)" in content


@pytest.mark.asyncio
async def test_choose_available_research_path_adds_suffixes() -> None:
    """Choose the first readable timestamped path not already in the vault."""

    class ExistingNotes:
        async def note_exists(self, note_path: str) -> bool:
            return note_path.endswith(".md") and "(2)" not in note_path

    path = await research.choose_available_research_path(
        ExistingNotes(),  # type: ignore[arg-type]
        "Research/2025-01-01 00:00 - Berlin U-Bahn.md",
    )

    assert path == "Research/2025-01-01 00:00 - Berlin U-Bahn (2).md"


def test_research_draft_notification_is_debug_only_and_safe() -> None:
    """Research progress notifications contain no transcript or summary text."""
    note_id = uuid4()
    notification = research_draft_notification(note_id)

    assert notification.event_key == f"{note_id}:research-draft"
    assert notification.debug_only is True
    assert notification.priority == -1
    assert note_id.hex[:8] in notification.message


def test_validate_research_config_reports_missing_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow fails explicitly when Sagashi or LLM config is missing."""
    monkeypatch.setattr(research, "SAGASHI_MCP_URL", None)
    with pytest.raises(RuntimeError, match="SAGASHI_MCP_URL"):
        research.validate_research_config()


@pytest.mark.asyncio
async def test_handle_research_persists_result_before_writing(
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Run the agent once, persist the draft, then write to NoteDiscovery."""
    raw_transcript = "research the history of the Berlin U-Bahn"
    note = _note(transcript=raw_transcript)
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    sources = [
        research.ResearchSource(
            title="Wikipedia", url="https://en.wikipedia.org/wiki/Berlin_U-Bahn"
        )
    ]

    class FakeAgent:
        async def run(self, topic: str, **_: object) -> SimpleNamespace:
            assert topic == "the history of the Berlin U-Bahn"
            return SimpleNamespace(
                output=research.ResearchResult(
                    title="Berlin U-Bahn history",
                    summary="It opened in 1902.",
                    sources=sources,
                )
            )

    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, api_url: str) -> None:
            # The client must be constructed with the NoteDiscovery URL, never
            # the Sagashi MCP URL.
            assert api_url == "http://notes.test"

        async def note_exists(self, path: str) -> bool:
            calls.append(("exists", path))
            return False

        async def create_folder(self, path: str) -> dict[str, object]:
            calls.append(("folder", path))
            return {"success": True}

        async def create_or_update_note(
            self, path: str, content: str
        ) -> dict[str, object]:
            with Session(database_engine) as session:
                saved = session.get(Note, note.id)
                assert saved is not None
                assert saved.research_path == path
                assert saved.research_content == content
            calls.append(("write", path))
            return {"success": True}

    monkeypatch.setattr(research, "_get_research_agent", lambda: FakeAgent())  # type: ignore[assignment,return-value]
    monkeypatch.setattr(research, "NoteDiscoveryClient", FakeClient)
    monkeypatch.setattr(research, "NOTEDISCOVERY_API_URL", "http://notes.test")
    monkeypatch.setattr(research, "SAGASHI_MCP_URL", "http://sagashi.test/mcp")
    monkeypatch.setattr(research, "TIMEZONE", ZoneInfo("UTC"))

    await research.handle_research(note)

    pretty_title = "2025-01-01 00:00 - Berlin U-Bahn history"
    expected_path = f"Research/{pretty_title}.md"
    assert note.research_path == expected_path
    assert calls == [
        ("exists", expected_path),
        ("folder", "Research"),
        ("write", expected_path),
    ]
    assert raw_transcript not in caplog.text
    assert "It opened in 1902." not in caplog.text

    with Session(database_engine) as session:
        saved = session.get(Note, note.id)
    assert saved is not None
    assert saved.research_title == pretty_title
    assert "**Research question:** the history of the Berlin U-Bahn" in (
        saved.research_content or ""
    )


@pytest.mark.asyncio
async def test_retry_reuses_persisted_research_without_calling_agent(
    database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries write the exact persisted title, content, and destination."""
    note = _note()
    note.research_title = "2025-01-01 00:00 - Saved research"
    note.research_content = "# 2025-01-01 00:00 - Saved research\n\nSaved summary"
    note.research_path = f"Research/{note.research_title}.md"
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    class FakeClient:
        def __init__(self, api_url: str) -> None:
            assert api_url == "http://notes.test"

        async def create_folder(self, path: str) -> dict[str, object]:
            assert path == "Research"
            return {"success": True}

        async def create_or_update_note(
            self, path: str, content: str
        ) -> dict[str, object]:
            assert path == note.research_path
            assert content == note.research_content
            return {"success": True}

    def unexpected_agent() -> object:
        raise AssertionError("retry must not rerun the research agent")

    monkeypatch.setattr(research, "_get_research_agent", unexpected_agent)
    monkeypatch.setattr(research, "NoteDiscoveryClient", FakeClient)
    monkeypatch.setattr(research, "NOTEDISCOVERY_API_URL", "http://notes.test")
    monkeypatch.setattr(research, "SAGASHI_MCP_URL", "http://sagashi.test/mcp")

    await research.handle_research(note)


@pytest.mark.asyncio
async def test_handle_research_rejects_bare_keyword(
    database_engine: Engine,
) -> None:
    """A transcript with only the keyword fails before any side effect."""
    note = _note(transcript="research")
    with pytest.raises(ValueError, match="no topic"):
        await research.handle_research(note)


def test_persist_research_draft_requires_current_worker_lease(
    database_engine: Engine,
) -> None:
    """Do not persist generated output after the worker lease has gone stale."""
    note = _note()
    with Session(database_engine) as session:
        session.add(note)
        session.commit()
        session.refresh(note)

    note.lease_expires_at = datetime(2025, 1, 2, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="lease may be stale"):
        research.persist_research_draft(note, "t", "c", "Research/t.md")


def test_budget_guard_replaces_over_budget_tool_results() -> None:
    """The budget callback swaps the tool result for a finalize message."""
    import asyncio

    async def call_next(name: str, args: dict[str, object], metadata=None) -> str:
        return f"result:{name}"

    async def scenario() -> tuple[list[str], str]:
        state = {"calls": 0}
        guard = research._budget_guard(state)
        results = []
        for _ in range(research.RESEARCH_MAX_TOOL_CALLS + 1):
            results.append(await guard(None, call_next, "web_search", {}))  # type: ignore[arg-type]
        return results, await guard(None, call_next, "fetch_page", {})  # type: ignore[arg-type]

    results, last = asyncio.run(scenario())

    assert all(result == "result:web_search" for result in results[:-1])
    assert "budget exhausted" in results[-1].lower()
    assert "budget exhausted" in last.lower()
