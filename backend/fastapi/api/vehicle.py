"""Vehicle REST endpoints (§3, §7: GET/POST /vehicle/{id}/*)."""

from __future__ import annotations

from typing import Annotated

from core.security import verify_vehicle_token
from db.session import get_db_session
from fastapi import APIRouter, Depends, Query, Request
from schemas.entities import (
    ObservationRead,
    VehicleCommandAckResponse,
    VehicleManualCommandRequest,
    VehicleSensorsResponse,
    VehicleSimulateAckResponse,
    VehicleSimulateRequest,
    VehicleStatusResponse,
)
from services.vehicle_service import VehicleService, vehicle_service
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/vehicle", tags=["vehicle"])


def get_vehicle_service(request: Request) -> VehicleService:
    """Resolve VehicleService from app.state or fall back to default singleton."""
    svc: VehicleService | None = getattr(request.app.state, "vehicle_service", None)
    return svc if svc is not None else vehicle_service


@router.get("/{vehicle_id}/status", response_model=VehicleStatusResponse)
async def get_vehicle_status_endpoint(
    vehicle_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[VehicleService, Depends(get_vehicle_service)],
) -> VehicleStatusResponse:
    """Return current authoritative vehicle state from the World Model & SQLite (§7)."""
    return await service.get_vehicle_status(session, vehicle_id)


@router.get("/{vehicle_id}/sensors", response_model=VehicleSensorsResponse)
async def get_vehicle_sensors_endpoint(
    vehicle_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[VehicleService, Depends(get_vehicle_service)],
) -> VehicleSensorsResponse:
    """Return latest raw telemetry and sensor state for a vehicle (§7)."""
    return await service.get_vehicle_sensors(session, vehicle_id)


@router.get("/{vehicle_id}/observations", response_model=list[ObservationRead])
async def get_vehicle_observations_endpoint(
    vehicle_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[VehicleService, Depends(get_vehicle_service)],
    mission_id: Annotated[str | None, Query()] = None,
) -> list[ObservationRead]:
    """Return observations reported by `vehicle_id`, optionally filtered by `mission_id` (§7)."""
    return await service.list_vehicle_observations(
        session,
        vehicle_id,
        mission_id=mission_id,
    )


@router.post("/{vehicle_id}/command", response_model=VehicleCommandAckResponse)
async def post_vehicle_command_endpoint(
    vehicle_id: str,
    payload: VehicleManualCommandRequest,
    _token: Annotated[str, Depends(verify_vehicle_token)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[VehicleService, Depends(get_vehicle_service)],
) -> VehicleCommandAckResponse:
    """Dispatch a validated manual override command protected by shared-secret auth (§7, §9)."""
    return await service.execute_manual_command(session, vehicle_id, payload)


@router.post("/{vehicle_id}/simulate", response_model=VehicleSimulateAckResponse)
async def post_vehicle_simulate_endpoint(
    vehicle_id: str,
    payload: VehicleSimulateRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[VehicleService, Depends(get_vehicle_service)],
) -> VehicleSimulateAckResponse:
    """Toggle `VEHICLES.mode` between simulated and real (enforcing hardware lockout) (§7)."""
    return await service.toggle_simulated_mode(session, vehicle_id, payload.enabled)
