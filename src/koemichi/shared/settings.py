"""Load storage paths, service URLs, and secrets from the process environment."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from koemichi.shared.models.notification import NotificationMode

load_dotenv()


def _required_env(name: str) -> str:
    """Read a required, non-empty environment variable.

    Parameters
    ----------
    name : str
        Name of the environment variable to read.

    Returns
    -------
    str
        Configured value.

    Raises
    ------
    RuntimeError
        If the variable is unset or empty.
    """
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be set and non-empty")
    return value


DATA_DIR: Path = Path(os.getenv("DATA_DIR") or "data")
AUDIO_STORAGE_ROOT: Path = DATA_DIR
MEMO_SAVE_DIR: Path = DATA_DIR / "memos"
ACTION_SAVE_DIR: Path = DATA_DIR / "actions"
DATABASE_PATH: Path = Path(os.getenv("DATABASE_PATH") or DATA_DIR / "koemichi.db")

WEBHOOK_TOKEN: str = _required_env("WEBHOOK_TOKEN")
STT_URL: str = _required_env("STT_URL")
STT_MODEL_NAME: str = _required_env("STT_MODEL_NAME")

# Local LLM endpoint for intent classification. LLM_URL must be the
# OpenAI-compatible API base of the local llama.cpp server, e.g. http://llm:8080/v1.
LLM_URL: str | None = os.getenv("LLM_URL")
LLM_MODEL_NAME: str | None = os.getenv("LLM_MODEL_NAME")

# n8n dispatch webhook (the seam): the router POSTs {note_id, transcript,
# intent} here. Optional in settings; validated when the router worker starts.
N8N_WEBHOOK_URL: str | None = os.getenv("N8N_WEBHOOK_URL")
TIMEZONE: ZoneInfo = ZoneInfo(os.getenv("TZ") or "Europe/Berlin")

PUSHOVER_API_TOKEN: str | None = os.getenv("PUSHOVER_API_TOKEN") or None
PUSHOVER_USER_KEY: str | None = os.getenv("PUSHOVER_USER_KEY") or None
NOTIFICATION_MODE = NotificationMode(
    (os.getenv("NOTIFICATION_MODE") or NotificationMode.NORMAL.value).strip().lower()
)
