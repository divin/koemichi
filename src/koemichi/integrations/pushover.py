"""Asynchronous client for Pushover message delivery."""

import httpx

from koemichi.shared.settings import PUSHOVER_API_TOKEN, PUSHOVER_USER_KEY

PUSHOVER_MESSAGE_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_TIMEOUT_SECONDS = 10


class PushoverError(Exception):
    """Sanitized Pushover delivery failure and retry classification.

    Parameters
    ----------
    message : str
        Safe diagnostic that does not include request credentials or response
        content.
    retryable : bool
        Whether the worker should retry this failure with bounded backoff.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        """Initialize an error with a safe message and retry policy.

        Parameters
        ----------
        message : str
            Diagnostic message that excludes credentials.
        retryable : bool
            Whether bounded retry should be attempted.
        """
        super().__init__(message)
        self.retryable = retryable


async def send_message(message: str, title: str, priority: int) -> None:
    """Send a form-encoded Pushover message and validate its JSON response.

    Parameters
    ----------
    message : str
        Message body, limited by Pushover to 1024 UTF-8 characters.
    title : str
        Message title, limited by Pushover to 250 characters.
    priority : int
        Pushover message priority from -2 through 2. Routine pipeline events
        use only -1, 0, or 1.

    Raises
    ------
    PushoverError
        If credentials are missing, Pushover rejects the request, or the
        request fails. Network errors and server errors are retryable; client
        errors and invalid responses are permanent.
    """
    if not PUSHOVER_API_TOKEN or not PUSHOVER_USER_KEY:
        raise PushoverError("Pushover credentials are not configured", retryable=False)

    data = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "message": message,
        "title": title,
        "priority": str(priority),
    }
    try:
        async with httpx.AsyncClient(timeout=PUSHOVER_TIMEOUT_SECONDS) as client:
            response = await client.post(PUSHOVER_MESSAGE_URL, data=data)
    except httpx.HTTPError as exc:
        raise PushoverError("Pushover network request failed", retryable=True) from exc

    if response.status_code >= 500:
        raise PushoverError(
            f"Pushover server error (HTTP {response.status_code})", retryable=True
        )
    if response.status_code >= 400:
        raise PushoverError(
            f"Pushover rejected the request (HTTP {response.status_code})",
            retryable=False,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise PushoverError(
            "Pushover returned an invalid response", retryable=False
        ) from exc

    if not isinstance(payload, dict) or payload.get("status") != 1:
        raise PushoverError("Pushover did not accept the message", retryable=False)
