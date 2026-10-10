"""Read-only MCP tools for searching and reading NoteDiscovery notes."""

from fastmcp import FastMCP

from koemichi.integrations.notediscovery import JsonResponse, NoteDiscoveryClient
from koemichi.shared.settings import NOTEDISCOVERY_API_URL

mcp = FastMCP("Koemichi NoteDiscovery")


def _client() -> NoteDiscoveryClient:
    """Create a NoteDiscovery API client from configured settings."""
    if not NOTEDISCOVERY_API_URL:
        raise RuntimeError("NOTEDISCOVERY_API_URL is not configured")
    return NoteDiscoveryClient(NOTEDISCOVERY_API_URL)


@mcp.tool
async def search_notes(
    query: str, limit: int | None = None, offset: int = 0
) -> JsonResponse:
    """Search NoteDiscovery note contents for a query.

    The search is case-insensitive. Queries shorter than two characters return
    no results according to the NoteDiscovery API.
    """
    return await _client().search_notes(query, limit=limit, offset=offset)


@mcp.tool
async def list_notes(limit: int | None = None, offset: int = 0) -> JsonResponse:
    """List NoteDiscovery notes, optionally with limit/offset pagination."""
    return await _client().list_notes(limit=limit, offset=offset)


@mcp.tool
async def get_note(note_path: str, include_backlinks: bool = True) -> JsonResponse:
    """Read a note's content and metadata using a vault-relative path."""
    return await _client().get_note(note_path, include_backlinks=include_backlinks)


__all__ = ["mcp"]
