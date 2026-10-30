"""Pydantic v2 request, response, and persistence schemas for Operation Shadow entities.

Maintains strict separation between SQLAlchemy ORM models (`db.models`) and
validated API/service contracts (`schemas.entities`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from schemas.enums import (
    EmergencyStopTargetEnum,
    EventCategoryEnum,
    MissionStatusEnum,
    TaskStatusEnum,
    VehicleConnectionStateEnum,
    VehicleIdEnum,
    VehicleModeEnum,
    VehicleStatusEnum,
    VehicleTypeEnum,
)

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=256)]
IdentifierStr = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]
UppercaseCommandStr = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description="Uppercase structured command identifier (never free-form text per §5.1)",
    ),
]
BatteryPercent = Annotated[
    float,
    Field(ge=0.0, le=100.0, allow_inf_nan=False, description="Battery percentage [0.0, 100.0]"),
]
ConfidenceScore = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False, description="Observation confidence [0.0, 1.0]"),
]
CoordinateFloat = Annotated[
    float,
    Field(allow_inf_nan=False, description="Finite indoor spatial coordinate"),
]
PriorityInt = Annotated[
    int,
    Field(ge=1, le=100, description="Task priority integer [1, 100]"),
]


def ensure_utc_datetime(value: datetime | None) -> datetime | None:
    """Normalize naive datetimes to UTC and preserve timezone-aware datetimes in UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SchemaBase(BaseModel):
    """Shared base configuration for Operation Shadow Pydantic schemas."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class OrmReadBase(SchemaBase):
    """Base class for read schemas serialized from SQLAlchemy ORM models."""

    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


# ---------------------------------------------------------------------------
# Mission & Task Schemas (§6, §7)
# ---------------------------------------------------------------------------


class MissionCreateRequest(SchemaBase):
    """POST /mission request payload (§7: `{ "objective": "string" }`)."""

    objective: Annotated[str, Field(min_length=1, max_length=2000)]


class MissionCreateResponse(SchemaBase):
    """POST /mission response payload (§7: `{ "mission_id": "M001" }`)."""

    mission_id: IdentifierStr


class MissionCreateRecord(SchemaBase):
    """Internal persistence contract for creating a Mission row."""

    id: IdentifierStr
    objective: Annotated[str, Field(min_length=1, max_length=2000)]
    status: MissionStatusEnum = MissionStatusEnum.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    @field_validator("created_at", "completed_at", mode="after")
    @classmethod
    def _normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)

    @model_validator(mode="after")
    def _validate_completion_order(self) -> MissionCreateRecord:
        if self.completed_at is not None and self.completed_at < self.created_at:
            msg = "completed_at cannot be earlier than created_at"
            raise ValueError(msg)
        return self


class TaskCreate(SchemaBase):
    """Persistence and validation contract for creating a Task row (§6)."""

    id: IdentifierStr
    mission_id: IdentifierStr
    vehicle_id: VehicleIdEnum | None = None
    type: NonEmptyStr
    area: str | None = Field(default=None, max_length=128)
    target: str | None = Field(default=None, max_length=128)
    priority: PriorityInt = 1
    status: TaskStatusEnum = TaskStatusEnum.PENDING


class TaskRead(OrmReadBase):
    """API-facing representation of a Task record (§6, §7)."""

    id: IdentifierStr
    mission_id: IdentifierStr
    vehicle_id: VehicleIdEnum | None = None
    type: NonEmptyStr
    area: str | None = None
    target: str | None = None
    priority: PriorityInt
    status: TaskStatusEnum


class MissionRead(OrmReadBase):
    """Summary API representation of a Mission record (§6)."""

    id: IdentifierStr
    objective: str
    status: MissionStatusEnum
    created_at: datetime
    completed_at: datetime | None = None

    @field_validator("created_at", "completed_at", mode="after")
    @classmethod
    def _normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


class MissionDetailRead(MissionRead):
    """Full mission state returned by GET /mission/{id} (§7: objective, tasks, status)."""

    tasks: list[TaskRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Vehicle & Emergency Stop Schemas (§6, §7, §10)
# ---------------------------------------------------------------------------


class VehicleCreate(SchemaBase):
    """Persistence contract for registering or seeding a Vehicle record (§6)."""

    id: VehicleIdEnum
    vehicle_type: VehicleTypeEnum
    status: VehicleStatusEnum = VehicleStatusEnum.IDLE
    battery: BatteryPercent = 100.0
    position_x: CoordinateFloat = 0.0
    position_y: CoordinateFloat = 0.0
    position_alt: CoordinateFloat = 0.0
    connection: VehicleConnectionStateEnum = VehicleConnectionStateEnum.DISCONNECTED
    mode: VehicleModeEnum = VehicleModeEnum.SIMULATED
    last_heartbeat: datetime | None = None

    @field_validator("last_heartbeat", mode="after")
    @classmethod
    def _normalize_heartbeat(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


class VehicleUpdate(SchemaBase):
    """Partial update contract for vehicle telemetry, connection, or mode updates."""

    status: VehicleStatusEnum | None = None
    battery: BatteryPercent | None = None
    position_x: CoordinateFloat | None = None
    position_y: CoordinateFloat | None = None
    position_alt: CoordinateFloat | None = None
    connection: VehicleConnectionStateEnum | None = None
    mode: VehicleModeEnum | None = None
    last_heartbeat: datetime | None = None

    @field_validator("last_heartbeat", mode="after")
    @classmethod
    def _normalize_heartbeat(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


class VehicleRead(OrmReadBase):
    """API-facing representation of a Vehicle record (§6, §7)."""

    id: VehicleIdEnum
    vehicle_type: VehicleTypeEnum
    status: VehicleStatusEnum
    battery: BatteryPercent
    position_x: CoordinateFloat
    position_y: CoordinateFloat
    position_alt: CoordinateFloat
    connection: VehicleConnectionStateEnum
    mode: VehicleModeEnum
    last_heartbeat: datetime | None = None

    @field_validator("last_heartbeat", mode="after")
    @classmethod
    def _normalize_heartbeat(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


class VehicleStatusResponse(VehicleRead):
    """GET /vehicle/{id}/status response payload (§7).

    Distinguishes CONNECTED, DISCONNECTED, UNKNOWN, and stale telemetry states.
    """

    is_stale: bool = False
    active_task_id: str | None = None


class VehicleSensorsResponse(SchemaBase):
    """GET /vehicle/{id}/sensors response payload (§7: latest raw telemetry)."""

    vehicle_id: VehicleIdEnum
    status: VehicleStatusEnum
    connection: VehicleConnectionStateEnum
    mode: VehicleModeEnum
    battery: BatteryPercent
    position_x: CoordinateFloat
    position_y: CoordinateFloat
    position_alt: CoordinateFloat
    last_heartbeat: datetime | None = None
    is_stale: bool = False
    sequence: int = Field(default=0, ge=0)
    raw_sensors: dict[str, Any] = Field(default_factory=dict)

    @field_validator("last_heartbeat", mode="after")
    @classmethod
    def _normalize_heartbeat(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


class VehicleManualCommandRequest(SchemaBase):
    """POST /vehicle/{id}/command request payload (§7: structured command override).

    Enforces structured uppercase command identifiers and rejects arbitrary free-form text.
    """

    command_id: IdentifierStr | None = None
    command_type: UppercaseCommandStr
    parameters: dict[str, Any] = Field(default_factory=dict)


class VehicleCommandAckResponse(SchemaBase):
    """POST /vehicle/{id}/command response payload (§7)."""

    command_id: IdentifierStr
    vehicle_id: VehicleIdEnum
    command_type: UppercaseCommandStr
    status: Literal["ACCEPTED", "REJECTED", "FAILED"]
    acknowledged_at: datetime
    message: str | None = None
    is_simulated: bool = True

    @field_validator("acknowledged_at", mode="after")
    @classmethod
    def _normalize_ack_time(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class VehicleSimulateRequest(SchemaBase):
    """POST /vehicle/{id}/simulate request payload (§7: `{ "enabled": true }`)."""

    enabled: bool


class VehicleSimulateAckResponse(SchemaBase):
    """POST /vehicle/{id}/simulate acknowledgement response (§7)."""

    vehicle_id: VehicleIdEnum
    mode: VehicleModeEnum
    enabled: bool
    status: VehicleStatusEnum
    connection: VehicleConnectionStateEnum


class EmergencyStopRequest(SchemaBase):
    """POST /emergency-stop request payload (§7: `{ "vehicle_id": "ALL" | "SCOUT01" | "HAWK01" }`)."""

    vehicle_id: EmergencyStopTargetEnum


class VehicleEmergencyStopOutcome(SchemaBase):
    """Per-vehicle emergency-stop evidence returned by POST /emergency-stop (§10).

    Explicitly separates request acceptance, dispatch attempt, vehicle acknowledgement,
    and confirmed stopping so a 200 OK HTTP response is never confused with a
    confirmed physical or simulated stop.
    """

    vehicle_id: VehicleIdEnum
    mode: VehicleModeEnum
    is_simulated: bool
    real_hardware_available: bool
    connection_at_dispatch: VehicleConnectionStateEnum
    dispatch_attempted: bool
    dispatch_succeeded: bool
    vehicle_acknowledged: bool
    vehicle_confirmed_stopped: bool
    resulting_status: VehicleStatusEnum
    was_already_stopped: bool
    dispatched_at: datetime
    acknowledged_at: datetime | None = None
    reason: str

    @field_validator("dispatched_at", "acknowledged_at", mode="after")
    @classmethod
    def _normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


class EmergencyStopResponse(SchemaBase):
    """POST /emergency-stop response payload (§7, §10)."""

    request_accepted: bool = True
    target: EmergencyStopTargetEnum
    overall_outcome: Literal[
        "ALL_CONFIRMED_STOPPED",
        "PARTIAL_STOPPED",
        "NONE_CONFIRMED_STOPPED",
    ]
    all_confirmed_stopped: bool
    any_dispatch_failed: bool
    hardware_lockout_active: bool
    real_hardware_estop_available: bool = False
    fast_path_used: bool = True
    async_audit_log_scheduled: bool = True
    dispatch_elapsed_ms: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    safety_notice: str
    vehicle_outcomes: dict[VehicleIdEnum, VehicleEmergencyStopOutcome]


# ---------------------------------------------------------------------------
# Observation, Event, and ReasoningTrace Schemas (§6, §7)
# ---------------------------------------------------------------------------


class ObservationCreate(SchemaBase):
    """Persistence and validation contract for an Observation record (§6)."""

    id: IdentifierStr
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


class ObservationRead(OrmReadBase):
    """API-facing representation of an Observation record (§6, §7)."""

    id: IdentifierStr
    mission_id: IdentifierStr
    vehicle_id: VehicleIdEnum
    obs_type: NonEmptyStr
    confidence: ConfidenceScore
    location_x: CoordinateFloat
    location_y: CoordinateFloat
    timestamp: datetime
    raw: dict[str, Any] | None = None

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class EventCreate(SchemaBase):
    """Persistence and validation contract for an Event log record (§6)."""

    id: IdentifierStr
    mission_id: IdentifierStr
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    category: EventCategoryEnum | NonEmptyStr
    message: Annotated[str, Field(min_length=1, max_length=4000)]

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class EventRead(OrmReadBase):
    """API-facing representation of an Event log record (§6, §7)."""

    id: IdentifierStr
    mission_id: IdentifierStr
    timestamp: datetime
    category: str
    message: str

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class ReasoningTraceCreate(SchemaBase):
    """Persistence and validation contract for a ReasoningTrace record (§6)."""

    id: IdentifierStr
    mission_id: IdentifierStr
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trigger: Annotated[str, Field(min_length=1, max_length=2000)]
    rationale: Annotated[str, Field(min_length=1, max_length=8000)]
    resulting_task_ids: list[IdentifierStr] = Field(default_factory=list)

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class ReasoningTraceRead(OrmReadBase):
    """API-facing representation of a ReasoningTrace record (§6, §7)."""

    id: IdentifierStr
    mission_id: IdentifierStr
    timestamp: datetime
    trigger: str
    rationale: str
    resulting_task_ids: list[IdentifierStr] = Field(default_factory=list)

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


# ---------------------------------------------------------------------------
# Mission Replay Timeline Schemas (§7, §8.5)
# ---------------------------------------------------------------------------


class MissionReplayTimelineEntry(SchemaBase):
    """Single chronological item in GET /mission/{id}/replay (§7, §8.5).

    Note: PRD v2.0 §5.2 ('World Model snapshot: plan before / plan after') is
    not yet supplied; `world_model_snapshot` is an optional structured dict stub.
    """

    entry_type: Literal["event", "reasoning_trace"]
    timestamp: datetime
    event: EventRead | None = None
    reasoning_trace: ReasoningTraceRead | None = None
    world_model_snapshot: dict[str, Any] | None = None

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized

    @model_validator(mode="after")
    def _validate_entry_payload(self) -> MissionReplayTimelineEntry:
        if self.entry_type == "event" and self.event is None:
            msg = "Replay entry of type 'event' must include an 'event' payload"
            raise ValueError(msg)
        if self.entry_type == "reasoning_trace" and self.reasoning_trace is None:
            msg = "Replay entry of type 'reasoning_trace' must include a 'reasoning_trace' payload"
            raise ValueError(msg)
        return self


class MissionReplayResponse(SchemaBase):
    """Response contract for GET /mission/{id}/replay (§7, §8.5)."""

    mission_id: IdentifierStr
    entries: list[MissionReplayTimelineEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Static Floor-Plan Configuration Schemas (§7, §8.2 — Hardware/IoT §11 stub)
# ---------------------------------------------------------------------------


class FloorPlanRoomSchema(SchemaBase):
    """Indoor room boundary rectangle for the SVG floor map (§7, §8.2)."""

    id: IdentifierStr
    name: NonEmptyStr
    x: CoordinateFloat
    y: CoordinateFloat
    width: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    height: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]


class FloorPlanWaypointSchema(SchemaBase):
    """Fixed indoor waypoint marker for the SVG floor map (§7, §8.2)."""

    id: IdentifierStr
    label: NonEmptyStr
    x: CoordinateFloat
    y: CoordinateFloat


class FloorPlanGeofenceSchema(SchemaBase):
    """HAWK aerial geofence boundary rectangle (§8.2)."""

    id: IdentifierStr
    label: NonEmptyStr
    x: CoordinateFloat
    y: CoordinateFloat
    width: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    height: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    max_altitude_m: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 3.0


class FloorPlanConfigResponse(SchemaBase):
    """GET /map response payload (§7, §8.2).

    Explicitly labeled as a provisional simulation configuration until Hardware & IoT
    doc §11 indoor dimensions and geofence coordinates are supplied.
    """

    is_provisional_simulation_config: bool = True
    coordinate_system: str = "local_cartesian_meters"
    width_m: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    height_m: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    rooms: list[FloorPlanRoomSchema] = Field(default_factory=list)
    waypoints: list[FloorPlanWaypointSchema] = Field(default_factory=list)
    hawk_geofence: FloorPlanGeofenceSchema
