"""Tests for the NoteDiscovery API client used by MCP tools."""

import httpx
import pytest
from fastmcp import Client

from koemichi.integrations.notediscovery import NoteDiscoveryClient
from koemichi.services.mcp.server import mcp


@pytest.mark.asyncio
async def test_note_api_operations_use_expected_routes_and_payloads() -> None:
    """Verify MCP-facing client methods map to the documented REST API."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    client = NoteDiscoveryClient(
        "http://notes.test",
        transport=httpx.MockTransport(handler),
    )

    await client.list_notes(limit=10, offset=20)
    await client.get_note("ideas/my memo.md", include_backlinks=False)
    await client.create_or_update_note("ideas/new.md", "# New")
    await client.append_to_note("ideas/log.md", "Entry", add_timestamp=True)
    await client.search_notes("voice notes", limit=5, offset=10)

    assert [
        (request.method, request.url.raw_path.split(b"?")[0].decode())
        for request in requests
    ] == [
        ("GET", "/api/notes"),
        ("GET", "/api/notes/ideas/my%20memo.md"),
        ("POST", "/api/notes/ideas/new.md"),
        ("PATCH", "/api/notes/ideas/log.md"),
        ("GET", "/api/search"),
    ]
    assert dict(requests[0].url.params) == {"offset": "20", "limit": "10"}
    assert dict(requests[1].url.params) == {"include_backlinks": "false"}
    assert requests[2].read() == b'{"content":"# New"}'
    assert requests[3].read() == b'{"content":"Entry","add_timestamp":true}'
    assert dict(requests[4].url.params) == {
        "q": "voice notes",
        "offset": "10",
        "limit": "5",
    }


@pytest.mark.asyncio
async def test_note_path_rejects_absolute_and_traversal_paths() -> None:
    """Reject paths that could address files outside the notes vault."""
    client = NoteDiscoveryClient("http://notes.test")

    for path in ("/private.md", "../private.md", "folder/../../private.md", "a\\b"):
        with pytest.raises(ValueError):
            await client.get_note(path)
        with pytest.raises(ValueError):
            await client.create_folder(path)


@pytest.mark.asyncio
async def test_create_folder_uses_documented_endpoint() -> None:
    """Create folders through NoteDiscovery's non-destructive folders API."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    client = NoteDiscoveryClient(
        "http://notes.test",
        transport=httpx.MockTransport(handler),
    )

    await client.create_folder("Journal/2026/10")

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/folders"
    assert requests[0].read() == b'{"path":"Journal/2026/10"}'


@pytest.mark.asyncio
async def test_note_exists_returns_false_only_for_not_found() -> None:
    """Treat HTTP 404 as an available name but propagate other API failures."""
    missing = NoteDiscoveryClient(
        "http://notes.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={"detail": "not found"})
        ),
    )
    broken = NoteDiscoveryClient(
        "http://notes.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"detail": "unavailable"})
        ),
    )

    assert await missing.note_exists("Memo/new.md") is False
    with pytest.raises(httpx.HTTPStatusError, match="503"):
        await broken.note_exists("Memo/new.md")


@pytest.mark.asyncio
async def test_api_http_errors_are_propagated() -> None:
    """Preserve HTTP failures so MCP clients can observe failed API calls."""
    client = NoteDiscoveryClient(
        "http://notes.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={"detail": "not found"})
        ),
    )

    with pytest.raises(httpx.HTTPStatusError, match="404"):
        await client.get_note("missing.md")


@pytest.mark.asyncio
async def test_pagination_rejects_negative_or_zero_values() -> None:
    """Reject invalid pagination parameters before making an HTTP request."""
    client = NoteDiscoveryClient("http://notes.test")

    with pytest.raises(ValueError, match="offset"):
        await client.list_notes(offset=-1)
    with pytest.raises(ValueError, match="limit"):
        await client.search_notes("memo", limit=0)


@pytest.mark.asyncio
async def test_mcp_server_registers_note_api_tools() -> None:
    """Expose the expected NoteDiscovery operations over the MCP protocol."""
    async with Client(mcp) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools} == {
        "search_notes",
        "list_notes",
        "get_note",
    }


def test_base_url_must_be_absolute_http_url() -> None:
    """Reject an invalid API base URL at client construction time."""
    with pytest.raises(ValueError, match="absolute HTTP"):
        NoteDiscoveryClient("not-a-url")
