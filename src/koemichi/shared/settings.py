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
TIMEZONE = ZoneInfo(os.getenv("TZ") or "Europe/Berlin")

# Local LLM endpoint for intent classification, if keyword matching does not apply.
LLM_URL: str | None = os.getenv("LLM_URL")
LLM_MODEL_NAME: str | None = os.getenv("LLM_MODEL_NAME")

PUSHOVER_API_TOKEN: str | None = os.getenv("PUSHOVER_API_TOKEN") or None
PUSHOVER_USER_KEY: str | None = os.getenv("PUSHOVER_USER_KEY") or None
NOTIFICATION_MODE = NotificationMode(
    (os.getenv("NOTIFICATION_MODE") or NotificationMode.NORMAL.value).strip().lower()
)
