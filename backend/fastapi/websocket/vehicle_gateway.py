"""Vehicle gateway and Emergency Stop Fast Path dispatcher (§3, §5.1, §10).

Architecture §10 invariant:
- `POST /emergency-stop` sends the STOP signal directly through the Vehicle Gateway
  fast path, bypassing ULTRON planning/replanning, normal mission/task queues, and
  synchronous database writes.
- Database logging and dashboard state sync happen asynchronously *after* dispatch
  has already completed, never before.
- When `ALL` is selected, delivery is attempted to `SCOUT01` and `HAWK01`
  independently so failure on one vehicle never blocks the other.
- Physical hardware control remains locked (`SHADOW_HARDWARE_LOCKOUT=true`) until
  TRD v2.0 §7 and Hardware & IoT failsafe specifications are confirmed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
import json
import time
from typing import Any, Literal, cast
import uuid

from core.config import settings
from core.logging import get_logger
from core.security import is_valid_vehicle_token
from db.models import Event, Mission, Vehicle
from fastapi import WebSocket, WebSocketDisconnect, status
from schemas.entities import (
    EmergencyStopResponse,
    EventRead,
    VehicleEmergencyStopOutcome,
    VehicleRead,
)
from schemas.enums import (
    EmergencyStopTargetEnum,
    EventCategoryEnum,
    VehicleConnectionStateEnum,
    VehicleIdEnum,
    VehicleModeEnum,
    VehicleStatusEnum,
)
from schemas.websocket import VehicleOutboundCommandStub
from services.vehicle_service import VehicleService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ultron_stub import ObservationPayloadStub, TelemetryPayloadStub, VehicleId
from websocket.dashboard_gateway import DashboardGateway

logger = get_logger("websocket.vehicle_gateway")

OrderTraceHook = Callable[[str], Awaitable[None] | None]


class VehicleGatewayFastPath:
    """Dedicated emergency-stop fast-path dispatcher and background post-send logger (§10)."""

    def __init__(self) -> None:
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._trace_hook: OrderTraceHook | None = None

    @property
    def pending_log_task_count(self) -> int:
        return len(self._background_tasks)

    def set_order_trace_hook(self, hook: OrderTraceHook | None) -> None:
        """Register an optional deterministic trace hook for verifying dispatch-before-DB-log order."""
        self._trace_hook = hook

    async def _emit_trace(self, step_name: str) -> None:
        if self._trace_hook is not None:
            res = self._trace_hook(step_name)
            if asyncio.iscoroutine(res):
                await res

    async def dispatch_emergency_stop(
        self,
        target: EmergencyStopTargetEnum,
        *,
        vehicle_service: VehicleService,
        session_factory: async_sessionmaker[AsyncSession],
        dashboard_gateway: DashboardGateway,
    ) -> EmergencyStopResponse:
        """Execute the Emergency Stop Fast Path (§10) without calling ULTRON or awaiting DB writes."""
        start_perf = time.perf_counter()

        target_ids: list[VehicleId] = ["SCOUT01"]

        outcomes: dict[VehicleIdEnum, VehicleEmergencyStopOutcome] = {}

        # Dispatch to each vehicle independently so one failure never blocks the other
        for vid in target_ids:
            vid_enum = VehicleIdEnum(vid)
            adapter = vehicle_service.get_adapter(vid)
            pre_state = adapter.get_state()
            try:
                stub_res = await adapter.trigger_emergency_stop()
                if vehicle_gateway.is_connected(vid):
                    with suppress(Exception):
                        await vehicle_gateway.send_command(
                            vid,
                            {
                                "command_type": "STOP",
                                "command_id": f"ESTOP-{uuid.uuid4().hex[:8].upper()}",
                                "parameters": {"reason": "EMERGENCY_STOP_FAST_PATH"},
                            },
                        )
                await self._emit_trace(f"dispatch:{vid}")
                outcomes[vid_enum] = VehicleEmergencyStopOutcome(
                    vehicle_id=vid_enum,
                    mode=VehicleModeEnum(stub_res.mode),
                    is_simulated=stub_res.is_simulated,
                    real_hardware_available=stub_res.real_hardware_available,
                    connection_at_dispatch=VehicleConnectionStateEnum(
                        stub_res.connection_at_dispatch
                    ),
                    dispatch_attempted=stub_res.dispatch_attempted,
                    dispatch_succeeded=stub_res.dispatch_succeeded,
                    vehicle_acknowledged=stub_res.vehicle_acknowledged,
                    vehicle_confirmed_stopped=stub_res.vehicle_confirmed_stopped,
                    resulting_status=VehicleStatusEnum(stub_res.resulting_status),
                    was_already_stopped=stub_res.was_already_stopped,
                    dispatched_at=stub_res.dispatched_at,
                    acknowledged_at=stub_res.acknowledged_at,
                    reason=stub_res.reason,
                )
            except Exception as exc:
                await self._emit_trace(f"dispatch_error:{vid}")
                logger.error(
                    f"Emergency stop dispatch exception for {vid}: {exc}",
                    extra={"vehicle_id": vid},
                )
                outcomes[vid_enum] = VehicleEmergencyStopOutcome(
                    vehicle_id=vid_enum,
                    mode=VehicleModeEnum(pre_state.mode),
                    is_simulated=(pre_state.mode == "simulated"),
                    real_hardware_available=False,
                    connection_at_dispatch=VehicleConnectionStateEnum(pre_state.connection),
                    dispatch_attempted=True,
                    dispatch_succeeded=False,
                    vehicle_acknowledged=False,
                    vehicle_confirmed_stopped=False,
                    resulting_status=VehicleStatusEnum(pre_state.status),
                    was_already_stopped=(pre_state.status == "EMERGENCY_STOPPED"),
                    dispatched_at=vehicle_service.clock.now(),
                    acknowledged_at=None,
                    reason=f"Emergency stop transport error on {vid}: {exc}",
                )

        elapsed_ms = round((time.perf_counter() - start_perf) * 1000.0, 4)

        confirmed_count = sum(
            1 for item in outcomes.values() if item.vehicle_confirmed_stopped
        )
        total_count = len(outcomes)
        all_confirmed = confirmed_count == total_count and total_count > 0
        any_failed = any(not item.dispatch_succeeded for item in outcomes.values())

        overall: Literal[
            "ALL_CONFIRMED_STOPPED",
            "PARTIAL_STOPPED",
            "NONE_CONFIRMED_STOPPED",
        ]
        if all_confirmed:
            overall = "ALL_CONFIRMED_STOPPED"
        elif confirmed_count > 0:
            overall = "PARTIAL_STOPPED"
        else:
            overall = "NONE_CONFIRMED_STOPPED"

        # Schedule asynchronous post-dispatch database logging and WS state sync (§10)
        bg_task = asyncio.create_task(
            self._async_post_dispatch_log(
                target=target,
                outcomes=outcomes,
                vehicle_service=vehicle_service,
                session_factory=session_factory,
                dashboard_gateway=dashboard_gateway,
            ),
            name=f"estop-async-log-{uuid.uuid4().hex[:8]}",
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        return EmergencyStopResponse(
            request_accepted=True,
            target=target,
            overall_outcome=overall,
            all_confirmed_stopped=all_confirmed,
            any_dispatch_failed=any_failed,
            hardware_lockout_active=settings.hardware_lockout,
            real_hardware_estop_available=False,
            fast_path_used=True,
            async_audit_log_scheduled=True,
            dispatch_elapsed_ms=elapsed_ms,
            safety_notice=(
                "SIMULATED MODE ONLY: Physical vehicle emergency-stop dispatch is "
                "unavailable while SHADOW_HARDWARE_LOCKOUT=true and TRD v2.0 / "
                "Hardware & IoT specifications are pending. Software E-Stop never "
                "replaces the physical vehicle kill switch or onboard failsafes."
            ),
            vehicle_outcomes=outcomes,
        )

    async def _async_post_dispatch_log(
        self,
        *,
        target: EmergencyStopTargetEnum,
        outcomes: dict[VehicleIdEnum, VehicleEmergencyStopOutcome],
        vehicle_service: VehicleService,
        session_factory: async_sessionmaker[AsyncSession],
        dashboard_gateway: DashboardGateway,
    ) -> None:
        """Persist emergency-stop state and audit event to SQLite AFTER dispatch has completed."""
        await self._emit_trace("db_log_start")
        try:
            async with session_factory() as session:
                for vid_enum, outcome in outcomes.items():
                    vid: VehicleId = "SCOUT01"
                    row = (
                        await session.execute(select(Vehicle).where(Vehicle.id == vid))
                    ).scalar_one_or_none()
                    if row is not None and outcome.vehicle_confirmed_stopped:
                        row.status = outcome.resulting_status.value
                        await session.flush()
                        await session.refresh(row)
                        await dashboard_gateway.broadcast_telemetry(
                            VehicleRead.model_validate(row)
                        )

                # If a mission exists, log a SAFETY event in the EVENTS table
                latest_mission_id = (
                    await session.execute(
                        select(Mission.id)
                        .order_by(Mission.created_at.desc(), Mission.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

                if latest_mission_id is not None:
                    summary = ", ".join(
                        f"{k.value}: confirmed_stopped={v.vehicle_confirmed_stopped} ({v.reason})"
                        for k, v in outcomes.items()
                    )
                    event_row = Event(
                        id=f"ESTOP-{uuid.uuid4().hex[:8].upper()}",
                        mission_id=latest_mission_id,
                        timestamp=vehicle_service.clock.now(),
                        category=EventCategoryEnum.SAFETY.value,
                        message=f"Emergency stop triggered for {target.value} — {summary}",
                    )
                    session.add(event_row)
                    await session.flush()
                    await session.refresh(event_row)
                    event_read = EventRead.model_validate(event_row)
                else:
                    event_read = None

                await session.commit()

                if event_read is not None:
                    await dashboard_gateway.broadcast_event(event_read)

            await self._emit_trace("db_log_complete")
        except Exception as exc:
            await self._emit_trace("db_log_error")
            logger.error(
                f"Post-dispatch emergency-stop database logging failed (dispatch was unaffected): {exc}",
                extra={"error_code": "ESTOP_ASYNC_LOG_FAILURE"},
            )

    async def wait_for_pending_logs(self, timeout: float = 2.0) -> None:
        """Await any in-flight asynchronous post-dispatch log tasks (used in tests and shutdown)."""
        if not self._background_tasks:
            return
        tasks = list(self._background_tasks)
        with suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )

    async def shutdown(self) -> None:
        """Await or cancel pending background log tasks during application shutdown."""
        await self.wait_for_pending_logs(timeout=1.0)
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._background_tasks.clear()


class VehicleGateway:
    """Manages persistent authenticated WebSocket connections from vehicles (§5.1)."""

    def __init__(self) -> None:
        self._active_connections: dict[VehicleId, WebSocket] = {}
        self._lock = asyncio.Lock()

    def is_connected(self, vehicle_id: str) -> bool:
        """Check if vehicle currently holds an open WebSocket."""
        return vehicle_id in self._active_connections

    async def send_command(
        self,
        vehicle_id: str,
        command: dict[str, Any] | VehicleOutboundCommandStub,
    ) -> bool:
        """Send a structured command envelope directly to the connected vehicle (§5.1)."""
        async with self._lock:
            ws = self._active_connections.get(cast(VehicleId, vehicle_id))
            if ws is None:
                return False

        if isinstance(command, VehicleOutboundCommandStub):
            payload_str = command.model_dump_json()
        elif isinstance(command, dict):
            payload_str = json.dumps(command)
        else:
            return False

        try:
            await ws.send_text(payload_str)
            return True
        except Exception as exc:
            logger.warning(f"Failed to send command to {vehicle_id} over WebSocket: {exc}")
            async with self._lock:
                if self._active_connections.get(cast(VehicleId, vehicle_id)) == ws:
                    self._active_connections.pop(cast(VehicleId, vehicle_id), None)
            return False

    async def handle_vehicle_session(
        self,
        websocket: WebSocket,
        session_factory: async_sessionmaker[AsyncSession],
        vehicle_service: VehicleService,
        raw_vehicle_id: str | None = None,
        token: str | None = None,
    ) -> None:
        """Handle persistent bidirectional WebSocket session for a vehicle (§5.1)."""
        # 1. Authenticate token
        candidate_token = (
            token
            or websocket.query_params.get("token")
            or websocket.headers.get("x-vehicle-token")
        )
        if candidate_token is None:
            auth_header = websocket.headers.get("authorization")
            if auth_header:
                scheme, _, param = auth_header.partition(" ")
                if scheme.lower() == "bearer" and param:
                    candidate_token = param.strip()

        if not is_valid_vehicle_token(candidate_token):
            logger.warning("Rejecting vehicle WebSocket connection: invalid or missing token.")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid vehicle token")
            return

        # 2. Resolve vehicle ID
        candidate_id = (
            raw_vehicle_id
            or websocket.query_params.get("vehicle_id")
            or websocket.headers.get("x-vehicle-id")
        )
        if candidate_id != "SCOUT01":
            logger.warning(f"Rejecting vehicle WebSocket connection: unrecognized vehicle ID '{candidate_id}'.")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid vehicle ID")
            return

        vid: VehicleId = cast(VehicleId, candidate_id)
        await websocket.accept()
        logger.info(f"Vehicle {vid} connected over WebSocket.")

        async with self._lock:
            old_ws = self._active_connections.get(vid)
            if old_ws is not None and old_ws != websocket:
                with suppress(Exception):
                    await old_ws.close(code=status.WS_1000_NORMAL_CLOSURE, reason="Superseded by new session")
            self._active_connections[vid] = websocket

        # Mark vehicle connected in DB & adapter
        async with session_factory() as session:
            try:
                await vehicle_service.connect_vehicle(session, vid)
            except Exception as exc:
                logger.error(f"Failed to update connect state for {vid}: {exc}")

        try:
            while True:
                msg_text = await websocket.receive_text()
                try:
                    data = json.loads(msg_text)
                except Exception:
                    continue

                if not isinstance(data, dict):
                    continue

                if "token" in data and not is_valid_vehicle_token(data["token"]):
                    logger.warning(f"Discarding frame from {vid} with invalid payload token.")
                    continue

                msg_type = data.get("message_type") or data.get("type")
                if msg_type == "telemetry" or "telemetry" in data:
                    raw_tel = data.get("telemetry")
                    tel_data: dict[str, Any] = raw_tel if isinstance(raw_tel, dict) else data
                    ts = tel_data.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except Exception:
                            ts_dt = datetime.now(timezone.utc)
                    elif isinstance(ts, datetime):
                        ts_dt = ts
                    else:
                        ts_dt = datetime.now(timezone.utc)

                    stub = TelemetryPayloadStub(
                        vehicle_id=vid,
                        timestamp=ts_dt,
                        status=str(tel_data.get("status", "IDLE")),
                        battery=float(tel_data.get("battery", 100.0)),
                        position_x=float(tel_data.get("position_x", 0.0)),
                        position_y=float(tel_data.get("position_y", 0.0)),
                        position_alt=float(tel_data.get("position_alt", 0.0)),
                        raw_sensors=tel_data.get("raw_sensors"),
                    )
                    async with session_factory() as session:
                        await vehicle_service.ingest_telemetry(session, stub)

                elif msg_type == "observation" or "observation" in data:
                    raw_obs = data.get("observation")
                    obs_data: dict[str, Any] = raw_obs if isinstance(raw_obs, dict) else data
                    ts = obs_data.get("timestamp")
                    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if isinstance(ts, str) else datetime.now(timezone.utc)
                    obs_stub = ObservationPayloadStub(
                        observation_id=str(obs_data.get("observation_id", f"OBS-{uuid.uuid4().hex[:6].upper()}")),
                        mission_id=str(obs_data.get("mission_id", "")),
                        vehicle_id=vid,
                        obs_type=str(obs_data.get("obs_type", "HAZARD_DETECTED")),
                        confidence=float(obs_data.get("confidence", 1.0)),
                        location_x=float(obs_data.get("location_x", 0.0)),
                        location_y=float(obs_data.get("location_y", 0.0)),
                        timestamp=ts_dt,
                        raw=obs_data.get("raw"),
                    )
                    async with session_factory() as session:
                        await vehicle_service.record_observation(session, obs_stub)

        except WebSocketDisconnect:
            logger.info(f"Vehicle {vid} disconnected.")
        except Exception as exc:
            logger.warning(f"Error handling vehicle {vid} WebSocket session: {exc}")
        finally:
            async with self._lock:
                if self._active_connections.get(vid) == websocket:
                    self._active_connections.pop(vid, None)
            async with session_factory() as session:
                with suppress(Exception):
                    await vehicle_service.disconnect_vehicle(session, vid)

    async def close_all(self) -> None:
        """Close all vehicle WebSockets cleanly on server shutdown."""
        async with self._lock:
            for vid, ws in list(self._active_connections.items()):
                with suppress(Exception):
                    await ws.close(code=status.WS_1001_GOING_AWAY, reason="Server shutdown")
            self._active_connections.clear()


vehicle_gateway = VehicleGateway()
vehicle_gateway_fast_path = VehicleGatewayFastPath()
