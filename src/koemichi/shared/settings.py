"""Load storage paths, service URLs, and secrets from the process environment."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from koemichi.shared.models.notification import NotificationMode

load_dotenv()


DATA_DIR: Path = Path(os.getenv("DATA_DIR") or "data")
DATABASE_PATH: Path = Path(os.getenv("DATABASE_PATH") or DATA_DIR / "koemichi.db")

TRANSCRIPT_INGEST_TOKEN: str | None = os.getenv("TRANSCRIPT_INGEST_TOKEN") or None
NOTEDISCOVERY_API_URL: str | None = os.getenv("NOTEDISCOVERY_API_URL") or None
NOTEDISCOVERY_MEMO_FOLDER: str = os.getenv("NOTEDISCOVERY_MEMO_FOLDER") or "Memo"
NOTEDISCOVERY_JOURNAL_FOLDER: str = (
    os.getenv("NOTEDISCOVERY_JOURNAL_FOLDER") or "Journal"
)
NOTEDISCOVERY_RESEARCH_FOLDER: str = (
    os.getenv("NOTEDISCOVERY_RESEARCH_FOLDER") or "Research"
)

# Vikunja inbox used by the todo workflow and its MCP tools.
VIKUNJA_API_URL: str | None = os.getenv("VIKUNJA_API_URL") or None
VIKUNJA_API_TOKEN: str | None = os.getenv("VIKUNJA_API_TOKEN") or None
_vikunja_project_id = os.getenv("VIKUNJA_PROJECT_ID")
VIKUNJA_PROJECT_ID: int | None = (
    int(_vikunja_project_id) if _vikunja_project_id else None
)
# Optional override for Vikunja's built-in MCP endpoint; defaults to
# ``{VIKUNJA_API_URL}/api/v2/mcp``.
VIKUNJA_MCP_URL: str | None = os.getenv("VIKUNJA_MCP_URL") or None

TIMEZONE = ZoneInfo(os.getenv("TZ") or "Europe/Berlin")

# Local LLM endpoint for intent classification, if keyword matching does not apply.
LLM_URL: str | None = os.getenv("LLM_URL")
LLM_MODEL_NAME: str | None = os.getenv("LLM_MODEL_NAME")

# Sagashi MCP server used by the research workflow's read-only search tools.
SAGASHI_MCP_URL: str | None = os.getenv("SAGASHI_MCP_URL") or None

# Bounded research agent loop: soft tool-call budget (the agent is asked to
# finalize its summary once spent) and a hard wall-clock timeout per run.
RESEARCH_MAX_TOOL_CALLS: int = int(os.getenv("RESEARCH_MAX_TOOL_CALLS") or 10)
RESEARCH_TIMEOUT_SECONDS: int = int(os.getenv("RESEARCH_TIMEOUT_SECONDS") or 300)

PUSHOVER_API_TOKEN: str | None = os.getenv("PUSHOVER_API_TOKEN") or None
PUSHOVER_USER_KEY: str | None = os.getenv("PUSHOVER_USER_KEY") or None
NOTIFICATION_MODE = NotificationMode(
    (os.getenv("NOTIFICATION_MODE") or NotificationMode.NORMAL.value).strip().lower()
)
