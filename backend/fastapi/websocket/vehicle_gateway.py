"""Vehicle gateway, strict telemetry/observation validation, and Emergency Stop Fast Path dispatcher (§3, §5.1, §10).

Architecture §10 & Second-Pass Safety Invariants:
- `POST /emergency-stop` latches Emergency Stop immediately in memory (`latch_global_emergency_stop`),
  cancels any pending ULTRON replanning, and sends the STOP signal directly through the
  Vehicle Gateway fast path, bypassing ULTRON planning/replanning, normal mission/task
  queues, and synchronous database writes.
- Database logging and dashboard state sync happen asynchronously *after* dispatch
  has already completed, never before.
- Stop-command transport failures are never silently ignored: if transmitting STOP
  over an active vehicle WebSocket or adapter fails, it is logged as a `CRITICAL` fault
  and reported in the `EmergencyStopResponse` outcome (`dispatch_succeeded=False`,
  `vehicle_confirmed_stopped=False`).
- Strict Telemetry & Observation Validation (§3):
  - Never converts missing battery values to `100.0`.
  - Never converts missing positions to `(0.0, 0.0)`.
  - Never converts malformed or missing timestamps into `datetime.now()`.
  - Never converts missing observation confidence into `1.0`.
  - Never creates observations with invented coordinates.
  - Distinguishes unavailable measurements (`None`, missing key, `bool`, `NaN`, `Infinity`)
    from actual zero values (`0` / `0.0`).
  - Rejects malformed or incomplete frames with structured logs without refreshing
    telemetry freshness (`last_heartbeat`) and without dropping the WebSocket session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
import json
import math
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
from services.scout_serial_writer import latch_global_emergency_stop
from services.vehicle_service import VehicleService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ultron_stub import ObservationPayloadStub, TelemetryPayloadStub, VehicleId
from websocket.dashboard_gateway import DashboardGateway

logger = get_logger("websocket.vehicle_gateway")

OrderTraceHook = Callable[[str], Awaitable[None] | None]

_VALID_VEHICLE_STATUSES = {member.value for member in VehicleStatusEnum}


def _require_finite_float(
    mapping: dict[str, Any],
    field_name: str,
    *,
    min_val: float | None = None,
    max_val: float | None = None,
) -> float:
    """Extract a required finite float from `mapping`, distinguishing 0.0 from None/missing/NaN/bool."""
    if field_name not in mapping or mapping[field_name] is None:
        raise ValueError(f"Missing required numeric field '{field_name}'.")
    raw = mapping[field_name]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(
            f"Field '{field_name}' must be a finite number (int or float), got {raw!r}."
        )
    val = float(raw)
    if not math.isfinite(val):
        raise ValueError(f"Field '{field_name}' must be finite (not NaN or Infinity), got {raw!r}.")
    if min_val is not None and val < min_val:
        raise ValueError(f"Field '{field_name}'={val} is below minimum {min_val}.")
    if max_val is not None and val > max_val:
        raise ValueError(f"Field '{field_name}'={val} exceeds maximum {max_val}.")
    return val


def _parse_strict_timestamp(
    mapping: dict[str, Any],
    field_name: str = "timestamp",
) -> datetime:
    """Strictly parse a required timestamp without falling back to current wall-clock time."""
    if field_name not in mapping or mapping[field_name] is None:
        raise ValueError(f"Missing required timestamp field '{field_name}'.")
    raw_ts = mapping[field_name]
    if isinstance(raw_ts, datetime):
        if raw_ts.tzinfo is None:
            return raw_ts.replace(tzinfo=timezone.utc)
        return raw_ts.astimezone(timezone.utc)
    if isinstance(raw_ts, str):
        cleaned = raw_ts.strip()
        if not cleaned:
            raise ValueError(f"Empty timestamp string in '{field_name}'.")
        try:
            parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except Exception as exc:
            raise ValueError(f"Malformed ISO-8601 timestamp '{raw_ts}': {exc}") from exc
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        ts_val = float(raw_ts)
        if not math.isfinite(ts_val) or ts_val <= 0.0:
            raise ValueError(f"Invalid numeric timestamp {raw_ts!r}.")
        return datetime.fromtimestamp(ts_val, tz=timezone.utc)
    raise ValueError(f"Unsupported timestamp type for '{field_name}': {type(raw_ts).__name__}.")


def validate_inbound_telemetry_frame(
    vid: VehicleId,
    tel_data: dict[str, Any],
) -> TelemetryPayloadStub:
    """Strictly validate an inbound telemetry frame before updating state or refreshing heartbeats."""
    if not isinstance(tel_data, dict):
        raise ValueError("Telemetry payload must be a JSON object.")

    raw_status = tel_data.get("status")
    if not isinstance(raw_status, str) or not raw_status.strip():
        raise ValueError("Missing or empty required 'status' field in telemetry.")
    status_str = raw_status.strip().upper()
    if status_str not in _VALID_VEHICLE_STATUSES:
        raise ValueError(f"Invalid vehicle status '{raw_status}' in telemetry.")

    battery = _require_finite_float(tel_data, "battery", min_val=0.0, max_val=100.0)
    pos_x = _require_finite_float(tel_data, "position_x")
    pos_y = _require_finite_float(tel_data, "position_y")

    if "position_alt" in tel_data and tel_data["position_alt"] is not None:
        pos_alt = _require_finite_float(tel_data, "position_alt", min_val=0.0, max_val=0.0)
    else:
        pos_alt = 0.0

    ts_dt = _parse_strict_timestamp(tel_data, "timestamp")

    raw_sensors = tel_data.get("raw_sensors")
    if raw_sensors is not None and not isinstance(raw_sensors, dict):
        raise ValueError("Optional 'raw_sensors' field must be a JSON object if provided.")

    return TelemetryPayloadStub(
        vehicle_id=vid,
        timestamp=ts_dt,
        status=status_str,
        battery=battery,
        position_x=pos_x,
        position_y=pos_y,
        position_alt=pos_alt,
        raw_sensors=raw_sensors,
    )


def validate_inbound_observation_frame(
    vid: VehicleId,
    obs_data: dict[str, Any],
) -> ObservationPayloadStub:
    """Strictly validate an inbound observation frame without inventing coordinates, confidence, or timestamps."""
    if not isinstance(obs_data, dict):
        raise ValueError("Observation payload must be a JSON object.")

    raw_mission_id = obs_data.get("mission_id")
    if not isinstance(raw_mission_id, str) or not raw_mission_id.strip():
        raise ValueError("Missing or empty required 'mission_id' in observation.")

    raw_obs_type = obs_data.get("obs_type")
    if not isinstance(raw_obs_type, str) or not raw_obs_type.strip():
        raise ValueError("Missing or empty required 'obs_type' in observation.")

    confidence = _require_finite_float(obs_data, "confidence", min_val=0.0, max_val=1.0)
    loc_x = _require_finite_float(obs_data, "location_x")
    loc_y = _require_finite_float(obs_data, "location_y")
    ts_dt = _parse_strict_timestamp(obs_data, "timestamp")

    raw_obs_id = obs_data.get("observation_id")
    if raw_obs_id is not None:
        if not isinstance(raw_obs_id, str) or not raw_obs_id.strip():
            raise ValueError("Invalid 'observation_id' in observation.")
        obs_id = raw_obs_id.strip()
    else:
        obs_id = f"OBS-{uuid.uuid4().hex[:8].upper()}"

    raw_meta = obs_data.get("raw")
    if raw_meta is not None and not isinstance(raw_meta, dict):
        raise ValueError("Optional 'raw' metadata field must be a JSON object if provided.")

    return ObservationPayloadStub(
        observation_id=obs_id,
        mission_id=raw_mission_id.strip(),
        vehicle_id=vid,
        obs_type=raw_obs_type.strip(),
        confidence=confidence,
        location_x=loc_x,
        location_y=loc_y,
        timestamp=ts_dt,
        raw=raw_meta,
    )


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

        # 1. Immediately latch global Emergency Stop in memory and cancel any pending ULTRON replanning
        latch_global_emergency_stop()
        if hasattr(vehicle_service.agent, "cancel_pending_replanning"):
            vehicle_service.agent.cancel_pending_replanning("emergency_stop_fast_path")

        target_ids: list[VehicleId] = ["SCOUT01"]
        outcomes: dict[VehicleIdEnum, VehicleEmergencyStopOutcome] = {}

        # 2. Dispatch to SCOUT01 adapter and live WebSocket (if connected), reporting any transport failure as a CRITICAL fault
        for vid in target_ids:
            vid_enum = VehicleIdEnum(vid)
            adapter = vehicle_service.get_adapter(vid)
            pre_state = adapter.get_state()
            try:
                stub_res = await adapter.trigger_emergency_stop()
                ws_stop_failed = False
                ws_fault_reason = ""
                if vehicle_gateway.is_connected(vid):
                    sent_ok = await vehicle_gateway.send_command(
                        vid,
                        {
                            "command_type": "STOP",
                            "command_id": f"ESTOP-{uuid.uuid4().hex[:8].upper()}",
                            "parameters": {"reason": "EMERGENCY_STOP_FAST_PATH"},
                        },
                    )
                    if not sent_ok:
                        ws_stop_failed = True
                        ws_fault_reason = (
                            f"CRITICAL FAULT: Failed to transmit STOP frame over active WebSocket to {vid}."
                        )
                        logger.critical(ws_fault_reason, extra={"vehicle_id": vid})

                await self._emit_trace(f"dispatch:{vid}")
                dispatch_ok = stub_res.dispatch_succeeded and not ws_stop_failed
                confirmed_stopped = stub_res.vehicle_confirmed_stopped and not ws_stop_failed
                reason_str = (
                    f"{stub_res.reason} | {ws_fault_reason}"
                    if ws_stop_failed
                    else stub_res.reason
                )
                outcomes[vid_enum] = VehicleEmergencyStopOutcome(
                    vehicle_id=vid_enum,
                    mode=VehicleModeEnum(stub_res.mode),
                    is_simulated=stub_res.is_simulated,
                    real_hardware_available=stub_res.real_hardware_available,
                    connection_at_dispatch=VehicleConnectionStateEnum(
                        stub_res.connection_at_dispatch
                    ),
                    dispatch_attempted=stub_res.dispatch_attempted,
                    dispatch_succeeded=dispatch_ok,
                    vehicle_acknowledged=stub_res.vehicle_acknowledged and not ws_stop_failed,
                    vehicle_confirmed_stopped=confirmed_stopped,
                    resulting_status=VehicleStatusEnum(stub_res.resulting_status),
                    was_already_stopped=stub_res.was_already_stopped,
                    dispatched_at=stub_res.dispatched_at,
                    acknowledged_at=stub_res.acknowledged_at,
                    reason=reason_str,
                )
            except Exception as exc:
                await self._emit_trace(f"dispatch_error:{vid}")
                logger.critical(
                    f"CRITICAL FAULT: Emergency stop dispatch exception for {vid}: {exc}",
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
                    reason=f"CRITICAL FAULT — Emergency stop transport error on {vid}: {exc}",
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
                for _vid_enum, outcome in outcomes.items():
                    vid: VehicleId = "SCOUT01"
                    row = (
                        await session.execute(select(Vehicle).where(Vehicle.id == vid))
                    ).scalar_one_or_none()
                    if row is not None:
                        # Latch EMERGENCY_STOPPED in SQLite and WorldModel regardless of transport status
                        row.status = VehicleStatusEnum.EMERGENCY_STOPPED.value
                        await session.flush()
                        await session.refresh(row)
                        await vehicle_service.agent.update_vehicle_state(
                            vid,
                            status=VehicleStatusEnum.EMERGENCY_STOPPED.value,
                        )
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
                        f"{k.value}: confirmed_stopped={v.vehicle_confirmed_stopped}, "
                        f"dispatch_succeeded={v.dispatch_succeeded} ({v.reason})"
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
    """Manages persistent authenticated WebSocket connections from SCOUT01 (§5.1)."""

    def __init__(self) -> None:
        self._active_connections: dict[VehicleId, WebSocket] = {}
        self._lock = asyncio.Lock()
        self.rejected_frame_count: int = 0
        self.accepted_frame_count: int = 0

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
            logger.error(f"Failed to send command to {vehicle_id} over WebSocket: {exc}")
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
        """Handle persistent bidirectional WebSocket session for SCOUT01 (§5.1)."""
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
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION, reason="Invalid vehicle token"
            )
            return

        # 2. Resolve vehicle ID (SCOUT01 only)
        candidate_id = (
            raw_vehicle_id
            or websocket.query_params.get("vehicle_id")
            or websocket.headers.get("x-vehicle-id")
        )
        if candidate_id != "SCOUT01":
            logger.warning(
                f"Rejecting vehicle WebSocket connection: unrecognized vehicle ID '{candidate_id}'."
            )
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION, reason="Invalid vehicle ID"
            )
            return

        vid: VehicleId = cast(VehicleId, candidate_id)
        await websocket.accept()
        logger.info(f"Vehicle {vid} connected over WebSocket.")

        async with self._lock:
            old_ws = self._active_connections.get(vid)
            if old_ws is not None and old_ws != websocket:
                with suppress(Exception):
                    await old_ws.close(
                        code=status.WS_1000_NORMAL_CLOSURE,
                        reason="Superseded by new session",
                    )
            self._active_connections[vid] = websocket

        # Mark vehicle connected in DB & adapter without refreshing heartbeat timestamp
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
                except Exception as json_exc:
                    self.rejected_frame_count += 1
                    logger.warning(
                        f"Discarding non-JSON WebSocket frame from {vid}: {json_exc}",
                        extra={"vehicle_id": vid, "error_code": "INVALID_JSON"},
                    )
                    continue

                if not isinstance(data, dict):
                    self.rejected_frame_count += 1
                    logger.warning(
                        f"Discarding non-object WebSocket frame from {vid}.",
                        extra={"vehicle_id": vid, "error_code": "NON_OBJECT_PAYLOAD"},
                    )
                    continue

                if "token" in data and not is_valid_vehicle_token(data["token"]):
                    self.rejected_frame_count += 1
                    logger.warning(
                        f"Discarding frame from {vid} with invalid payload token.",
                        extra={"vehicle_id": vid, "error_code": "INVALID_PAYLOAD_TOKEN"},
                    )
                    continue

                msg_type = data.get("message_type") or data.get("type")
                try:
                    if msg_type == "telemetry" or "telemetry" in data:
                        raw_tel = data.get("telemetry")
                        if "telemetry" in data and not isinstance(raw_tel, dict):
                            raise ValueError("Field 'telemetry' must be a JSON object.")
                        tel_data: dict[str, Any] = (
                            raw_tel if isinstance(raw_tel, dict) else data
                        )
                        stub = validate_inbound_telemetry_frame(vid, tel_data)
                        async with session_factory() as session:
                            ingest_res = await vehicle_service.ingest_telemetry(session, stub)
                            if not ingest_res.accepted:
                                self.rejected_frame_count += 1
                                logger.warning(
                                    f"Rejected stale/out-of-order telemetry frame from {vid}: {ingest_res.reason}",
                                    extra={"vehicle_id": vid, "error_code": ingest_res.reason},
                                )
                                continue
                        self.accepted_frame_count += 1

                    elif msg_type == "observation" or "observation" in data:
                        raw_obs = data.get("observation")
                        if "observation" in data and not isinstance(raw_obs, dict):
                            raise ValueError("Field 'observation' must be a JSON object.")
                        obs_data: dict[str, Any] = (
                            raw_obs if isinstance(raw_obs, dict) else data
                        )
                        obs_stub = validate_inbound_observation_frame(vid, obs_data)
                        async with session_factory() as session:
                            await vehicle_service.record_observation(session, obs_stub)
                        self.accepted_frame_count += 1

                    else:
                        self.rejected_frame_count += 1
                        logger.warning(
                            f"Discarding unrecognized message_type '{msg_type}' from {vid}.",
                            extra={"vehicle_id": vid, "error_code": "UNKNOWN_MESSAGE_TYPE"},
                        )
                except Exception as frame_exc:
                    self.rejected_frame_count += 1
                    logger.warning(
                        f"Discarding invalid vehicle frame from {vid}: {frame_exc}",
                        extra={"vehicle_id": vid, "error_code": "VALIDATION_FAILED"},
                    )
                    continue

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
            for _vid, ws in list(self._active_connections.items()):
                with suppress(Exception):
                    await ws.close(
                        code=status.WS_1001_GOING_AWAY, reason="Server shutdown"
                    )
            self._active_connections.clear()


vehicle_gateway = VehicleGateway()
vehicle_gateway_fast_path = VehicleGatewayFastPath()
