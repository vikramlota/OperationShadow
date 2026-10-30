"""Explicitly labeled stub interfaces for unresolved external specifications.

References:
- ULTRON Agent Architecture doc (unresolved)
- TRD v2.0 §7 structured command schema (unresolved)
- Hardware & IoT doc §5.2 telemetry payload (unresolved)
- PRD v2.0 §5.2 World Model snapshot (unresolved)

Do not embed real vehicle protocol logic or planning heuristics here; replace or
implement these interfaces when the companion specification documents are provided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

VehicleId = Literal["SCOUT01", "HAWK01"]
EmergencyStopTarget = Literal["ALL", "SCOUT01", "HAWK01"]
VehicleMode = Literal["simulated", "real"]
VehicleType = Literal["ground", "aerial"]
CommandAckStatus = Literal["ACCEPTED", "REJECTED", "FAILED"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class VehicleCommandEnvelopeStub:
    """Provisional structured command envelope placeholder for TRD v2.0 §7.

    Per Architecture §5.1, the vehicle gateway only forwards structured commands
    and never arbitrary free-form text.
    """

    command_id: str
    vehicle_id: VehicleId
    command_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    issued_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not self.command_id or not self.command_id.strip():
            msg = "command_id must be a non-empty string"
            raise ValueError(msg)
        if self.vehicle_id not in ("SCOUT01", "HAWK01"):
            msg = f"Unsupported vehicle_id '{self.vehicle_id}'"
            raise ValueError(msg)
        if (
            not self.command_type
            or not self.command_type.strip()
            or " " in self.command_type
            or self.command_type != self.command_type.upper()
        ):
            msg = (
                "command_type must be an uppercase structured command token "
                "(arbitrary free-form text is prohibited per Architecture §5.1)"
            )
            raise ValueError(msg)


@dataclass(frozen=True)
class CommandAcknowledgementStub:
    """Provisional command acknowledgement returned by a vehicle adapter."""

    command_id: str
    vehicle_id: VehicleId
    command_type: str
    status: CommandAckStatus
    acknowledged_at: datetime
    message: str | None = None
    is_simulated: bool = True


@dataclass(frozen=True)
class EmergencyStopDispatchResultStub:
    """Detailed per-vehicle emergency-stop outcome (§10).

    Explicitly separates:
    - dispatch_attempted: whether the backend attempted to send the stop signal
    - dispatch_succeeded: whether transport transmission succeeded
    - vehicle_acknowledged: whether the vehicle acknowledged receipt
    - vehicle_confirmed_stopped: whether the vehicle is confirmed halted
    """

    vehicle_id: VehicleId
    mode: VehicleMode
    is_simulated: bool
    real_hardware_available: bool
    connection_at_dispatch: str
    dispatch_attempted: bool
    dispatch_succeeded: bool
    vehicle_acknowledged: bool
    vehicle_confirmed_stopped: bool
    resulting_status: str
    was_already_stopped: bool
    dispatched_at: datetime
    acknowledged_at: datetime | None = None
    reason: str = ""


@dataclass(frozen=True)
class TelemetryPayloadStub:
    """Provisional inbound telemetry envelope placeholder for Hardware & IoT §5.2."""

    vehicle_id: VehicleId
    battery: float
    position_x: float
    position_y: float
    position_alt: float
    status: str
    timestamp: datetime = field(default_factory=_utc_now)
    sequence: int = 0

    def __post_init__(self) -> None:
        if not (0.0 <= self.battery <= 100.0):
            msg = f"battery must be within [0.0, 100.0], got {self.battery}"
            raise ValueError(msg)


@dataclass(frozen=True)
class ObservationPayloadStub:
    """Provisional structured observation emitted by a vehicle adapter (§5.1, §6)."""

    observation_id: str
    mission_id: str
    vehicle_id: VehicleId
    obs_type: str
    confidence: float
    location_x: float
    location_y: float
    timestamp: datetime = field(default_factory=_utc_now)
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            msg = f"confidence must be within [0.0, 1.0], got {self.confidence}"
            raise ValueError(msg)


@dataclass(frozen=True)
class SimulatedTaskSpecStub:
    """Explicitly simulated task decomposition item returned by StubUltronAgent."""

    task_id: str
    vehicle_id: VehicleId
    task_type: str
    area: str | None
    target: str | None
    priority: int
    status: str = "ASSIGNED"


@dataclass(frozen=True)
class SimulatedMissionPlanStub:
    """Explicitly simulated mission initialization output from StubUltronAgent.

    This is a deterministic development placeholder for `POST /mission` (§4)
    and does not implement real ULTRON LLM planning or replanning.
    """

    mission_id: str
    objective: str
    status: str
    tasks: list[SimulatedTaskSpecStub]
    trigger: str
    rationale: str
    is_stub: bool = True


@dataclass(frozen=True)
class WorldModelSnapshotStub:
    """Provisional World Model snapshot placeholder for PRD v2.0 §5.2."""

    active_mission_id: str | None
    vehicles: dict[str, dict[str, Any]]
    observations: list[dict[str, Any]] = field(default_factory=list)
    is_stub: bool = True


@runtime_checkable
class UltronAgentPort(Protocol):
    """In-process ULTRON agent boundary imported by the FastAPI backend (§2, §4).

    Provides minimal World Model ingestion and simulated mission creation hooks
    without inventing real ULTRON planning/replanning internals.
    """

    def is_stub_implementation(self) -> bool:
        """Return True when backed by the development stub."""
        ...

    async def get_world_model_snapshot(self) -> WorldModelSnapshotStub:
        """Return a snapshot of current in-memory World Model state."""
        ...

    async def create_simulated_mission(
        self,
        mission_id: str,
        objective: str,
    ) -> SimulatedMissionPlanStub:
        """Generate a deterministic stub mission decomposition for development/testing."""
        ...

    async def update_vehicle_state(
        self,
        vehicle_id: VehicleId,
        *,
        status: str | None = None,
        connection: str | None = None,
        mode: VehicleMode | None = None,
        battery: float | None = None,
        position_x: float | None = None,
        position_y: float | None = None,
        position_alt: float | None = None,
        last_heartbeat: datetime | None = None,
    ) -> None:
        """Update the in-memory World Model state for a vehicle."""
        ...

    async def mark_vehicle_unknown(
        self,
        vehicle_id: VehicleId,
        *,
        detected_at: datetime,
    ) -> None:
        """Mark a vehicle UNKNOWN in the World Model when heartbeats time out (§5.1)."""
        ...

    async def record_observation(
        self,
        observation: ObservationPayloadStub,
    ) -> None:
        """Forward a structured vehicle observation into the World Model (§4)."""
        ...
