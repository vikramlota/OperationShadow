"""Shared-secret authentication for vehicle WebSocket and manual override REST endpoints (§5.1, §9)."""

from __future__ import annotations

import hmac
from typing import Annotated

from core.config import settings
from fastapi import Header, HTTPException, Query, status


def is_valid_vehicle_token(candidate_token: str | None, expected_secret: str | None = None) -> bool:
    """Constant-time comparison of the shared-secret vehicle token."""
    secret = expected_secret if expected_secret is not None else settings.vehicle_shared_secret
    if not candidate_token or not secret:
        return False
    return hmac.compare_digest(candidate_token, secret)


async def verify_vehicle_token(
    x_vehicle_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> str:
    """FastAPI dependency enforcing shared-secret token authentication (§5.1, §9)."""
    candidate = x_vehicle_token
    if candidate is None and authorization is not None:
        scheme, _, param = authorization.partition(" ")
        if scheme.lower() == "bearer" and param:
            candidate = param.strip()
    if candidate is None:
        candidate = token

    if not is_valid_vehicle_token(candidate):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing vehicle shared-secret token.",
        )
    assert candidate is not None
    return candidate
