"""Emergency stop fast-path REST endpoint (§3, §7, §10: POST /emergency-stop).

Bypasses ULTRON planning/replanning, normal task queues, and synchronous SQLite
writes. Dispatches directly through `VehicleGatewayFastPath` and schedules
database event logging asynchronously after dispatch completes.
"""

from __future__ import annotations

from db.session import AsyncSessionLocal
from fastapi import APIRouter, Request
from schemas.entities import EmergencyStopRequest, EmergencyStopResponse
from services.vehicle_service import VehicleService, vehicle_service
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway
from websocket.vehicle_gateway import VehicleGatewayFastPath, vehicle_gateway_fast_path

router = APIRouter(prefix="/emergency-stop", tags=["emergency"])


@router.post("", response_model=EmergencyStopResponse)
async def post_emergency_stop_endpoint(
    payload: EmergencyStopRequest,
    request: Request,
) -> EmergencyStopResponse:
    """Trigger the Emergency Stop Fast Path for ALL, SCOUT01, or HAWK01 (§7, §10)."""
    fast_path: VehicleGatewayFastPath = (
        getattr(request.app.state, "vehicle_gateway_fast_path", None)
        or vehicle_gateway_fast_path
    )
    vehicle_svc: VehicleService = (
        getattr(request.app.state, "vehicle_service", None) or vehicle_service
    )
    session_factory: async_sessionmaker[AsyncSession] = (
        getattr(request.app.state, "session_factory", None) or AsyncSessionLocal
    )
    dash_gateway: DashboardGateway = (
        getattr(request.app.state, "dashboard_gateway", None) or dashboard_gateway
    )

    return await fast_path.dispatch_emergency_stop(
        payload.vehicle_id,
        vehicle_service=vehicle_svc,
        session_factory=session_factory,
        dashboard_gateway=dash_gateway,
    )
