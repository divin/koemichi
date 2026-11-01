"""Deterministic Vikunja workflow using the transcript as the task title."""

import logging
from typing import Any, Protocol, cast

from sqlalchemy import update
from sqlmodel import Session

from koemichi.integrations.vikunja import JsonResponse
from koemichi.services.mcp.vikunja import get_configured_client
from koemichi.shared.db import engine
from koemichi.shared.models.note import Note, NoteStatus
from koemichi.shared.notifications import enqueue_notification, todo_draft_notification
from koemichi.shared.settings import (
    VIKUNJA_API_TOKEN,
    VIKUNJA_API_URL,
    VIKUNJA_PROJECT_ID,
)

logger = logging.getLogger(__name__)
columns = cast(Any, Note).__table__.c
MAX_TASK_LIST_PAGES = 100


class TaskLister(Protocol):
    """Minimal API used to list tasks in the dedicated Vikunja inbox."""

    async def list_tasks(
        self, *, page: int = 1, per_page: int | None = None
    ) -> JsonResponse:
        """List the inbox project's tasks, page by page."""
        ...


def validate_todo_config() -> None:
    """Validate the Vikunja settings required by the todo workflow.

    Raises
    ------
    RuntimeError
        If a required Vikunja setting is missing.
    """
    if not VIKUNJA_API_URL:
        raise RuntimeError("Todo workflow config missing: VIKUNJA_API_URL")
    if not VIKUNJA_API_TOKEN:
        raise RuntimeError("Todo workflow config missing: VIKUNJA_API_TOKEN")
    if VIKUNJA_PROJECT_ID is None:
        raise RuntimeError("Todo workflow config missing: VIKUNJA_PROJECT_ID")


def task_title_key(title: str) -> str:
    """Normalize task titles for deterministic duplicate comparison."""
    return " ".join(title.strip().lower().split())


def _task_items(payload: JsonResponse) -> tuple[list[dict[str, Any]], int]:
    """Split a Vikunja task list response into items and total page count."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], 1
    if isinstance(payload, dict):
        items = payload.get("items")
        if not isinstance(items, list):
            raise TypeError("Vikunja task list returned no items array")
        total_pages = payload.get("total_pages")
        if not isinstance(total_pages, int) or total_pages < 1:
            total_pages = 1
        return [item for item in items if isinstance(item, dict)], total_pages
    raise TypeError("Vikunja task list has an unexpected shape")


async def task_title_exists(client: TaskLister, title: str) -> bool:
    """Return whether a task with the same normalized title exists in the inbox.

    Pages through the project's task list, which is the deterministic duplicate
    check the application performs before creating a task.
    """
    key = task_title_key(title)
    page = 1
    while page <= MAX_TASK_LIST_PAGES:
        payload = await client.list_tasks(page=page)
        items, total_pages = _task_items(payload)
        if any(task_title_key(item.get("title", "")) == key for item in items):
            return True
        if page >= total_pages:
            return False
        page += 1
    logger.warning("Task list pagination exceeded %s pages", MAX_TASK_LIST_PAGES)
    return False


def persist_todo_draft(note: Note, title: str) -> None:
    """Persist the transcript title only while the worker still owns the lease."""
    with Session(engine) as session:
        result = session.exec(
            update(Note)
            .where(
                columns.id == note.id,
                columns.status == NoteStatus.ROUTED,
                columns.lease_expires_at == note.lease_expires_at,
                columns.todo_title.is_(None),
                columns.todo_description.is_(None),
                columns.todo_due_date.is_(None),
            )
            .values(
                todo_title=title,
                todo_description=None,
                todo_due_date=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise RuntimeError(
                "Todo draft was not persisted; worker lease may be stale"
            )
        enqueue_notification(session, note.id, todo_draft_notification(note.id))
        session.commit()


async def handle_todo(note: Note) -> None:
    """Create a Vikunja inbox task whose title is the original transcript."""
    if not note.transcript or not note.transcript.strip():
        raise ValueError("Cannot create a todo from an empty transcript")
    validate_todo_config()
    client = get_configured_client()
    logger.info("Todo workflow started for note %s", note.id)

    if (
        note.todo_title is None
        and note.todo_description is None
        and note.todo_due_date is None
    ):
        title = note.transcript.strip()
        logger.info("Persisting transcript as todo title for note %s", note.id)
        persist_todo_draft(note, title)
        note.todo_title = title
        note.todo_description = None
        note.todo_due_date = None
        logger.info("Todo title persisted for note %s", note.id)
    elif not note.todo_title:
        raise RuntimeError("Persisted todo draft is incomplete")

    assert note.todo_title is not None
    if await task_title_exists(client, note.todo_title):
        logger.info("Todo task already present in Vikunja for note %s", note.id)
        return

    await client.add_task(
        note.todo_title,
        description=note.todo_description,
        due_date=note.todo_due_date,
    )
    logger.info("Todo task created in Vikunja for note %s", note.id)
