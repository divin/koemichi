"""HTTP client for the NoteDiscovery notes API."""

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

JsonResponse = dict[str, Any] | list[Any]


class NoteDiscoveryClient:
    """Call NoteDiscovery's note, search, and append endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialize a client for a NoteDiscovery instance.

        Parameters
        ----------
        base_url : str
            NoteDiscovery origin, for example ``http://notediscovery:8000``.
        timeout : float, optional
            Per-request timeout in seconds.

        Raises
        ------
        ValueError
            If ``base_url`` is not an absolute HTTP(S) URL.
        """
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("NoteDiscovery URL must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("NoteDiscovery URL must not include a query or fragment")

        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def list_notes(
        self, *, limit: int | None = None, offset: int = 0
    ) -> JsonResponse:
        """List notes, optionally using the API's limit/offset pagination."""
        _validate_pagination(limit, offset)
        params: dict[str, int] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", "/api/notes", params=params)

    async def create_folder(self, folder_path: str) -> JsonResponse:
        """Create a vault-relative folder using NoteDiscovery's folders API."""
        path = _validated_vault_path(folder_path)
        return await self._request("POST", "/api/folders", json={"path": path})

    async def note_exists(self, note_path: str) -> bool:
        """Return whether a note exists, propagating non-404 API failures."""
        try:
            await self.get_note(note_path, include_backlinks=False)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return False
            raise
        return True

    async def get_note(
        self, note_path: str, *, include_backlinks: bool = True
    ) -> JsonResponse:
        """Fetch note content and metadata by vault-relative path."""
        path = _validated_vault_path(note_path)
        return await self._request(
            "GET",
            f"/api/notes/{quote(path, safe='/')}",
            params={"include_backlinks": str(include_backlinks).lower()},
        )

    async def create_or_update_note(self, note_path: str, content: str) -> JsonResponse:
        """Create or replace a note using NoteDiscovery's POST endpoint."""
        path = _validated_vault_path(note_path)
        return await self._request(
            "POST",
            f"/api/notes/{quote(path, safe='/')}",
            json={"content": content},
        )

    async def append_to_note(
        self, note_path: str, content: str, *, add_timestamp: bool = False
    ) -> JsonResponse:
        """Append content to a note, optionally asking the API to add a timestamp."""
        path = _validated_vault_path(note_path)
        return await self._request(
            "PATCH",
            f"/api/notes/{quote(path, safe='/')}",
            json={"content": content, "add_timestamp": add_timestamp},
        )

    async def search_notes(
        self, query: str, *, limit: int | None = None, offset: int = 0
    ) -> JsonResponse:
        """Search note contents using NoteDiscovery's search endpoint."""
        _validate_pagination(limit, offset)
        params: dict[str, str | int] = {"q": query, "offset": offset}
        if limit is not None:
            params["limit"] = limit
        return await self._request("GET", "/api/search", params=params)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> JsonResponse:
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            response = await client.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=json,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise TypeError("NoteDiscovery returned a non-JSON-object response")
        return payload


def _validate_pagination(limit: int | None, offset: int) -> None:
    """Reject invalid pagination values before sending an API request."""
    if offset < 0:
        raise ValueError("Pagination offset must be zero or greater")
    if limit is not None and limit < 1:
        raise ValueError("Pagination limit must be greater than zero")


def _validated_vault_path(path_value: str) -> str:
    """Reject absolute and traversal paths before passing them to the API."""
    if not path_value or "\\" in path_value:
        raise ValueError("Path must be a non-empty vault-relative POSIX path")
    path = PurePosixPath(path_value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} for part in path_value.split("/")
    ):
        raise ValueError("Path must stay within the NoteDiscovery vault")
    return path_value
