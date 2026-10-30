"""Operation Shadow FastAPI entry point (§3).

Wires the REST API routers (`/mission`, `/vehicle`, `/events`, `/map`, `/emergency-stop`),
the Dashboard WebSocket Gateway (`/ws/dashboard`), the Emergency Stop Fast Path
(`VehicleGatewayFastPath`), structured logging, standardized error-response
handlers, asynchronous SQLite initialization, and clean shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from api import emergency, events, map as map_router, mission, vehicle
from core.config import settings
from core.errors import register_exception_handlers
from core.logging import configure_logging, get_logger
from db.session import (
    AsyncSessionLocal,
    check_db_connection,
    get_db_session,
    init_db,
)
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from schemas.entities import FloorPlanConfigResponse
from services.event_service import EventService, event_service
from services.mission_service import MissionService, mission_service
from services.vehicle_service import VehicleService, vehicle_service
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway
from websocket.vehicle_gateway import VehicleGatewayFastPath, vehicle_gateway_fast_path

logger = get_logger("main")


class HealthResponse(BaseModel):
    """Health check response schema."""

    status: str
    database: str
    hardware_lockout: bool
    default_vehicle_mode: str
    ultron_is_stub: bool
    vehicles: list[str]


def create_app(
    *,
    db_engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    mission_svc: MissionService | None = None,
    vehicle_svc: VehicleService | None = None,
    event_svc: EventService | None = None,
    dash_gateway: DashboardGateway | None = None,
    estop_fast_path: VehicleGatewayFastPath | None = None,
    floor_plan_config: FloorPlanConfigResponse | None = None,
) -> FastAPI:
    """Construct and configure the Operation Shadow FastAPI application."""
    configure_logging()

    active_session_factory = session_factory or AsyncSessionLocal
    active_gateway = dash_gateway or dashboard_gateway
    active_estop_fast_path = estop_fast_path or vehicle_gateway_fast_path
    active_event_svc = event_svc or event_service
    active_mission_svc = mission_svc or mission_service
    active_vehicle_svc = vehicle_svc or vehicle_service

    active_event_svc.set_gateway(active_gateway)
    active_mission_svc.set_gateway(active_gateway)
    active_vehicle_svc.set_gateway(active_gateway)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Initialize SQLite metadata on startup and clean up WS/vehicle/E-Stop tasks on shutdown."""
        configure_logging()
        await init_db(db_engine)
        logger.info("Operation Shadow backend initialized.")
        try:
            yield
        finally:
            await active_estop_fast_path.shutdown()
            await active_gateway.close_all()
            await active_vehicle_svc.stop()
            logger.info("Operation Shadow backend shut down cleanly.")

    application = FastAPI(
        title="Operation Shadow Mission Control API",
        version="0.1.0",
        lifespan=lifespan,
    )

    application.state.session_factory = active_session_factory
    application.state.dashboard_gateway = active_gateway
    application.state.vehicle_gateway_fast_path = active_estop_fast_path
    application.state.event_service = active_event_svc
    application.state.mission_service = active_mission_svc
    application.state.vehicle_service = active_vehicle_svc
    if floor_plan_config is not None:
        application.state.floor_plan_config = floor_plan_config

    if session_factory is not None:
        async def _override_db_session() -> AsyncIterator[AsyncSession]:
            async with active_session_factory() as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise

        application.dependency_overrides[get_db_session] = _override_db_session

    register_exception_handlers(application)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(mission.router)
    application.include_router(vehicle.router)
    application.include_router(events.router)
    application.include_router(map_router.router)
    application.include_router(emergency.router)

    @application.get("/health", response_model=HealthResponse, tags=["system"])
    async def health_check() -> HealthResponse:
        db_ok = await check_db_connection(db_engine)
        snapshot = await active_mission_svc.agent.get_world_model_snapshot()
        return HealthResponse(
            status="ok" if db_ok else "degraded",
            database="ok" if db_ok else "error",
            hardware_lockout=settings.hardware_lockout,
            default_vehicle_mode=settings.default_vehicle_mode,
            ultron_is_stub=active_mission_svc.agent.is_stub_implementation(),
            vehicles=sorted(snapshot.vehicles.keys()),
        )

    @application.websocket("/ws/dashboard")
    async def dashboard_websocket_endpoint(websocket: WebSocket) -> None:
        """Dashboard push WebSocket endpoint (§5.2)."""
        await active_gateway.handle_client_session(websocket, active_session_factory)

    return application


app = create_app()
