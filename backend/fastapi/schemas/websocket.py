"""Versioned WebSocket message contracts for Operation Shadow (§5.1, §5.2).

Separates three distinct communication channels:
1. Dashboard WebSocket pushes & connection snapshot (`dashboard_gateway.py` -> React UI)
2. Vehicle-to-Backend inbound telemetry & observations (`SCOUT01`/`HAWK01` -> `vehicle_gateway.py`)
3. Backend-to-Vehicle outbound structured commands (`vehicle_gateway.py` -> `SCOUT01`/`HAWK01`)

Note: Vehicle protocols and TRD v2.0 §7 command schemas are currently unresolved
external specifications; vehicle message models are explicitly marked as provisional
stub contracts and reject arbitrary free-form text commands (§5.1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Discriminator, Field, Tag, TypeAdapter, field_validator
from schemas.entities import (
    BatteryPercent,
    ConfidenceScore,
    CoordinateFloat,
    EventRead,
    IdentifierStr,
    MissionDetailRead,
    NonEmptyStr,
    ObservationRead,
    ReasoningTraceRead,
    SchemaBase,
    TaskRead,
    VehicleRead,
    ensure_utc_datetime,
)
from schemas.enums import (
    DashboardEnvelopeTypeEnum,
    MissionStatusEnum,
    VehicleConnectionStateEnum,
    VehicleIdEnum,
    VehicleModeEnum,
    VehicleStatusEnum,
)

WS_PROTOCOL_VERSION: Literal["1.0"] = "1.0"


# ---------------------------------------------------------------------------
# 1. Dashboard WebSocket Contracts (§5.2)
# ---------------------------------------------------------------------------


class TelemetryPushPayload(SchemaBase):
    """Payload for `type = 'telemetry'` dashboard WebSocket pushes (§5.2)."""

    vehicle_id: VehicleIdEnum
    status: VehicleStatusEnum
    connection: VehicleConnectionStateEnum
    mode: VehicleModeEnum
    battery: BatteryPercent
    position_x: CoordinateFloat
    position_y: CoordinateFloat
    position_alt: CoordinateFloat
    last_heartbeat: datetime | None = None

    @field_validator("last_heartbeat", mode="after")
    @classmethod
    def _normalize_heartbeat(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


class MissionStatusPushPayload(SchemaBase):
    """Payload for `type = 'mission_status'` dashboard WebSocket pushes (§5.2)."""

    mission_id: IdentifierStr
    objective: str
    status: MissionStatusEnum
    tasks: list[TaskRead] = Field(default_factory=list)
    completed_at: datetime | None = None

    @field_validator("completed_at", mode="after")
    @classmethod
    def _normalize_completed_at(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


class DashboardSnapshotPayload(SchemaBase):
    """Full current-state snapshot sent immediately upon dashboard WS connect (§5.2).

    Contains current mission state, both vehicles' latest telemetry, and recent
    event/reasoning logs so a newly connected or refreshed client renders truth
    immediately without waiting for the next incremental event.
    """

    protocol_version: Literal["1.0"] = WS_PROTOCOL_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    current_mission: MissionDetailRead | None = None
    vehicles: dict[VehicleIdEnum, VehicleRead]
    recent_events: list[EventRead] = Field(default_factory=list)
    recent_reasoning_traces: list[ReasoningTraceRead] = Field(default_factory=list)

    @field_validator("generated_at", mode="after")
    @classmethod
    def _normalize_generated_at(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class _DashboardEnvelopeBase(SchemaBase):
    """Common metadata wrapper for versioned dashboard WebSocket messages (§5.2)."""

    protocol_version: Literal["1.0"] = WS_PROTOCOL_VERSION
    envelope_id: IdentifierStr | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    vehicle_id: VehicleIdEnum | None = None
    sequence: Annotated[int | None, Field(default=None, ge=0)] = None

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class DashboardSnapshotEnvelope(_DashboardEnvelopeBase):
    """Initial full-state snapshot envelope sent on dashboard client connect (§5.2)."""

    type: Literal["snapshot"] = "snapshot"
    payload: DashboardSnapshotPayload


class DashboardTelemetryEnvelope(_DashboardEnvelopeBase):
    """Dashboard WebSocket message envelope with `type = 'telemetry'`."""

    type: Literal[DashboardEnvelopeTypeEnum.TELEMETRY] = DashboardEnvelopeTypeEnum.TELEMETRY
    payload: TelemetryPushPayload


class DashboardEventEnvelope(_DashboardEnvelopeBase):
    """Dashboard WebSocket message envelope with `type = 'event'`."""

    type: Literal[DashboardEnvelopeTypeEnum.EVENT] = DashboardEnvelopeTypeEnum.EVENT
    payload: EventRead


class DashboardReasoningTraceEnvelope(_DashboardEnvelopeBase):
    """Dashboard WebSocket message envelope with `type = 'reasoning_trace'`."""

    type: Literal[DashboardEnvelopeTypeEnum.REASONING_TRACE] = (
        DashboardEnvelopeTypeEnum.REASONING_TRACE
    )
    payload: ReasoningTraceRead


class DashboardMissionStatusEnvelope(_DashboardEnvelopeBase):
    """Dashboard WebSocket message envelope with `type = 'mission_status'`."""

    type: Literal[DashboardEnvelopeTypeEnum.MISSION_STATUS] = (
        DashboardEnvelopeTypeEnum.MISSION_STATUS
    )
    payload: MissionStatusPushPayload


DashboardWebSocketEnvelope = Annotated[
    Annotated[DashboardTelemetryEnvelope, Tag("telemetry")]
    | Annotated[DashboardEventEnvelope, Tag("event")]
    | Annotated[DashboardReasoningTraceEnvelope, Tag("reasoning_trace")]
    | Annotated[DashboardMissionStatusEnvelope, Tag("mission_status")],
    Discriminator("type"),
]

_dashboard_envelope_type: Any = DashboardWebSocketEnvelope
_dashboard_envelope_adapter: TypeAdapter[Any] = TypeAdapter(_dashboard_envelope_type)


def validate_dashboard_ws_envelope(
    raw_data: dict[str, Any],
) -> (
    DashboardTelemetryEnvelope
    | DashboardEventEnvelope
    | DashboardReasoningTraceEnvelope
    | DashboardMissionStatusEnvelope
):
    """Validate a raw dictionary against the discriminated DashboardWebSocketEnvelope union."""
    result: (
        DashboardTelemetryEnvelope
        | DashboardEventEnvelope
        | DashboardReasoningTraceEnvelope
        | DashboardMissionStatusEnvelope
    ) = _dashboard_envelope_adapter.validate_python(raw_data)
    return result


# ---------------------------------------------------------------------------
# 2. Vehicle-to-Backend Inbound Message Stubs (§5.1 — Hardware/IoT §5.2 unresolved)
# ---------------------------------------------------------------------------


class VehicleInboundTelemetryStub(SchemaBase):
    """Provisional stub for inbound vehicle heartbeat telemetry (§5.1).

    Subject to replacement once Hardware & IoT doc §5.2 is provided.
    """

    protocol_version: Literal["1.0-stub"] = "1.0-stub"
    message_type: Literal["telemetry"] = "telemetry"
    vehicle_id: VehicleIdEnum
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: VehicleStatusEnum = VehicleStatusEnum.IDLE
    battery: BatteryPercent
    position_x: CoordinateFloat
    position_y: CoordinateFloat
    position_alt: CoordinateFloat = 0.0
    raw_sensors: dict[str, Any] | None = None

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class VehicleInboundObservationStub(SchemaBase):
    """Provisional stub for inbound structured vehicle observations (§5.1)."""

    protocol_version: Literal["1.0-stub"] = "1.0-stub"
    message_type: Literal["observation"] = "observation"
    observation_id: IdentifierStr
    mission_id: IdentifierStr
    vehicle_id: VehicleIdEnum
    obs_type: NonEmptyStr
    confidence: ConfidenceScore
    location_x: CoordinateFloat
    location_y: CoordinateFloat
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] | None = None

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized

    def to_observation_read(self) -> ObservationRead:
        """Convert the inbound observation message stub into an ObservationRead contract."""
        return ObservationRead(
            id=self.observation_id,
            mission_id=self.mission_id,
            vehicle_id=self.vehicle_id,
            obs_type=self.obs_type,
            confidence=self.confidence,
            location_x=self.location_x,
            location_y=self.location_y,
            timestamp=self.timestamp,
            raw=self.raw,
        )


# ---------------------------------------------------------------------------
# 3. Backend-to-Vehicle Outbound Command Stub (§5.1 — TRD v2.0 §7 unresolved)
# ---------------------------------------------------------------------------


class VehicleOutboundCommandStub(SchemaBase):
    """Provisional structured command envelope stub for backend -> vehicle dispatch (§5.1).

    Per Architecture §5.1, the vehicle gateway only forwards structured commands
    and never accepts or forwards arbitrary raw text. Actual command types and
    parameter schemas depend on the missing TRD v2.0 §7 specification.
    """

    protocol_version: Literal["1.0-stub"] = "1.0-stub"
    command_id: IdentifierStr
    vehicle_id: VehicleIdEnum
    command_type: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            pattern=r"^[A-Z][A-Z0-9_]*$",
            description="Uppercase structured command identifier (never free-form text)",
        ),
    ]
    parameters: dict[str, Any] = Field(default_factory=dict)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("issued_at", mode="after")
    @classmethod
    def _normalize_issued_at(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized
