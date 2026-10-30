"""Vehicle state, heartbeat timeout monitor, and SQLite synchronization service (§3, §5.1, §6, §7).

Responsibilities:
1. Manage vehicle adapters (`SCOUT01` and `HAWK01`) via `VehicleAdapterPort`,
   enforcing `SHADOW_HARDWARE_LOCKOUT` so real hardware cannot be controlled
   until TRD v2.0 and Hardware & IoT specifications are confirmed.
2. Track vehicle heartbeats using a testable `ClockPort` and transition vehicles
   whose heartbeat exceeds `heartbeat_timeout_ms` to `UNKNOWN` in SQLite,
   the in-process ULTRON World Model, and `DashboardGateway` (§5.1).
3. Reject stale or out-of-order telemetry frames so expired packets cannot
   overwrite newer state or falsely recover a vehicle from `UNKNOWN`.
4. Enforce a sensible SQLite persistence boundary while broadcasting live
   telemetry updates over `DashboardGateway`.
5. Serve authoritative vehicle status, sensor telemetry, observations, manual
   command dispatch, and simulated-mode toggles for the `/vehicle/{id}/*` REST API.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast
import uuid

from core.config import Settings, settings
from core.errors import HardwareLockoutError, ResourceNotFoundError
from db.models import Mission, Observation, Vehicle
from schemas.entities import (
    ObservationRead,
    VehicleCommandAckResponse,
    VehicleManualCommandRequest,
    VehicleRead,
    VehicleSensorsResponse,
    VehicleSimulateAckResponse,
    VehicleStatusResponse,
    ensure_utc_datetime,
)
from schemas.enums import (
    VehicleConnectionStateEnum,
    VehicleIdEnum,
    VehicleModeEnum,
    VehicleStatusEnum,
    VehicleTypeEnum,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ultron_stub import (
    ClockPort,
    CommandAcknowledgementStub,
    LockedRealVehicleAdapter,
    MockFleetSimulator,
    MockVehicleAdapter,
    ObservationPayloadStub,
    RealHardwareLockedError,
    StubUltronAgent,
    SystemClock,
    TelemetryPayloadStub,
    UltronAgentPort,
    VehicleAdapterPort,
    VehicleCommandEnvelopeStub,
    VehicleId,
    VehicleMode,
    VehicleType,
)
from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway


def resolve_vehicle_id(raw_vehicle_id: str) -> VehicleId:
    """Validate that `raw_vehicle_id` is SCOUT01 or HAWK01, or raise ResourceNotFoundError."""
    if raw_vehicle_id not in ("SCOUT01", "HAWK01"):
        raise ResourceNotFoundError("Vehicle", raw_vehicle_id)
    return cast(VehicleId, raw_vehicle_id)


@dataclass(frozen=True)
class TelemetryIngestionResult:
    """Outcome of processing an inbound telemetry frame."""

    vehicle_id: VehicleId
    accepted: bool
    persisted_to_db: bool
    connection: str
    status: str
    reason: str = "accepted"


class VehicleService:
    """Coordinates vehicle adapters, heartbeat monitoring, World Model state, SQLite, and WS pushes."""

    def __init__(
        self,
        *,
        agent: UltronAgentPort | None = None,
        clock: ClockPort | None = None,
        simulator: MockFleetSimulator | None = None,
        gateway: DashboardGateway | None = None,
        app_settings: Settings | None = None,
        heartbeat_interval_ms: int | None = None,
        heartbeat_timeout_ms: int | None = None,
        persist_every_n_heartbeats: int = 5,
    ) -> None:
        if persist_every_n_heartbeats < 1:
            msg = "persist_every_n_heartbeats must be >= 1"
            raise ValueError(msg)

        self._settings = app_settings or settings
        self._clock: ClockPort = clock or SystemClock()
        self._agent: UltronAgentPort = agent if agent is not None else StubUltronAgent()
        self._gateway: DashboardGateway = gateway or dashboard_gateway
        self._heartbeat_interval_ms = (
            heartbeat_interval_ms
            if heartbeat_interval_ms is not None
            else self._settings.heartbeat_interval_ms
        )
        self._heartbeat_timeout_ms = (
            heartbeat_timeout_ms
            if heartbeat_timeout_ms is not None
            else self._settings.heartbeat_timeout_ms
        )
        if self._heartbeat_timeout_ms <= self._heartbeat_interval_ms:
            msg = "heartbeat_timeout_ms must be greater than heartbeat_interval_ms"
            raise ValueError(msg)

        self._persist_every_n_heartbeats = persist_every_n_heartbeats
        self._simulator = simulator or MockFleetSimulator(clock=self._clock)

        self._adapters: dict[VehicleId, VehicleAdapterPort] = {
            "SCOUT01": self._simulator.scout,
            "HAWK01": self._simulator.hawk,
        }
        self._unpersisted_heartbeat_counts: dict[VehicleId, int] = {
            "SCOUT01": 0,
            "HAWK01": 0,
        }
        self._latest_heartbeats: dict[VehicleId, datetime | None] = {
            "SCOUT01": None,
            "HAWK01": None,
        }
        self._monitor_task: asyncio.Task[None] | None = None
        self._monitor_running: bool = False

    @property
    def clock(self) -> ClockPort:
        return self._clock

    @property
    def agent(self) -> UltronAgentPort:
        return self._agent

    @property
    def simulator(self) -> MockFleetSimulator:
        return self._simulator

    @property
    def gateway(self) -> DashboardGateway:
        return self._gateway

    def set_gateway(self, gateway: DashboardGateway) -> None:
        self._gateway = gateway

    @property
    def heartbeat_interval_ms(self) -> int:
        return self._heartbeat_interval_ms

    @property
    def heartbeat_timeout_ms(self) -> int:
        return self._heartbeat_timeout_ms

    @property
    def has_monitor_task(self) -> bool:
        return self._monitor_task is not None and not self._monitor_task.done()

    def get_adapter(self, vehicle_id: str) -> VehicleAdapterPort:
        vid = resolve_vehicle_id(vehicle_id)
        return self._adapters[vid]

    def _is_heartbeat_stale(self, last_heartbeat: datetime | None) -> bool:
        hb = ensure_utc_datetime(last_heartbeat)
        if hb is None:
            return False
        return (self._clock.now() - hb) > timedelta(milliseconds=self._heartbeat_timeout_ms)

    async def start(self) -> None:
        """Start all registered simulated vehicle adapters."""
        for adapter in self._adapters.values():
            await adapter.start()

    async def stop(self) -> None:
        """Stop background heartbeat monitor and all registered vehicle adapters cleanly."""
        await self.stop_heartbeat_monitor()
        for adapter in self._adapters.values():
            await adapter.stop()

    async def start_heartbeat_monitor(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        check_interval_ms: int | None = None,
    ) -> None:
        """Start an optional periodic background task checking for heartbeat timeouts."""
        interval = check_interval_ms or self._heartbeat_interval_ms
        if interval <= 0:
            msg = "check_interval_ms must be positive"
            raise ValueError(msg)
        if self._monitor_task is not None and not self._monitor_task.done():
            return

        self._monitor_running = True

        async def _monitor_loop() -> None:
            try:
                while self._monitor_running:
                    async with session_factory() as session:
                        await self.evaluate_heartbeats(session)
                    await asyncio.sleep(interval / 1000.0)
            except asyncio.CancelledError:
                raise

        self._monitor_task = asyncio.create_task(
            _monitor_loop(),
            name="vehicle-heartbeat-monitor",
        )

    async def stop_heartbeat_monitor(self) -> None:
        """Cancel and await the periodic heartbeat monitor task."""
        self._monitor_running = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None

    async def _get_or_create_vehicle_row(
        self,
        session: AsyncSession,
        vehicle_id: VehicleId,
    ) -> Vehicle:
        row = (
            await session.execute(select(Vehicle).where(Vehicle.id == vehicle_id))
        ).scalar_one_or_none()
        if row is not None:
            return row

        adapter = self.get_adapter(vehicle_id)
        state = adapter.get_state()
        row = Vehicle(
            id=vehicle_id,
            vehicle_type=state.vehicle_type,
            status=state.status,
            battery=state.battery,
            position_x=state.position_x,
            position_y=state.position_y,
            position_alt=state.position_alt,
            connection=state.connection,
            mode=state.mode,
            last_heartbeat=state.last_heartbeat,
        )
        session.add(row)
        await session.flush()
        return row

    async def sync_adapter_to_db(
        self,
        session: AsyncSession,
        vehicle_id: str,
    ) -> VehicleRead:
        """Explicitly synchronize the adapter's current state to SQLite, World Model, and WS."""
        vid = resolve_vehicle_id(vehicle_id)
        adapter = self.get_adapter(vid)
        state = adapter.get_state()
        row = await self._get_or_create_vehicle_row(session, vid)

        row.status = state.status
        row.connection = state.connection
        row.mode = state.mode
        row.battery = state.battery
        row.position_x = state.position_x
        row.position_y = state.position_y
        row.position_alt = state.position_alt
        row.last_heartbeat = state.last_heartbeat

        await session.commit()
        await session.refresh(row)
        self._unpersisted_heartbeat_counts[vid] = 0
        self._latest_heartbeats[vid] = ensure_utc_datetime(row.last_heartbeat)

        await self._agent.update_vehicle_state(
            vid,
            status=row.status,
            connection=row.connection,
            mode=state.mode,
            battery=row.battery,
            position_x=row.position_x,
            position_y=row.position_y,
            position_alt=row.position_alt,
            last_heartbeat=row.last_heartbeat,
        )
        vehicle_read = VehicleRead.model_validate(row)
        await self._gateway.broadcast_telemetry(vehicle_read)
        return vehicle_read

    async def get_vehicle_status(
        self,
        session: AsyncSession,
        vehicle_id: str,
    ) -> VehicleStatusResponse:
        """Return authoritative vehicle state for GET /vehicle/{id}/status (§7)."""
        vid = resolve_vehicle_id(vehicle_id)
        row = await self._get_or_create_vehicle_row(session, vid)
        adapter = self.get_adapter(vid)
        adapter_state = adapter.get_state()

        snapshot = await self._agent.get_world_model_snapshot()
        wm_vehicle = snapshot.vehicles.get(vid, {})

        last_hb = (
            self._latest_heartbeats.get(vid)
            or ensure_utc_datetime(wm_vehicle.get("last_heartbeat"))
            or ensure_utc_datetime(row.last_heartbeat)
        )
        battery = float(wm_vehicle.get("battery", row.battery))
        pos_x = float(wm_vehicle.get("position_x", row.position_x))
        pos_y = float(wm_vehicle.get("position_y", row.position_y))
        pos_alt = float(wm_vehicle.get("position_alt", row.position_alt))
        is_stale = self._is_heartbeat_stale(last_hb)

        return VehicleStatusResponse(
            id=VehicleIdEnum(vid),
            vehicle_type=VehicleTypeEnum(row.vehicle_type),
            status=VehicleStatusEnum(row.status),
            battery=battery,
            position_x=pos_x,
            position_y=pos_y,
            position_alt=pos_alt,
            connection=VehicleConnectionStateEnum(row.connection),
            mode=VehicleModeEnum(row.mode),
            last_heartbeat=last_hb,
            is_stale=is_stale,
            active_task_id=adapter_state.active_task_id,
        )

    async def get_vehicle_sensors(
        self,
        session: AsyncSession,
        vehicle_id: str,
    ) -> VehicleSensorsResponse:
        """Return latest raw telemetry and sensor data for GET /vehicle/{id}/sensors (§7)."""
        vid = resolve_vehicle_id(vehicle_id)
        status_resp = await self.get_vehicle_status(session, vid)
        adapter = self.get_adapter(vid)
        latest_telem = adapter.get_latest_telemetry()
        seq = latest_telem.sequence if latest_telem is not None else 0

        return VehicleSensorsResponse(
            vehicle_id=status_resp.id,
            status=status_resp.status,
            connection=status_resp.connection,
            mode=status_resp.mode,
            battery=status_resp.battery,
            position_x=status_resp.position_x,
            position_y=status_resp.position_y,
            position_alt=status_resp.position_alt,
            last_heartbeat=status_resp.last_heartbeat,
            is_stale=status_resp.is_stale,
            sequence=seq,
            raw_sensors={
                "is_simulated": status_resp.mode == VehicleModeEnum.SIMULATED,
                "vehicle_type": status_resp.vehicle_type.value,
                "active_task_id": status_resp.active_task_id,
            },
        )

    async def list_vehicle_observations(
        self,
        session: AsyncSession,
        vehicle_id: str,
        *,
        mission_id: str | None = None,
    ) -> list[ObservationRead]:
        """Return chronological observations reported by `vehicle_id` (§7: GET /vehicle/{id}/observations)."""
        vid = resolve_vehicle_id(vehicle_id)
        if mission_id is not None:
            mission_exists = (
                await session.execute(select(Mission.id).where(Mission.id == mission_id))
            ).scalar_one_or_none()
            if mission_exists is None:
                raise ResourceNotFoundError("Mission", mission_id)

        stmt = select(Observation).where(Observation.vehicle_id == vid)
        if mission_id is not None:
            stmt = stmt.where(Observation.mission_id == mission_id)
        stmt = stmt.order_by(Observation.timestamp.asc(), Observation.id.asc())

        rows = (await session.execute(stmt)).scalars().all()
        return [ObservationRead.model_validate(row) for row in rows]

    async def connect_vehicle(
        self,
        session: AsyncSession,
        vehicle_id: str,
    ) -> VehicleRead:
        """Connect a vehicle adapter and persist the transition to SQLite + World Model."""
        vid = resolve_vehicle_id(vehicle_id)
        adapter = self.get_adapter(vid)
        try:
            await adapter.connect()
        except RealHardwareLockedError as exc:
            raise HardwareLockoutError(str(exc)) from exc
        return await self.sync_adapter_to_db(session, vid)

    async def disconnect_vehicle(
        self,
        session: AsyncSession,
        vehicle_id: str,
    ) -> VehicleRead:
        """Disconnect a vehicle adapter and immediately persist DISCONNECTED state."""
        vid = resolve_vehicle_id(vehicle_id)
        adapter = self.get_adapter(vid)
        await adapter.disconnect()
        return await self.sync_adapter_to_db(session, vid)

    async def reconnect_vehicle(
        self,
        session: AsyncSession,
        vehicle_id: str,
    ) -> VehicleRead:
        """Reconnect a vehicle adapter (clearing simulated heartbeat loss) and sync state."""
        vid = resolve_vehicle_id(vehicle_id)
        adapter = self.get_adapter(vid)
        if isinstance(adapter, MockVehicleAdapter):
            await adapter.reconnect()
        else:
            await adapter.connect()
        return await self.sync_adapter_to_db(session, vid)

    async def set_vehicle_mode(
        self,
        session: AsyncSession,
        vehicle_id: str,
        mode: VehicleMode,
    ) -> VehicleRead:
        """Switch a vehicle between 'simulated' and 'real' modes, respecting hardware lockout."""
        vid = resolve_vehicle_id(vehicle_id)

        try:
            self._settings.validate_requested_mode(mode)
        except ValueError as exc:
            raise HardwareLockoutError(str(exc)) from exc

        vehicle_type: VehicleType = "ground" if vid == "SCOUT01" else "aerial"
        if mode == "real":
            self._adapters[vid] = LockedRealVehicleAdapter(vid, vehicle_type)
        else:
            self._adapters[vid] = self._simulator.get_vehicle(vid)

        row = await self._get_or_create_vehicle_row(session, vid)
        row.mode = mode
        await session.commit()
        await session.refresh(row)

        await self._agent.update_vehicle_state(vid, mode=mode)
        vehicle_read = VehicleRead.model_validate(row)
        await self._gateway.broadcast_telemetry(vehicle_read)
        return vehicle_read

    async def toggle_simulated_mode(
        self,
        session: AsyncSession,
        vehicle_id: str,
        enabled: bool,
    ) -> VehicleSimulateAckResponse:
        """Handle POST /vehicle/{id}/simulate (§7: `{ 'enabled': true }`)."""
        target_mode: VehicleMode = "simulated" if enabled else "real"
        updated = await self.set_vehicle_mode(session, vehicle_id, target_mode)
        return VehicleSimulateAckResponse(
            vehicle_id=updated.id,
            mode=updated.mode,
            enabled=(updated.mode == VehicleModeEnum.SIMULATED),
            status=updated.status,
            connection=updated.connection,
        )

    async def ingest_telemetry(
        self,
        session: AsyncSession,
        telemetry: TelemetryPayloadStub,
        *,
        force_persist: bool = False,
    ) -> TelemetryIngestionResult:
        """Validate, filter stale frames, update World Model, broadcast to WS, and checkpoint DB."""
        vehicle_id = telemetry.vehicle_id
        row = await self._get_or_create_vehicle_row(session, vehicle_id)

        now = self._clock.now()
        frame_ts = ensure_utc_datetime(telemetry.timestamp)
        assert frame_ts is not None
        last_hb = self._latest_heartbeats.get(vehicle_id) or ensure_utc_datetime(
            row.last_heartbeat
        )

        # 1. Reject out-of-order telemetry older than the latest accepted heartbeat
        if last_hb is not None and frame_ts < last_hb:
            return TelemetryIngestionResult(
                vehicle_id=vehicle_id,
                accepted=False,
                persisted_to_db=False,
                connection=row.connection,
                status=row.status,
                reason="stale_older_than_last_heartbeat",
            )

        # 2. Reject stale telemetry whose timestamp is already past the heartbeat timeout window
        age = now - frame_ts
        if age > timedelta(milliseconds=self._heartbeat_timeout_ms):
            return TelemetryIngestionResult(
                vehicle_id=vehicle_id,
                accepted=False,
                persisted_to_db=False,
                connection=row.connection,
                status=row.status,
                reason="stale_exceeded_timeout_window",
            )

        self._latest_heartbeats[vehicle_id] = frame_ts
        connection_changed = row.connection != "CONNECTED"
        status_changed = row.status != telemetry.status

        # Always keep the in-process World Model current on every valid heartbeat
        await self._agent.update_vehicle_state(
            vehicle_id,
            status=telemetry.status,
            connection="CONNECTED",
            battery=telemetry.battery,
            position_x=telemetry.position_x,
            position_y=telemetry.position_y,
            position_alt=telemetry.position_alt,
            last_heartbeat=frame_ts,
        )

        self._unpersisted_heartbeat_counts[vehicle_id] = (
            self._unpersisted_heartbeat_counts.get(vehicle_id, 0) + 1
        )
        should_persist = (
            force_persist
            or connection_changed
            or status_changed
            or self._unpersisted_heartbeat_counts[vehicle_id]
            >= self._persist_every_n_heartbeats
        )

        if should_persist:
            row.connection = "CONNECTED"
            row.status = telemetry.status
            row.battery = telemetry.battery
            row.position_x = telemetry.position_x
            row.position_y = telemetry.position_y
            row.position_alt = telemetry.position_alt
            row.last_heartbeat = frame_ts
            await session.commit()
            self._unpersisted_heartbeat_counts[vehicle_id] = 0

        await self._gateway.broadcast_vehicle_dict(
            {
                "id": vehicle_id,
                "status": telemetry.status,
                "connection": "CONNECTED",
                "mode": row.mode,
                "battery": telemetry.battery,
                "position_x": telemetry.position_x,
                "position_y": telemetry.position_y,
                "position_alt": telemetry.position_alt,
                "last_heartbeat": frame_ts,
            }
        )

        return TelemetryIngestionResult(
            vehicle_id=vehicle_id,
            accepted=True,
            persisted_to_db=should_persist,
            connection="CONNECTED",
            status=telemetry.status,
            reason="accepted",
        )

    async def evaluate_heartbeats(
        self,
        session: AsyncSession,
    ) -> list[VehicleId]:
        """Check connected vehicles for missed heartbeats and mark timed-out vehicles UNKNOWN (§5.1)."""
        now = self._clock.now()
        timeout_delta = timedelta(milliseconds=self._heartbeat_timeout_ms)
        transitioned: list[VehicleId] = []
        transitioned_rows: list[Vehicle] = []

        rows = (
            await session.execute(
                select(Vehicle).where(Vehicle.id.in_(["SCOUT01", "HAWK01"]))
            )
        ).scalars().all()

        for row in rows:
            vehicle_id: VehicleId = "SCOUT01" if row.id == "SCOUT01" else "HAWK01"
            last_hb = self._latest_heartbeats.get(vehicle_id) or ensure_utc_datetime(
                row.last_heartbeat
            )

            if row.connection == "CONNECTED" and last_hb is not None:
                if (now - last_hb) > timeout_delta:
                    row.connection = "UNKNOWN"
                    row.status = "UNKNOWN"
                    row.last_heartbeat = last_hb
                    adapter = self._adapters.get(vehicle_id)
                    if isinstance(adapter, MockVehicleAdapter):
                        adapter.mark_unknown_by_monitor()
                    await self._agent.mark_vehicle_unknown(
                        vehicle_id,
                        detected_at=now,
                    )
                    transitioned.append(vehicle_id)
                    transitioned_rows.append(row)

        if transitioned:
            await session.commit()
            for row in transitioned_rows:
                await session.refresh(row)
                await self._gateway.broadcast_telemetry(VehicleRead.model_validate(row))

        return transitioned

    async def record_observation(
        self,
        session: AsyncSession,
        observation: ObservationPayloadStub,
    ) -> ObservationRead:
        """Persist a structured vehicle observation to SQLite and forward to the World Model."""
        obs_row = Observation(
            id=observation.observation_id,
            mission_id=observation.mission_id,
            vehicle_id=observation.vehicle_id,
            obs_type=observation.obs_type,
            confidence=observation.confidence,
            location_x=observation.location_x,
            location_y=observation.location_y,
            timestamp=ensure_utc_datetime(observation.timestamp)
            or datetime.now(timezone.utc),
            raw=observation.raw,
        )
        session.add(obs_row)
        await session.commit()
        await session.refresh(obs_row)

        await self._agent.record_observation(observation)
        return ObservationRead.model_validate(obs_row)

    async def dispatch_command(
        self,
        session: AsyncSession,
        command: VehicleCommandEnvelopeStub,
    ) -> CommandAcknowledgementStub:
        """Send a structured command through the vehicle adapter and sync state transitions."""
        adapter = self.get_adapter(command.vehicle_id)
        before_state = adapter.get_state()
        try:
            ack = await adapter.send_command(command)
        except RealHardwareLockedError as exc:
            raise HardwareLockoutError(str(exc)) from exc

        after_state = adapter.get_state()
        if (
            before_state.status != after_state.status
            or before_state.connection != after_state.connection
        ):
            await self.sync_adapter_to_db(session, command.vehicle_id)
        return ack

    async def execute_manual_command(
        self,
        session: AsyncSession,
        vehicle_id: str,
        request: VehicleManualCommandRequest,
    ) -> VehicleCommandAckResponse:
        """Validate and dispatch a manual operator override command (§7: POST /vehicle/{id}/command)."""
        vid = resolve_vehicle_id(vehicle_id)
        cmd_id = request.command_id or f"CMD-{uuid.uuid4().hex[:8].upper()}"
        envelope = VehicleCommandEnvelopeStub(
            command_id=cmd_id,
            vehicle_id=vid,
            command_type=request.command_type,
            parameters=dict(request.parameters),
            issued_at=self._clock.now(),
        )
        ack = await self.dispatch_command(session, envelope)
        return VehicleCommandAckResponse(
            command_id=ack.command_id,
            vehicle_id=VehicleIdEnum(ack.vehicle_id),
            command_type=ack.command_type,
            status=ack.status,
            acknowledged_at=ack.acknowledged_at,
            message=ack.message,
            is_simulated=ack.is_simulated,
        )


vehicle_service = VehicleService()
