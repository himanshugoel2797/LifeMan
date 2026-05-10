"""Single-user bearer token authentication."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lifeman.config import settings

_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    # Allow unauthenticated access to UI pages (browser sessions)
    if request.url.path.startswith("/api/"):
        if creds is None or creds.credentials != settings.token:
            raise HTTPException(status_code=401, detail="Invalid or missing token")
    return "user"


def check_token(token: str) -> bool:
    return token == settings.token
