"""Deterministic mock vehicle simulation for SCOUT01 and HAWK01.

Supports:
- Configurable starting positions and battery levels
- Deterministic telemetry generation and step-based movement
- Connection, disconnection, heartbeat suppression, and reconnection/recovery
- Mock task-state transitions and structured observation emission
- Typed command acknowledgements, dedicated emergency-stop fast path, and fault injection
- Optional background heartbeat loop with guaranteed task cleanup on stop()
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ultron_stub.interface import (
    CommandAcknowledgementStub,
    EmergencyStopDispatchResultStub,
    ObservationPayloadStub,
    TelemetryPayloadStub,
    VehicleCommandEnvelopeStub,
    VehicleId,
    VehicleMode,
    VehicleType,
)
from ultron_stub.vehicle_adapter import (
    ClockPort,
    SystemClock,
    VehicleAdapterPort,
    VehicleAdapterState,
)

TelemetryCallback = Callable[[TelemetryPayloadStub], Awaitable[None]]
ObservationCallback = Callable[[ObservationPayloadStub], Awaitable[None]]


@dataclass
class MockVehicleConfig:
    """Configuration for a deterministic mock vehicle instance."""

    vehicle_id: VehicleId
    vehicle_type: VehicleType
    initial_x: float = 0.0
    initial_y: float = 0.0
    initial_alt: float = 0.0
    initial_battery: float = 100.0
    battery_drain_per_tick: float = 0.1
    default_step_distance: float = 0.5

    def __post_init__(self) -> None:
        if not (0.0 <= self.initial_battery <= 100.0):
            msg = "initial_battery must be between 0.0 and 100.0"
            raise ValueError(msg)
        if self.battery_drain_per_tick < 0.0:
            msg = "battery_drain_per_tick must be non-negative"
            raise ValueError(msg)
        if self.vehicle_type == "ground" and self.initial_alt != 0.0:
            msg = "Ground vehicle SCOUT01 initial_alt must be 0.0"
            raise ValueError(msg)


@dataclass
class FaultInjectionConfig:
    """Configurable failure modes for deterministic testing."""

    suppress_heartbeats: bool = False
    reject_commands: bool = False
    disconnect_on_command: bool = False
    force_fault_status: bool = False
    stale_telemetry_lag_ms: int = 0
    fail_emergency_stop_dispatch: bool = False


class MockVehicleAdapter(VehicleAdapterPort):
    """Deterministic simulated vehicle implementing VehicleAdapterPort."""

    def __init__(
        self,
        config: MockVehicleConfig,
        *,
        clock: ClockPort | None = None,
        on_telemetry: TelemetryCallback | None = None,
        on_observation: ObservationCallback | None = None,
    ) -> None:
        self._config = config
        self._clock: ClockPort = clock or SystemClock()
        self._on_telemetry = on_telemetry
        self._on_observation = on_observation

        self._status: str = "IDLE"
        self._connection: str = "DISCONNECTED"
        self._battery: float = config.initial_battery
        self._position_x: float = config.initial_x
        self._position_y: float = config.initial_y
        self._position_alt: float = config.initial_alt
        self._target_x: float | None = None
        self._target_y: float | None = None
        self._target_alt: float | None = None

        self._last_heartbeat: datetime | None = None
        self._latest_telemetry: TelemetryPayloadStub | None = None
        self._sequence: int = 0

        self._active_task_id: str | None = None
        self._active_task_status: str | None = None

        self._faults = FaultInjectionConfig()
        self._command_history: list[VehicleCommandEnvelopeStub] = []
        self._ack_history: list[CommandAcknowledgementStub] = []
        self._observation_history: list[ObservationPayloadStub] = []
        self._emergency_stop_count: int = 0

        self._is_running: bool = False
        self._loop_task: asyncio.Task[None] | None = None

    @property
    def vehicle_id(self) -> VehicleId:
        return self._config.vehicle_id

    @property
    def vehicle_type(self) -> VehicleType:
        return self._config.vehicle_type

    @property
    def mode(self) -> VehicleMode:
        return "simulated"

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def has_background_task(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def command_history(self) -> list[VehicleCommandEnvelopeStub]:
        return list(self._command_history)

    @property
    def ack_history(self) -> list[CommandAcknowledgementStub]:
        return list(self._ack_history)

    @property
    def observation_history(self) -> list[ObservationPayloadStub]:
        return list(self._observation_history)

    @property
    def emergency_stop_count(self) -> int:
        return self._emergency_stop_count

    def get_state(self) -> VehicleAdapterState:
        return VehicleAdapterState(
            vehicle_id=self._config.vehicle_id,
            vehicle_type=self._config.vehicle_type,
            mode="simulated",
            status=self._status,
            connection=self._connection,
            battery=round(self._battery, 4),
            position_x=round(self._position_x, 4),
            position_y=round(self._position_y, 4),
            position_alt=round(self._position_alt, 4),
            last_heartbeat=self._last_heartbeat,
            active_task_id=self._active_task_id,
            active_task_status=self._active_task_status,
            is_running=self._is_running,
        )

    def get_latest_telemetry(self) -> TelemetryPayloadStub | None:
        return self._latest_telemetry

    def get_connection_state(self) -> str:
        return self._connection

    def get_last_heartbeat(self) -> datetime | None:
        return self._last_heartbeat

    def configure_faults(
        self,
        *,
        suppress_heartbeats: bool | None = None,
        reject_commands: bool | None = None,
        disconnect_on_command: bool | None = None,
        force_fault_status: bool | None = None,
        stale_telemetry_lag_ms: int | None = None,
        fail_emergency_stop_dispatch: bool | None = None,
    ) -> FaultInjectionConfig:
        """Update deterministic fault injection settings."""
        if suppress_heartbeats is not None:
            self._faults.suppress_heartbeats = suppress_heartbeats
        if reject_commands is not None:
            self._faults.reject_commands = reject_commands
        if disconnect_on_command is not None:
            self._faults.disconnect_on_command = disconnect_on_command
        if force_fault_status is not None:
            self._faults.force_fault_status = force_fault_status
            if force_fault_status:
                self._status = "FAULT"
            elif self._status == "FAULT":
                self._status = "EXECUTING" if self._active_task_status == "IN_PROGRESS" else "IDLE"
        if stale_telemetry_lag_ms is not None:
            if stale_telemetry_lag_ms < 0:
                msg = "stale_telemetry_lag_ms must be >= 0"
                raise ValueError(msg)
            self._faults.stale_telemetry_lag_ms = stale_telemetry_lag_ms
        if fail_emergency_stop_dispatch is not None:
            self._faults.fail_emergency_stop_dispatch = fail_emergency_stop_dispatch
        return self._faults

    async def start(self) -> None:
        """Mark the mock vehicle adapter started and connect."""
        self._is_running = True
        await self.connect()

    async def start_periodic_telemetry(self, interval_ms: int = 250) -> None:
        """Start an optional background task emitting telemetry every `interval_ms`."""
        if interval_ms <= 0:
            msg = "interval_ms must be positive"
            raise ValueError(msg)
        if not self._is_running:
            await self.start()
        if self._loop_task is not None and not self._loop_task.done():
            return

        async def _runner() -> None:
            try:
                while self._is_running:
                    await self.step()
                    await asyncio.sleep(interval_ms / 1000.0)
            except asyncio.CancelledError:
                raise

        self._loop_task = asyncio.create_task(
            _runner(),
            name=f"mock-vehicle-loop-{self._config.vehicle_id}",
        )

    async def stop(self) -> None:
        """Cancel any background loop and cleanly disconnect the mock vehicle."""
        self._is_running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        await self.disconnect()

    async def connect(self) -> VehicleAdapterState:
        """Transition mock vehicle to CONNECTED and emit an initial heartbeat."""
        self._connection = "CONNECTED"
        if self._status == "UNKNOWN":
            self._status = "EXECUTING" if self._active_task_status == "IN_PROGRESS" else "IDLE"
        if not self._faults.suppress_heartbeats:
            await self.emit_heartbeat()
        return self.get_state()

    async def disconnect(self) -> VehicleAdapterState:
        """Transition mock vehicle to DISCONNECTED."""
        self._connection = "DISCONNECTED"
        return self.get_state()

    async def reconnect(self) -> VehicleAdapterState:
        """Clear heartbeat suppression if set and restore CONNECTED state with a fresh heartbeat."""
        self._faults.suppress_heartbeats = False
        return await self.connect()

    def mark_unknown_by_monitor(self) -> VehicleAdapterState:
        """Mark connection and status as UNKNOWN when heartbeat monitor detects a timeout (§5.1)."""
        self._connection = "UNKNOWN"
        self._status = "UNKNOWN"
        return self.get_state()

    def set_battery(self, battery: float) -> None:
        """Explicitly set simulated battery percentage within [0.0, 100.0]."""
        if not (0.0 <= battery <= 100.0):
            msg = f"Battery must be within [0.0, 100.0], got {battery}"
            raise ValueError(msg)
        self._battery = battery

    def set_target_position(
        self,
        x: float,
        y: float,
        alt: float | None = None,
    ) -> None:
        """Set a target coordinate for deterministic step-by-step simulated movement."""
        if self._config.vehicle_type == "ground" and alt not in (None, 0.0):
            msg = "Ground vehicle SCOUT01 cannot set non-zero altitude"
            raise ValueError(msg)
        self._target_x = x
        self._target_y = y
        self._target_alt = 0.0 if self._config.vehicle_type == "ground" else (
            alt if alt is not None else self._position_alt
        )
        if not self._faults.force_fault_status and self._status != "EMERGENCY_STOPPED":
            self._status = "ACTIVE"

    def move_by(self, dx: float = 0.0, dy: float = 0.0, dalt: float = 0.0) -> VehicleAdapterState:
        """Immediately adjust position by a deterministic delta."""
        if self._config.vehicle_type == "ground" and dalt != 0.0:
            msg = "Ground vehicle SCOUT01 cannot change altitude"
            raise ValueError(msg)
        if self._status == "EMERGENCY_STOPPED":
            return self.get_state()
        self._position_x = round(self._position_x + dx, 4)
        self._position_y = round(self._position_y + dy, 4)
        new_alt = self._position_alt + dalt
        if new_alt < 0.0:
            msg = "Altitude cannot be negative"
            raise ValueError(msg)
        self._position_alt = round(new_alt, 4)
        return self.get_state()

    def transition_task_state(
        self,
        task_id: str,
        new_status: str,
    ) -> VehicleAdapterState:
        """Simulate a vehicle task-state transition."""
        valid_statuses = {
            "PENDING",
            "ASSIGNED",
            "IN_PROGRESS",
            "COMPLETED",
            "CANCELLED",
            "FAILED",
        }
        if new_status not in valid_statuses:
            msg = f"Invalid task status '{new_status}'"
            raise ValueError(msg)

        self._active_task_id = task_id
        self._active_task_status = new_status

        if not self._faults.force_fault_status and self._status != "EMERGENCY_STOPPED":
            if new_status == "IN_PROGRESS":
                self._status = "EXECUTING"
            elif new_status == "ASSIGNED":
                self._status = "ACTIVE"
            elif new_status in ("COMPLETED", "CANCELLED"):
                self._status = "IDLE"
                self._active_task_id = None
            elif new_status == "FAILED":
                self._status = "FAULT"
        return self.get_state()

    def _advance_position_toward_target(self) -> None:
        if self._status == "EMERGENCY_STOPPED" or self._faults.force_fault_status:
            return
        if self._target_x is None or self._target_y is None:
            return

        step = self._config.default_step_distance
        dx = self._target_x - self._position_x
        dy = self._target_y - self._position_y
        dist_xy = float((dx * dx + dy * dy) ** 0.5)

        if dist_xy <= step:
            self._position_x = self._target_x
            self._position_y = self._target_y
        elif dist_xy > 0.0:
            ratio = step / dist_xy
            self._position_x = round(self._position_x + dx * ratio, 4)
            self._position_y = round(self._position_y + dy * ratio, 4)

        if self._target_alt is not None and self._config.vehicle_type == "aerial":
            dalt = self._target_alt - self._position_alt
            if abs(dalt) <= step:
                self._position_alt = self._target_alt
            else:
                self._position_alt = round(
                    self._position_alt + (step if dalt > 0 else -step),
                    4,
                )

        if (
            self._position_x == self._target_x
            and self._position_y == self._target_y
            and (
                self._target_alt is None
                or self._position_alt == self._target_alt
            )
        ):
            self._target_x = None
            self._target_y = None
            self._target_alt = None
            if self._active_task_status != "IN_PROGRESS":
                self._status = "IDLE"

    async def emit_heartbeat(
        self,
        *,
        override_timestamp: datetime | None = None,
    ) -> TelemetryPayloadStub | None:
        """Emit a single telemetry heartbeat if connected and not suppressed."""
        if self._connection == "DISCONNECTED" or self._faults.suppress_heartbeats:
            return None

        now = override_timestamp or self._clock.now()
        if self._faults.stale_telemetry_lag_ms > 0 and override_timestamp is None:
            now = now - timedelta(milliseconds=self._faults.stale_telemetry_lag_ms)

        self._sequence += 1
        if override_timestamp is None and self._faults.stale_telemetry_lag_ms == 0:
            self._last_heartbeat = now
            if self._connection == "UNKNOWN":
                self._connection = "CONNECTED"
                if self._status == "UNKNOWN":
                    self._status = (
                        "EXECUTING"
                        if self._active_task_status == "IN_PROGRESS"
                        else "IDLE"
                    )

        telemetry = TelemetryPayloadStub(
            vehicle_id=self._config.vehicle_id,
            battery=round(self._battery, 4),
            position_x=round(self._position_x, 4),
            position_y=round(self._position_y, 4),
            position_alt=round(self._position_alt, 4),
            status=self._status,
            timestamp=now,
            sequence=self._sequence,
        )
        self._latest_telemetry = telemetry
        if self._on_telemetry is not None:
            await self._on_telemetry(telemetry)
        return telemetry

    async def step(self) -> TelemetryPayloadStub | None:
        """Advance deterministic simulation by one tick (movement + battery drain + heartbeat)."""
        if self._connection == "DISCONNECTED":
            return None

        was_moving_or_active = (
            self._status in ("ACTIVE", "EXECUTING") or self._target_x is not None
        ) and not self._faults.force_fault_status
        self._advance_position_toward_target()
        if was_moving_or_active:
            self._battery = max(0.0, round(self._battery - self._config.battery_drain_per_tick, 4))

        return await self.emit_heartbeat()

    async def emit_observation(
        self,
        *,
        observation_id: str,
        mission_id: str,
        obs_type: str,
        confidence: float,
        location_x: float | None = None,
        location_y: float | None = None,
        raw: dict[str, Any] | None = None,
    ) -> ObservationPayloadStub:
        """Create and emit a structured mock observation at the current or specified position."""
        if self._connection == "DISCONNECTED":
            msg = f"Vehicle {self._config.vehicle_id} cannot emit observations while DISCONNECTED"
            raise RuntimeError(msg)

        observation = ObservationPayloadStub(
            observation_id=observation_id,
            mission_id=mission_id,
            vehicle_id=self._config.vehicle_id,
            obs_type=obs_type,
            confidence=confidence,
            location_x=self._position_x if location_x is None else location_x,
            location_y=self._position_y if location_y is None else location_y,
            timestamp=self._clock.now(),
            raw=raw,
        )
        self._observation_history.append(observation)
        if self._on_observation is not None:
            await self._on_observation(observation)
        return observation

    async def send_command(
        self,
        command: VehicleCommandEnvelopeStub,
    ) -> CommandAcknowledgementStub:
        """Process a structured command and return a deterministic acknowledgement."""
        if command.vehicle_id != self._config.vehicle_id:
            msg = (
                f"Command target '{command.vehicle_id}' does not match adapter "
                f"vehicle '{self._config.vehicle_id}'"
            )
            raise ValueError(msg)

        self._command_history.append(command)
        now = self._clock.now()

        if self._faults.disconnect_on_command:
            await self.disconnect()

        if self._connection == "DISCONNECTED":
            ack = CommandAcknowledgementStub(
                command_id=command.command_id,
                vehicle_id=self._config.vehicle_id,
                command_type=command.command_type,
                status="FAILED",
                acknowledged_at=now,
                message="Vehicle is DISCONNECTED.",
                is_simulated=True,
            )
            self._ack_history.append(ack)
            return ack

        if self._faults.reject_commands:
            ack = CommandAcknowledgementStub(
                command_id=command.command_id,
                vehicle_id=self._config.vehicle_id,
                command_type=command.command_type,
                status="REJECTED",
                acknowledged_at=now,
                message="Command rejected by simulated fault injection.",
                is_simulated=True,
            )
            self._ack_history.append(ack)
            return ack

        if command.command_type in ("EMERGENCY_STOP", "STOP"):
            self._status = "EMERGENCY_STOPPED"
            self._target_x = None
            self._target_y = None
            self._target_alt = None
            self._active_task_status = "CANCELLED"

        ack = CommandAcknowledgementStub(
            command_id=command.command_id,
            vehicle_id=self._config.vehicle_id,
            command_type=command.command_type,
            status="ACCEPTED",
            acknowledged_at=now,
            message=f"Simulated {command.command_type} accepted by {self._config.vehicle_id}.",
            is_simulated=True,
        )
        self._ack_history.append(ack)
        return ack

    async def trigger_emergency_stop(self) -> EmergencyStopDispatchResultStub:
        """Dedicated simulated emergency-stop fast-path operation (§10).

        Bypasses ordinary command queues and `reject_commands` policy, while
        accurately reporting whether the simulated vehicle was CONNECTED,
        DISCONNECTED, or UNKNOWN at the moment of dispatch.
        """
        now = self._clock.now()
        self._emergency_stop_count += 1

        if self._faults.fail_emergency_stop_dispatch:
            msg = f"Simulated emergency-stop transport failure on {self._config.vehicle_id}"
            raise RuntimeError(msg)

        conn_at_dispatch = self._connection

        if conn_at_dispatch == "DISCONNECTED":
            return EmergencyStopDispatchResultStub(
                vehicle_id=self._config.vehicle_id,
                mode="simulated",
                is_simulated=True,
                real_hardware_available=False,
                connection_at_dispatch="DISCONNECTED",
                dispatch_attempted=True,
                dispatch_succeeded=False,
                vehicle_acknowledged=False,
                vehicle_confirmed_stopped=False,
                resulting_status=self._status,
                was_already_stopped=(self._status == "EMERGENCY_STOPPED"),
                dispatched_at=now,
                acknowledged_at=None,
                reason=(
                    f"Simulated vehicle {self._config.vehicle_id} is DISCONNECTED; "
                    "stop signal could not be delivered or confirmed."
                ),
            )

        if conn_at_dispatch == "UNKNOWN":
            # Clear local simulation movement targets, but cannot confirm vehicle stopped while UNKNOWN
            self._target_x = None
            self._target_y = None
            self._target_alt = None
            return EmergencyStopDispatchResultStub(
                vehicle_id=self._config.vehicle_id,
                mode="simulated",
                is_simulated=True,
                real_hardware_available=False,
                connection_at_dispatch="UNKNOWN",
                dispatch_attempted=True,
                dispatch_succeeded=False,
                vehicle_acknowledged=False,
                vehicle_confirmed_stopped=False,
                resulting_status="UNKNOWN",
                was_already_stopped=False,
                dispatched_at=now,
                acknowledged_at=None,
                reason=(
                    f"Simulated vehicle {self._config.vehicle_id} heartbeat state is UNKNOWN; "
                    "stop dispatch attempted, but vehicle acknowledgement and stopped state "
                    "cannot be confirmed."
                ),
            )

        was_already_stopped = self._status == "EMERGENCY_STOPPED"
        self._status = "EMERGENCY_STOPPED"
        self._target_x = None
        self._target_y = None
        self._target_alt = None
        if self._active_task_id is not None:
            self._active_task_status = "CANCELLED"
            self._active_task_id = None

        return EmergencyStopDispatchResultStub(
            vehicle_id=self._config.vehicle_id,
            mode="simulated",
            is_simulated=True,
            real_hardware_available=False,
            connection_at_dispatch="CONNECTED",
            dispatch_attempted=True,
            dispatch_succeeded=True,
            vehicle_acknowledged=True,
            vehicle_confirmed_stopped=True,
            resulting_status="EMERGENCY_STOPPED",
            was_already_stopped=was_already_stopped,
            dispatched_at=now,
            acknowledged_at=now,
            reason=(
                f"Simulated vehicle {self._config.vehicle_id} was already in EMERGENCY_STOPPED state."
                if was_already_stopped
                else f"Simulated emergency stop acknowledged and confirmed by {self._config.vehicle_id}."
            ),
        )


class MockFleetSimulator:
    """Coordinates the deterministic mock vehicle pair (SCOUT01 and HAWK01)."""

    def __init__(
        self,
        *,
        clock: ClockPort | None = None,
        scout_config: MockVehicleConfig | None = None,
        hawk_config: MockVehicleConfig | None = None,
        on_telemetry: TelemetryCallback | None = None,
        on_observation: ObservationCallback | None = None,
    ) -> None:
        self._clock: ClockPort = clock or SystemClock()
        self._scout = MockVehicleAdapter(
            scout_config
            or MockVehicleConfig(
                vehicle_id="SCOUT01",
                vehicle_type="ground",
                initial_x=0.0,
                initial_y=0.0,
                initial_alt=0.0,
            ),
            clock=self._clock,
            on_telemetry=on_telemetry,
            on_observation=on_observation,
        )
        self._hawk = MockVehicleAdapter(
            hawk_config
            or MockVehicleConfig(
                vehicle_id="HAWK01",
                vehicle_type="aerial",
                initial_x=0.0,
                initial_y=0.0,
                initial_alt=1.5,
            ),
            clock=self._clock,
            on_telemetry=on_telemetry,
            on_observation=on_observation,
        )
        self._vehicles: dict[VehicleId, MockVehicleAdapter] = {
            "SCOUT01": self._scout,
            "HAWK01": self._hawk,
        }

    @property
    def clock(self) -> ClockPort:
        return self._clock

    @property
    def scout(self) -> MockVehicleAdapter:
        return self._scout

    @property
    def hawk(self) -> MockVehicleAdapter:
        return self._hawk

    def get_vehicle(self, vehicle_id: VehicleId) -> MockVehicleAdapter:
        if vehicle_id not in self._vehicles:
            msg = f"Unknown vehicle_id '{vehicle_id}'"
            raise KeyError(msg)
        return self._vehicles[vehicle_id]

    async def start_all(self) -> None:
        await self._scout.start()
        await self._hawk.start()

    async def stop_all(self) -> None:
        await self._scout.stop()
        await self._hawk.stop()

    async def step_all(self) -> dict[VehicleId, TelemetryPayloadStub | None]:
        scout_telem = await self._scout.step()
        hawk_telem = await self._hawk.step()
        return {
            "SCOUT01": scout_telem,
            "HAWK01": hawk_telem,
        }
