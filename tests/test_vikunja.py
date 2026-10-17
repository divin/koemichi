"""Tests for the MCP-based Vikunja client used by the todo workflow."""

import json

import pytest
from fastmcp import FastMCP

from koemichi.integrations.vikunja import VikunjaClient, canonical_due_date


def _fake_vikunja_mcp() -> FastMCP:
    """Build an in-process stand-in for Vikunja's built-in MCP tools."""
    fake = FastMCP("Fake Vikunja MCP")

    @fake.tool
    async def tasks_create(
        project: int,
        title: str,
        description: str | None = None,
        due_date: str | None = None,
        format: str = "html",
    ) -> dict[str, object]:
        return {
            "id": 42,
            "project_id": project,
            "title": title,
            "description": description,
            "due_date": due_date,
            "format": format,
        }

    @fake.tool
    async def tasks_list(
        filter: str | None = None,
        page: int = 1,
        per_page: int | None = None,
        format: str = "html",
    ) -> str:
        if page > 1:
            items = [{"id": 7, "title": "Pay rent"}]
        else:
            items = []
        return json.dumps(
            {
                "items": items,
                "filter": filter,
                "page": page,
                "per_page": per_page,
                "total": len(items),
                "total_pages": 2,
            }
        )

    return fake


@pytest.mark.asyncio
async def test_add_task_binds_project_and_markdown_format() -> None:
    """Create tasks in the configured project with the minimal input surface."""
    client = VikunjaClient(
        "https://vikunja.test/api/v2/mcp",
        token="tk_secret",
        project_id=7,
        transport=_fake_vikunja_mcp(),
    )

    created = await client.add_task(
        "Buy milk", description="At the corner shop", due_date="2026-03-14"
    )

    assert created == {
        "id": 42,
        "project_id": 7,
        "title": "Buy milk",
        "description": "At the corner shop",
        "due_date": "2026-03-14T00:00:00Z",
        "format": "markdown",
    }


@pytest.mark.asyncio
async def test_add_task_requires_nonempty_title() -> None:
    """Reject a blank title before invoking Vikunja."""
    client = VikunjaClient(
        "https://vikunja.test/api/v2/mcp",
        token="tk_secret",
        project_id=7,
        transport=_fake_vikunja_mcp(),
    )

    with pytest.raises(ValueError, match="title"):
        await client.add_task("   ")


@pytest.mark.asyncio
async def test_list_tasks_parses_catalog_json_and_passes_pagination() -> None:
    """Page through the inbox project and coerce the JSON tool result."""
    client = VikunjaClient(
        "https://vikunja.test/api/v2/mcp",
        token="tk_secret",
        project_id=7,
        transport=_fake_vikunja_mcp(),
    )

    first = await client.list_tasks(page=1, per_page=50)
    second = await client.list_tasks(page=2)

    assert first == {
        "items": [],
        "filter": "project_id=7",
        "page": 1,
        "per_page": 50,
        "total": 0,
        "total_pages": 2,
    }
    assert isinstance(second, dict)
    assert second["items"] == [{"id": 7, "title": "Pay rent"}]
    assert second["page"] == 2
    assert second["filter"] == "project_id=7"


@pytest.mark.asyncio
async def test_tool_failures_propagate() -> None:
    """Preserve Vikunja MCP tool errors for worker retries."""
    fake = FastMCP("Fake Vikunja MCP")

    @fake.tool
    async def tasks_create(
        project: int,
        title: str,
        description: str | None = None,
        due_date: str | None = None,
        format: str = "html",
    ) -> dict[str, object]:
        raise RuntimeError("vikunja exploded")

    client = VikunjaClient(
        "https://vikunja.test/api/v2/mcp",
        token="tk_secret",
        project_id=7,
        transport=fake,
    )

    with pytest.raises(Exception, match="vikunja exploded"):
        await client.add_task("Buy milk")


def test_canonical_due_date_accepts_dates_and_datetimes() -> None:
    """Normalize date-only values to Vikunja's date-time format."""
    assert canonical_due_date("2026-03-14") == "2026-03-14T00:00:00Z"
    assert canonical_due_date(" 2026-03-14 ") == "2026-03-14T00:00:00Z"
    assert canonical_due_date("2026-03-14T23:30:00Z") == "2026-03-14T23:30:00Z"
    assert (
        canonical_due_date("2026-03-14T23:30:00+02:00") == "2026-03-14T23:30:00+02:00"
    )


def test_canonical_due_date_rejects_invalid_or_vague_values() -> None:
    """Refuse non-ISO due dates such as relative deadlines."""
    for value in ("tomorrow", "next week", "2026-13-45", "March 14th", ""):
        with pytest.raises(ValueError):
            canonical_due_date(value)


def test_constructor_requires_absolute_url_token_and_project() -> None:
    """Reject invalid MCP URLs, empty tokens, and non-positive project ids."""
    with pytest.raises(ValueError, match="absolute HTTP"):
        VikunjaClient("not-a-url", token="tk", project_id=1)
    with pytest.raises(ValueError, match="query or fragment"):
        VikunjaClient("https://vikunja.test/api/v2/mcp?x=1", token="tk", project_id=1)
    with pytest.raises(ValueError, match="token"):
        VikunjaClient("https://vikunja.test/api/v2/mcp", token="", project_id=1)
    with pytest.raises(ValueError, match="positive integer"):
        VikunjaClient("https://vikunja.test/api/v2/mcp", token="tk", project_id=0)


@pytest.mark.asyncio
async def test_pagination_rejects_negative_or_zero_values() -> None:
    """Reject invalid pagination parameters before invoking Vikunja."""
    client = VikunjaClient(
        "https://vikunja.test/api/v2/mcp",
        token="tk_secret",
        project_id=7,
        transport=_fake_vikunja_mcp(),
    )

    with pytest.raises(ValueError, match="page"):
        await client.list_tasks(page=0)
    with pytest.raises(ValueError, match="per_page"):
        await client.list_tasks(per_page=0)
