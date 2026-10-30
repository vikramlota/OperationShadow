"""Event log REST endpoint (§3, §7: GET /events?mission_id=)."""

from __future__ import annotations

from typing import Annotated

from db.session import get_db_session
from fastapi import APIRouter, Depends, Query, Request
from schemas.entities import EventRead
from services.event_service import EventService, event_service
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/events", tags=["events"])


def get_event_service(request: Request) -> EventService:
    """Resolve EventService from app.state or fall back to default singleton."""
    svc: EventService | None = getattr(request.app.state, "event_service", None)
    return svc if svc is not None else event_service


@router.get("", response_model=list[EventRead])
async def list_events_endpoint(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[EventService, Depends(get_event_service)],
    mission_id: Annotated[str | None, Query()] = None,
) -> list[EventRead]:
    """Return chronological event log entries, optionally filtered by `mission_id` (§7)."""
    return await service.list_events(session, mission_id=mission_id)
