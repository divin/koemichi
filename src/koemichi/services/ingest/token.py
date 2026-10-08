"""Bearer-token authentication dependency for ingest endpoints."""

import logging
from hmac import compare_digest

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from koemichi.shared.settings import WEBHOOK_TOKEN

logger = logging.getLogger(__name__)

bearer = HTTPBearer(auto_error=False)


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
) -> None:
    """Validate the Bearer credentials supplied with a webhook request.

    Parameters
    ----------
    credentials : HTTPAuthorizationCredentials or None
        Credentials parsed by ``HTTPBearer``. None if no valid Bearer
        credentials were provided.

    Returns
    -------
    None
        Returned when the supplied token matches the configured token.

    Raises
    ------
    HTTPException
        If credentials are missing or the token is invalid.
    """
    if credentials is None or not compare_digest(
        credentials.credentials, WEBHOOK_TOKEN
    ):
        logger.warning("Rejecting request with invalid token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
