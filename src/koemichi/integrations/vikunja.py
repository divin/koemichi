"""MCP-based client for the Vikunja task API (v2).

Connects to Vikunja's built-in streamable-HTTP MCP server using an API token
and exposes only the inbox task operations Koemichi needs. The inbox project
is bound at construction time, so callers never choose the destination.
"""

import json
import logging
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic_ai.mcp import FastMCPClient, MCPToolset

JsonResponse = dict[str, Any] | list[Any]

logger = logging.getLogger(__name__)

VIKUNJA_MCP_PATH = "/api/v2/mcp"
VIKUNJA_RICH_FORMAT = "markdown"
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?$"
)


class VikunjaClient:
    """Call Vikunja's MCP ``tasks_create`` and page its inbox ``tasks_list``.

    Parameters
    ----------
    mcp_url : str
        Absolute URL of Vikunja's streamable-HTTP MCP endpoint, for example
        ``https://vikunja.example.com/api/v2/mcp``.
    token : str
        Vikunja API token (``tk_`` prefix) with ``mcp:access`` scope and task
        permissions for the inbox project.
    project_id : int
        Numeric id of the dedicated inbox project tasks are added to.
    transport : object or None, optional
        Internal override with the same interface as ``FastMCPClient``
        (for example an in-process FastMCP server) used by tests.
    timeout : float, optional
        Per-request timeout in seconds.

    Raises
    ------
    ValueError
        If the MCP URL, token, or project id is invalid.
    """

    def __init__(
        self,
        mcp_url: str,
        *,
        token: str,
        project_id: int,
        transport: Any | None = None,
        timeout: float = 30.0,
    ) -> None:
        parsed = urlsplit(mcp_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Vikunja MCP URL must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("Vikunja MCP URL must not include a query or fragment")
        if not token:
            raise ValueError("Vikunja API token must not be empty")
        if (
            not isinstance(project_id, int)
            or isinstance(project_id, bool)
            or project_id < 1
        ):
            raise ValueError("Vikunja project id must be a positive integer")

        self._project_id = project_id
        if transport is None:
            self._client = FastMCPClient(str(mcp_url), auth=token, timeout=timeout)
        else:
            self._client = FastMCPClient(transport, timeout=timeout)
        self._toolset = MCPToolset(self._client, tool_error_behavior="error")

    async def add_task(
        self,
        title: str,
        *,
        description: str | None = None,
        due_date: str | None = None,
    ) -> JsonResponse:
        """Create a task in the configured inbox project.

        Parameters
        ----------
        title : str
            Task title. Must be non-empty after stripping whitespace.
        description : str or None, optional
            Optional task description.
        due_date : str or None, optional
            Optional due date as an ISO 8601 date (``YYYY-MM-DD``) or
            datetime. Date-only values are sent as midnight UTC.

        Returns
        -------
        JsonResponse
            The created Vikunja task object.
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("Task title must not be empty")
        args: dict[str, Any] = {
            "project": self._project_id,
            "title": title,
            "format": VIKUNJA_RICH_FORMAT,
        }
        if description is not None:
            args["description"] = description
        if due_date is not None:
            args["due_date"] = canonical_due_date(due_date)
        result = await self._toolset.direct_call_tool("tasks_create", args)
        return _mcp_json(result)

    async def list_tasks(
        self, *, page: int = 1, per_page: int | None = None
    ) -> JsonResponse:
        """List the tasks in the configured inbox project.

        Parameters
        ----------
        page : int, optional
            1-based page number; must be positive.
        per_page : int or None, optional
            Items per page; must be positive when provided.

        Returns
        -------
        JsonResponse
            Vikunja's paginated task list object.
        """
        if page < 1:
            raise ValueError("Task list page must be one or greater")
        if per_page is not None and per_page < 1:
            raise ValueError("Task list per_page must be positive")
        args: dict[str, Any] = {
            "filter": f"project_id={self._project_id}",
            "page": page,
            "format": VIKUNJA_RICH_FORMAT,
        }
        if per_page is not None:
            args["per_page"] = per_page
        result = await self._toolset.direct_call_tool("tasks_list", args)
        return _mcp_json(result)


def canonical_due_date(value: str) -> str:
    """Validate and normalize an ISO 8601 due date to Vikunja's datetime format.

    Vikunja's ``due_date`` field accepts full datetimes; date-only values are
    sent as midnight UTC so the value always satisfies the API's date-time
    format.

    Raises
    ------
    ValueError
        If ``value`` is not a supported ISO 8601 date or datetime.
    """
    candidate = value.strip()
    if _DATE_ONLY.fullmatch(candidate):
        date.fromisoformat(candidate)
        return f"{candidate}T00:00:00Z"
    if _DATETIME.fullmatch(candidate):
        datetime.fromisoformat(candidate.replace("Z", "+00:00").replace("z", "+00:00"))
        return candidate
    raise ValueError("due_date must be an ISO 8601 date (YYYY-MM-DD) or datetime")


def _mcp_json(result: Any) -> JsonResponse:
    """Coerce an MCP tool result into a JSON response object.

    FastMCP's MCP client returns typed results as a dict and catalog-style
    results as a JSON string; older client versions may expose the same data
    on ``CallToolResult`` attributes.
    """
    if isinstance(result, (dict, list)):
        return result
    if isinstance(result, str):
        payload = json.loads(result)
        if not isinstance(payload, (dict, list)):
            raise TypeError("Vikunja MCP tool returned a non-JSON value")
        return payload
    for attribute in ("data", "structured_content"):
        value = getattr(result, attribute, None)
        if isinstance(value, (dict, list)):
            return value
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return _mcp_json(text)
    raise TypeError("Vikunja MCP tool returned an unexpected result shape")
