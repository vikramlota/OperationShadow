"""Operation Shadow FastAPI entry point (§3).

Wires the REST API routers (`/mission`, `/vehicle`, `/events`, `/map`, `/emergency-stop`),
the Dashboard WebSocket Gateway (`/ws/dashboard`), the Emergency Stop Fast Path
(`VehicleGatewayFastPath`), the read-only ROSBridge PerceptionService,
structured logging, standardized error-response handlers, asynchronous SQLite
initialization, and clean shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from api import emergency, events, map as map_router, mission, scout_control, vehicle
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
from services.perception_service import PerceptionService, perception_service
from services.scout_control_simulator import ScoutControlSimulator
from services.scout_serial_writer import ScoutSerialWriter
from services.vehicle_service import VehicleService, vehicle_service
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway
from websocket.vehicle_gateway import (
    VehicleGateway,
    VehicleGatewayFastPath,
    vehicle_gateway,
    vehicle_gateway_fast_path,
)

logger = get_logger("main")

_PLACEHOLDER_VEHICLE_SECRETS = frozenset(
    {"provisional_secret_change_me", "change-me-local-demo-secret"}
)


def log_startup_safety_warnings(active_settings: object | None = None) -> None:
    """Emit structured WARNING logs at startup for non-default safety posture or placeholder secrets on non-loopback hosts."""
    cfg = active_settings if active_settings is not None else settings
    hardware_lockout = getattr(cfg, "hardware_lockout", True)
    default_vehicle_mode = getattr(cfg, "default_vehicle_mode", "simulated")
    scout_serial_enabled = getattr(cfg, "scout_serial_enabled", False)
    scout_serial_failsafe_verified = getattr(cfg, "scout_serial_failsafe_verified", False)
    host = getattr(cfg, "host", "127.0.0.1")
    vehicle_shared_secret = getattr(cfg, "vehicle_shared_secret", "")

    if not hardware_lockout or default_vehicle_mode == "real":
        logger.warning(
            "SAFETY WARNING: Hardware lockout is disabled or default vehicle mode is real "
            f"(hardware_lockout={hardware_lockout}, default_vehicle_mode={default_vehicle_mode!r}, "
            f"scout_serial_enabled={scout_serial_enabled}, "
            f"scout_serial_failsafe_verified={scout_serial_failsafe_verified}). "
            "Ensure physical E-Stop and docs/SUPERVISED_REAL_MODE.md preconditions are satisfied."
        )

    if host != "127.0.0.1" and vehicle_shared_secret in _PLACEHOLDER_VEHICLE_SECRETS:
        logger.warning(
            "SECURITY WARNING: SHADOW_VEHICLE_SHARED_SECRET is still a placeholder value while "
            f"host={host!r} is not 127.0.0.1. Rotate SHADOW_VEHICLE_SHARED_SECRET before exposing "
            "vehicle or webhook endpoints on a non-loopback interface."
        )


class HealthResponse(BaseModel):
    """Health check response schema."""

    status: str
    database: str
    hardware_lockout: bool
    default_vehicle_mode: str
    ultron_is_stub: bool
    vehicles: list[str]
    ros_enabled: bool = False
    ros_connection_state: str = "DISABLED"
    ros_vehicle_id: str | None = None


def create_app(
    *,
    db_engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    mission_svc: MissionService | None = None,
    vehicle_svc: VehicleService | None = None,
    event_svc: EventService | None = None,
    perception_svc: PerceptionService | None = None,
    dash_gateway: DashboardGateway | None = None,
    veh_gateway: VehicleGateway | None = None,
    estop_fast_path: VehicleGatewayFastPath | None = None,
    floor_plan_config: FloorPlanConfigResponse | None = None,
) -> FastAPI:
    """Construct and configure the Operation Shadow FastAPI application."""
    configure_logging()

    active_session_factory = session_factory or AsyncSessionLocal
    active_gateway = dash_gateway or dashboard_gateway
    active_veh_gateway = veh_gateway or vehicle_gateway
    active_estop_fast_path = estop_fast_path or vehicle_gateway_fast_path
    active_event_svc = event_svc or event_service
    active_mission_svc = mission_svc or mission_service
    active_vehicle_svc = vehicle_svc or vehicle_service
    active_perception_svc = perception_svc or perception_service

    scout_serial_writer = ScoutSerialWriter(
        port=settings.scout_serial_port,
        baudrate=settings.scout_serial_baudrate,
        enabled=settings.scout_serial_enabled,
        hardware_lockout=settings.hardware_lockout,
        real_mode=settings.default_vehicle_mode == "real",
        failsafe_verified=settings.scout_serial_failsafe_verified,
    )
    active_scout_control_simulator = ScoutControlSimulator(scout_serial_writer)

    active_event_svc.set_gateway(active_gateway)
    active_mission_svc.set_gateway(active_gateway)
    active_vehicle_svc.set_gateway(active_gateway)
    active_perception_svc.set_gateway(active_gateway)
    active_perception_svc.set_session_factory(active_session_factory)
    active_perception_svc.set_agent(active_mission_svc.agent)
    active_gateway.set_detections_provider(active_perception_svc.get_all_detection_states)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Initialize SQLite metadata and ROSBridge subscriber on startup; clean up on shutdown."""
        configure_logging()
        log_startup_safety_warnings(settings)
        await init_db(db_engine)
        await active_perception_svc.start()
        await active_scout_control_simulator.start()
        logger.info("Operation Shadow backend initialized.")

        rosbridge = None
        if settings.rosbridge_enabled:
            try:
                from services.rosbridge_client import start_rosbridge

                rosbridge = await start_rosbridge(
                    mission_service_agent=active_mission_svc.agent
                )
                logger.info("ROSBridge client started for read-only SCOUT01 observations.")
            except Exception:
                logger.exception("ROSBridge client failed to start.")
        else:
            logger.info("ROSBridge client disabled by SHADOW_ROSBRIDGE_ENABLED=false.")

        try:
            yield
        finally:
            await active_scout_control_simulator.shutdown()
            await active_perception_svc.stop()
            if rosbridge:
                await rosbridge.stop()
            await active_estop_fast_path.shutdown()
            await active_veh_gateway.close_all()
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
    application.state.vehicle_gateway = active_veh_gateway
    application.state.vehicle_gateway_fast_path = active_estop_fast_path
    application.state.event_service = active_event_svc
    application.state.mission_service = active_mission_svc
    application.state.vehicle_service = active_vehicle_svc
    application.state.scout_control_simulator = active_scout_control_simulator
    application.state.perception_service = active_perception_svc
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
    application.include_router(map_router.upload_map_router)
    application.include_router(emergency.router)
    application.include_router(scout_control.router)
    application.include_router(scout_control.websocket_router)

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
            ros_enabled=active_perception_svc.ros_adapter.enabled,
            ros_connection_state=active_perception_svc.ros_adapter.connection_state.value,
            ros_vehicle_id=(
                active_perception_svc.ros_vehicle_id.value
                if active_perception_svc.ros_vehicle_id is not None
                else None
            ),
        )

    @application.get("/scout/status", tags=["scout"])
    async def scout_status() -> dict[str, str | bool]:
        """Return SCOUT01 ROSBridge configuration and connection status."""
        from services.rosbridge_client import build_rosbridge_url, get_rosbridge_client

        client = get_rosbridge_client()
        configured_url = (
            (client.url if client is not None and client.url else build_rosbridge_url(settings))
            or "ws://10.241.171.41:9090"
        )
        return {
            "scout_id": settings.ros_vehicle_id or "SCOUT01",
            "rosbridge_url": configured_url,
            "enabled": settings.rosbridge_enabled,
            "connected": client.connected if client else False,
            "mode": "read_only" if settings.rosbridge_enabled else "disabled",
        }

    @application.websocket("/ws/dashboard")
    async def dashboard_websocket_endpoint(websocket: WebSocket) -> None:
        """Dashboard push WebSocket endpoint (§5.2)."""
        await active_gateway.handle_client_session(websocket, active_session_factory)

    @application.websocket("/ws/vehicle")
    async def vehicle_websocket_endpoint(websocket: WebSocket) -> None:
        """Vehicle telemetry & command WebSocket endpoint (§5.1)."""
        await active_veh_gateway.handle_vehicle_session(
            websocket,
            active_session_factory,
            active_vehicle_svc,
        )

    return application


app = create_app()
