"""Mission REST endpoints (§3, §7: POST /mission, GET /mission/{id}, GET /mission/{id}/replay)."""

from __future__ import annotations

from typing import Annotated

from db.session import get_db_session
from fastapi import APIRouter, Depends, Request
from schemas.entities import (
    MissionCreateRequest,
    MissionCreateResponse,
    MissionDetailRead,
    MissionReplayResponse,
)
from services.mission_service import MissionService, mission_service
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/mission", tags=["mission"])


def get_mission_service(request: Request) -> MissionService:
    """Resolve MissionService from app.state or fall back to default singleton."""
    svc: MissionService | None = getattr(request.app.state, "mission_service", None)
    return svc if svc is not None else mission_service


@router.post("", response_model=MissionCreateResponse)
async def create_mission_endpoint(
    payload: MissionCreateRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> MissionCreateResponse:
    """Create a new mission via the in-process ULTRON stub and persist tasks (§7)."""
    return await service.create_mission(session, payload)


@router.get("/{mission_id}", response_model=MissionDetailRead)
async def get_mission_endpoint(
    mission_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> MissionDetailRead:
    """Return full mission state including objective, tasks, and status (§7)."""
    return await service.get_mission(session, mission_id)


@router.get("/{mission_id}/replay", response_model=MissionReplayResponse)
async def get_mission_replay_endpoint(
    mission_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> MissionReplayResponse:
    """Return chronological timeline of events and reasoning-trace entries (§7, §8.5)."""
    return await service.get_mission_replay(session, mission_id)


@router.post("/{mission_id}/complete", response_model=MissionDetailRead)
async def complete_mission_endpoint(
    mission_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> MissionDetailRead:
    """Complete an active simulated mission and finalize its tasks."""
    return await service.complete_mission(session, mission_id)

