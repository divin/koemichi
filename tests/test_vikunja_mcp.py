"""Tests for the Vikunja MCP tools and their in-process pydantic-ai wiring."""

import os

os.environ.setdefault("VIKUNJA_API_URL", "https://vikunja.test")
os.environ.setdefault("VIKUNJA_API_TOKEN", "tk_test")
os.environ.setdefault("VIKUNJA_PROJECT_ID", "7")
os.environ.setdefault("NOTIFICATION_MODE", "off")

from typing import cast

import pytest
from fastmcp import Client, FastMCP

from koemichi.integrations.vikunja import VikunjaClient
from koemichi.services.mcp import vikunja as vikunja_mcp_module
from koemichi.services.mcp.vikunja import mcp


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
            "id": 5,
            "project_id": project,
            "title": title,
            "description": description,
            "due_date": due_date,
            "format": format,
        }

    return fake


@pytest.mark.asyncio
async def test_mcp_server_registers_vikunja_tools() -> None:
    """Expose only the task creation and listing operations over MCP."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == {"add_task", "list_tasks"}
    schema = by_name["add_task"].input_schema
    assert set(schema["properties"]) == {"title", "description", "due_date"}
    assert schema["required"] == ["title"]


@pytest.mark.asyncio
async def test_add_task_tool_creates_task_in_configured_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxy Vikunja's built-in MCP server with the project bound from config."""
    monkeypatch.setattr(
        vikunja_mcp_module,
        "get_configured_client",
        lambda: VikunjaClient(
            "https://vikunja.test/api/v2/mcp",
            token="tk_test",
            project_id=7,
            transport=_fake_vikunja_mcp(),
        ),
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "add_task", {"title": "Buy milk", "due_date": "2026-03-14"}
        )

    assert result.data == {
        "id": 5,
        "project_id": 7,
        "title": "Buy milk",
        "description": None,
        "due_date": "2026-03-14T00:00:00Z",
        "format": "markdown",
    }


@pytest.mark.asyncio
async def test_vikunja_tools_are_available_to_pydantic_ai_in_process() -> None:
    """Attach the importable FastMCP server to a pydantic-ai toolset directly."""
    from pydantic_ai.mcp import FastMCPClient, MCPToolset

    toolset = MCPToolset(FastMCPClient(mcp))
    async with toolset:
        names = [tool.name for tool in await toolset.list_tools()]

    assert names == ["add_task", "list_tasks"]


def test_configured_client_requires_full_vikunja_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail fast when the todo integration is not configured."""
    monkeypatch.setattr(vikunja_mcp_module, "_configured_client", None)
    monkeypatch.setattr(vikunja_mcp_module, "VIKUNJA_API_URL", None)
    monkeypatch.setattr(vikunja_mcp_module, "VIKUNJA_API_TOKEN", "tk_test")
    monkeypatch.setattr(vikunja_mcp_module, "VIKUNJA_PROJECT_ID", 7)

    with pytest.raises(RuntimeError, match="incomplete"):
        vikunja_mcp_module.get_configured_client()


def test_configured_client_derives_mcp_url_from_api_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default the MCP endpoint to ``{API_URL}/api/v2/mcp`` when unset."""
    built_urls: list[str] = []

    def fake_vikunja_client(url: str, **_: object) -> VikunjaClient:
        built_urls.append(url)
        # The cached placeholder is never invoked by this test.
        return cast(VikunjaClient, object())

    monkeypatch.setattr(vikunja_mcp_module, "_configured_client", None)
    monkeypatch.setattr(vikunja_mcp_module, "VIKUNJA_MCP_URL", None)
    monkeypatch.setattr(vikunja_mcp_module, "VIKUNJA_API_URL", "https://vikunja.test")
    monkeypatch.setattr(vikunja_mcp_module, "VIKUNJA_API_TOKEN", "tk_test")
    monkeypatch.setattr(vikunja_mcp_module, "VIKUNJA_PROJECT_ID", 7)
    monkeypatch.setattr(vikunja_mcp_module, "VikunjaClient", fake_vikunja_client)

    vikunja_mcp_module.get_configured_client()

    assert "https://vikunja.test/api/v2/mcp" in built_urls
