"""MCP tools for adding and listing tasks in the configured Vikunja inbox.

The tools proxy Vikunja's built-in MCP server (``/api/v2/mcp``) and expose only
a minimal surface: ``add_task`` takes just ``title``, ``description``, and
``due_date``; the project id, API token, and endpoint come from configuration
and are never agent inputs.
"""

import logging

from fastmcp import FastMCP

from koemichi.integrations.vikunja import (
    VIKUNJA_MCP_PATH,
    JsonResponse,
    VikunjaClient,
)
from koemichi.shared.settings import (
    VIKUNJA_API_TOKEN,
    VIKUNJA_API_URL,
    VIKUNJA_MCP_URL,
    VIKUNJA_PROJECT_ID,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("Koemichi Vikunja")

_configured_client: VikunjaClient | None = None


def get_configured_client() -> VikunjaClient:
    """Build and cache a Vikunja API client from the configured inbox settings.

    Returns
    -------
    VikunjaClient
        Lazily created client bound to the configured endpoint and project.

    Raises
    ------
    RuntimeError
        If a required Vikunja setting is missing.
    """
    global _configured_client
    if _configured_client is not None:
        return _configured_client
    if not VIKUNJA_API_URL or not VIKUNJA_API_TOKEN or VIKUNJA_PROJECT_ID is None:
        raise RuntimeError("Vikunja configuration is incomplete")
    mcp_url = VIKUNJA_MCP_URL or f"{VIKUNJA_API_URL.rstrip('/')}{VIKUNJA_MCP_PATH}"
    logger.info("Connecting to Vikunja MCP endpoint %s", mcp_url)
    _configured_client = VikunjaClient(
        mcp_url,
        token=VIKUNJA_API_TOKEN,
        project_id=VIKUNJA_PROJECT_ID,
    )
    return _configured_client


@mcp.tool
async def add_task(
    title: str,
    description: str | None = None,
    due_date: str | None = None,
) -> JsonResponse:
    """Add a task to the configured Vikunja inbox project.

    Provide a non-empty ``title``. Optionally provide a short ``description``
    and a ``due_date`` as an ISO 8601 date (YYYY-MM-DD) or datetime.
    The project, API token, and MCP endpoint come from configuration and are
    not parameters.
    """
    return await get_configured_client().add_task(
        title, description=description, due_date=due_date
    )


@mcp.tool
async def list_tasks(page: int = 1, per_page: int | None = None) -> JsonResponse:
    """List tasks in the configured Vikunja inbox project.

    Use this to check whether a task with the same title already exists before
    creating a new one. Results are paginated with ``page`` (1-based) and
    ``per_page``.
    """
    return await get_configured_client().list_tasks(page=page, per_page=per_page)


if __name__ == "__main__":
    mcp.run()
