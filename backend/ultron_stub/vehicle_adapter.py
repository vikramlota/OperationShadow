"""Vehicle abstraction interfaces and locked real-hardware adapter guard.

Separates backend business logic (`VehicleService` and the Emergency Stop Fast Path)
from the underlying vehicle communication mechanism (deterministic mock simulator
vs. future physical WebSocket hardware transport).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from ultron_stub.interface import (
    CommandAcknowledgementStub,
    EmergencyStopDispatchResultStub,
    TelemetryPayloadStub,
    VehicleCommandEnvelopeStub,
    VehicleId,
    VehicleMode,
    VehicleType,
)


@runtime_checkable
class ClockPort(Protocol):
    """Testable clock abstraction for deterministic heartbeat and telemetry timing."""

    def now(self) -> datetime:
        """Return the current UTC timezone-aware datetime."""
        ...


class SystemClock(ClockPort):
    """Wall-clock implementation returning UTC datetimes."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock(ClockPort):
    """Deterministic controllable clock for unit and integration tests."""

    def __init__(self, initial_time: datetime | None = None) -> None:
        if initial_time is None:
            self._current = datetime(2026, 10, 28, 0, 0, 0, tzinfo=timezone.utc)
        elif initial_time.tzinfo is None:
            self._current = initial_time.replace(tzinfo=timezone.utc)
        else:
            self._current = initial_time.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._current

    def set(self, new_time: datetime) -> datetime:
        if new_time.tzinfo is None:
            self._current = new_time.replace(tzinfo=timezone.utc)
        else:
            self._current = new_time.astimezone(timezone.utc)
        return self._current

    def advance(self, *, milliseconds: float = 0.0, seconds: float = 0.0) -> datetime:
        self._current = self._current + timedelta(
            milliseconds=milliseconds,
            seconds=seconds,
        )
        return self._current


@dataclass(frozen=True)
class VehicleAdapterState:
    """Immutable snapshot of a vehicle's runtime state exposed by an adapter."""

    vehicle_id: VehicleId
    vehicle_type: VehicleType
    mode: VehicleMode
    status: str
    connection: str
    battery: float
    position_x: float
    position_y: float
    position_alt: float
    last_heartbeat: datetime | None
    active_task_id: str | None = None
    active_task_status: str | None = None
    is_running: bool = False


class RealHardwareLockedError(RuntimeError):
    """Raised when operational control of physical hardware is attempted before specs are confirmed."""


@runtime_checkable
class VehicleAdapterPort(Protocol):
    """Abstraction isolating backend logic from vehicle transport and simulation."""

    @property
    def vehicle_id(self) -> VehicleId:
        ...

    @property
    def vehicle_type(self) -> VehicleType:
        ...

    @property
    def mode(self) -> VehicleMode:
        ...

    @property
    def is_running(self) -> bool:
        ...

    def get_state(self) -> VehicleAdapterState:
        """Return the vehicle's current state snapshot."""
        ...

    def get_latest_telemetry(self) -> TelemetryPayloadStub | None:
        """Return the most recent telemetry frame emitted by the vehicle, if any."""
        ...

    def get_connection_state(self) -> str:
        """Return current connection state ('CONNECTED', 'DISCONNECTED', 'UNKNOWN')."""
        ...

    def get_last_heartbeat(self) -> datetime | None:
        """Return timestamp of the latest valid heartbeat."""
        ...

    async def start(self) -> None:
        """Initialize adapter resources."""
        ...

    async def stop(self) -> None:
        """Stop background loops and clean up adapter resources."""
        ...

    async def connect(self) -> VehicleAdapterState:
        """Establish or simulate vehicle connection."""
        ...

    async def disconnect(self) -> VehicleAdapterState:
        """Close or simulate loss of vehicle connection."""
        ...

    async def send_command(
        self,
        command: VehicleCommandEnvelopeStub,
    ) -> CommandAcknowledgementStub:
        """Send a validated structured command to the vehicle and return an acknowledgement."""
        ...

    async def trigger_emergency_stop(self) -> EmergencyStopDispatchResultStub:
        """Dedicated emergency-stop fast-path operation bypassing normal command queues (§10)."""
        ...


class LockedRealVehicleAdapter(VehicleAdapterPort):
    """Placeholder adapter for physical vehicles that enforces the hardware safety lockout.

    Per project constraints, real hardware must not be controlled until TRD v2.0 §7,
    Hardware & IoT §5.2, and physical failsafe specifications are confirmed.
    """

    def __init__(
        self,
        vehicle_id: VehicleId,
        vehicle_type: VehicleType,
        *,
        clock: ClockPort | None = None,
    ) -> None:
        self._vehicle_id: VehicleId = vehicle_id
        self._vehicle_type: VehicleType = vehicle_type
        self._clock: ClockPort = clock or SystemClock()

    @property
    def vehicle_id(self) -> VehicleId:
        return self._vehicle_id

    @property
    def vehicle_type(self) -> VehicleType:
        return self._vehicle_type

    @property
    def mode(self) -> VehicleMode:
        return "real"

    @property
    def is_running(self) -> bool:
        return False

    def get_state(self) -> VehicleAdapterState:
        return VehicleAdapterState(
            vehicle_id=self._vehicle_id,
            vehicle_type=self._vehicle_type,
            mode="real",
            status="IDLE",
            connection="DISCONNECTED",
            battery=100.0,
            position_x=0.0,
            position_y=0.0,
            position_alt=0.0,
            last_heartbeat=None,
            is_running=False,
        )

    def get_latest_telemetry(self) -> TelemetryPayloadStub | None:
        return None

    def get_connection_state(self) -> str:
        return "DISCONNECTED"

    def get_last_heartbeat(self) -> datetime | None:
        return None

    async def start(self) -> None:
        msg = (
            f"Cannot start real vehicle adapter for {self._vehicle_id}: "
            "physical hardware control is locked until TRD v2.0 and Hardware & IoT "
            "specifications are confirmed."
        )
        raise RealHardwareLockedError(msg)

    async def stop(self) -> None:
        return None

    async def connect(self) -> VehicleAdapterState:
        msg = (
            f"Cannot connect real vehicle {self._vehicle_id}: "
            "physical hardware control is locked."
        )
        raise RealHardwareLockedError(msg)

    async def disconnect(self) -> VehicleAdapterState:
        return self.get_state()

    async def send_command(
        self,
        command: VehicleCommandEnvelopeStub,
    ) -> CommandAcknowledgementStub:
        msg = (
            f"Cannot dispatch command '{command.command_type}' to real vehicle "
            f"{self._vehicle_id}: physical hardware control is locked."
        )
        raise RealHardwareLockedError(msg)

    async def trigger_emergency_stop(self) -> EmergencyStopDispatchResultStub:
        """Explicitly report that real-hardware emergency stop is unavailable while locked out."""
        now = self._clock.now()
        return EmergencyStopDispatchResultStub(
            vehicle_id=self._vehicle_id,
            mode="real",
            is_simulated=False,
            real_hardware_available=False,
            connection_at_dispatch="DISCONNECTED",
            dispatch_attempted=False,
            dispatch_succeeded=False,
            vehicle_acknowledged=False,
            vehicle_confirmed_stopped=False,
            resulting_status="UNKNOWN",
            was_already_stopped=False,
            dispatched_at=now,
            acknowledged_at=None,
            reason=(
                "Real-hardware emergency-stop dispatch is unavailable "
                "(SHADOW_HARDWARE_LOCKOUT=true; TRD v2.0 §7 and Hardware & IoT "
                "failsafe protocols not yet supplied). Rely on the physical "
                "vehicle kill switch and onboard hardware failsafes."
            ),
        )
