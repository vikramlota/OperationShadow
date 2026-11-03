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
    """POST /mission request payload (§7: `{ "objective": "string", "language": "English" }`)."""

    objective: Annotated[str, Field(min_length=1, max_length=2000)]
    language: Annotated[str, Field(min_length=1, max_length=64)] = "English"


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
    """POST /emergency-stop request payload (§7: `{ "vehicle_id": "ALL" | "SCOUT01" }`)."""

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
    """Single chronological item in GET /mission/{id}/replay (§7, §8.5 + Phase 3 Mission Replay).

    `world_model_snapshot` contains structured historical before/trigger/decision/after
    World Model state when persisted in `mission_snapshots`, or an explicit
    `historical_state_available: False` / `None` marker when historical state was not recorded.
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
# Phase 3 After-Action Mission Report Schemas (`GET /mission/{id}/report`)
# ---------------------------------------------------------------------------

TaskEvidenceStageLiteral = Literal[
    "PLANNED",
    "ASSIGNED",
    "COMMAND_ACCEPTED",
    "COMMAND_DISPATCHED",
    "VEHICLE_ACKNOWLEDGED",
    "PHYSICAL_EXECUTION_CONFIRMED",
    "SIMULATED_COMPLETED",
    "IN_PROGRESS",
    "CANCELLED",
    "FAILED",
]


class MissionReportOverview(SchemaBase):
    """Mission overview section of the After-Action Report."""

    mission_id: IdentifierStr
    objective: str
    created_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    final_status: str
    vehicle_mode: str = "simulated"
    is_simulated_execution: bool = True
    hardware_lockout_active: bool = True
    execution_provenance: str

    @field_validator("created_at", "completed_at", mode="after")
    @classmethod
    def _normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


class MissionReportTaskRecord(SchemaBase):
    """Individual task execution and provenance record in the After-Action Report."""

    task_id: str
    vehicle_id: str = "SCOUT01"
    task_type: str
    area: str | None = None
    target: str | None = None
    priority: int = 1
    status: str
    highest_evidence_stage: TaskEvidenceStageLiteral = "ASSIGNED"
    is_simulated: bool = True
    source_trace_ids: list[str] = Field(default_factory=list)
    provenance_note: str


class MissionReportTaskSummary(SchemaBase):
    """Task statistics and execution breakdown in the After-Action Report."""

    total_tasks: int = Field(ge=0)
    completed_tasks: int = Field(ge=0)
    pending_tasks: int = Field(ge=0)
    assigned_tasks: int = Field(default=0, ge=0)
    in_progress_tasks: int = Field(default=0, ge=0)
    failed_tasks: int = Field(ge=0)
    cancelled_tasks: int = Field(ge=0)
    tasks: list[MissionReportTaskRecord] = Field(default_factory=list)


class MissionReportObjectFinding(SchemaBase):
    """Detected object, target, or obstacle finding in the After-Action Report."""

    object_id: str
    label: str
    coordinate_frame: str
    position_x: float
    position_y: float
    position_z: float = 0.0
    range_m: float | None = None
    map_position_x: float | None = None
    map_position_y: float | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    epistemic_status: str = "OBSERVED"
    tracking_status: str = "ACTIVE"
    source_mode: str = "simulated"
    is_obstacle: bool = False
    is_hazard: bool = False
    matches_objective_target: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    coordinate_frame_note: str


class MissionReportHazardFinding(SchemaBase):
    """Reported hazard finding in the After-Action Report."""

    hazard_id: str
    hazard_type: str
    coordinate_frame: str
    position_x: float
    position_y: float
    confidence: float = Field(ge=0.0, le=1.0)
    epistemic_status: str = "OBSERVED"
    source_mode: str = "simulated"
    source_vehicle: str = "SCOUT01"
    evidence_ids: list[str] = Field(default_factory=list)
    handled: bool = False
    coordinate_frame_note: str


class MissionReportObservationFinding(SchemaBase):
    """Persisted sensor observation record in the After-Action Report."""

    observation_id: str
    vehicle_id: str
    sensor_type: str
    obs_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    coordinate_frame: str
    position_x: float
    position_y: float
    position_z: float = 0.0
    range_m: float | None = None
    timestamp: str
    source_mode: str = "simulated"
    epistemic_status: str = "OBSERVED"
    is_stale: bool = False


class MissionReportEnvironmentalFindings(SchemaBase):
    """Environmental findings section of the After-Action Report."""

    detected_objects: list[MissionReportObjectFinding] = Field(default_factory=list)
    identified_targets: list[MissionReportObjectFinding] = Field(default_factory=list)
    reported_hazards: list[MissionReportHazardFinding] = Field(default_factory=list)
    obstacles: list[MissionReportObjectFinding] = Field(default_factory=list)
    sensor_observations: list[MissionReportObservationFinding] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    coordinate_frame_limitations: list[str] = Field(default_factory=list)
    coverage_note: str


class MissionReportEventRecord(SchemaBase):
    """Important event entry in the After-Action Report."""

    event_id: str
    timestamp: datetime
    category: str
    event_type: str | None = None
    severity: str = "INFO"
    source: str = "mission"
    source_mode: str = "simulated"
    message: str
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_ts(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class MissionReportStandingOrderRecord(SchemaBase):
    """Standing Order application record in the After-Action Report."""

    standing_order_id: str
    source_event_id: str | None = None
    trace_id: str | None = None
    timestamp: str
    rationale: str
    resulting_task_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class MissionReportTriageRecord(SchemaBase):
    """LLM or deterministic triage outcome record in the After-Action Report."""

    trace_id: str
    event_id: str | None = None
    event_type: str | None = None
    disposition: str
    severity: str = "INFO"
    triage_source: str = "fallback"
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    timestamp: str


class MissionReportDecisionRecord(SchemaBase):
    """Planning, replanning, or rejected decision record in the After-Action Report."""

    trace_id: str
    timestamp: datetime
    trigger: str
    rationale: str
    source_event_id: str | None = None
    standing_order_id: str | None = None
    triage_disposition: str | None = None
    planner_source: str | None = None
    validation_passed: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    resulting_task_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    before_snapshot_id: str | None = None
    after_snapshot_id: str | None = None

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_ts(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


class MissionReportEscalationRecord(SchemaBase):
    """Human operator escalation event in the After-Action Report."""

    record_id: str
    timestamp: str
    reason: str
    source_event_id: str | None = None
    standing_order_id: str | None = None


class MissionReportDecisionHistory(SchemaBase):
    """ULTRON Decision History section of the After-Action Report."""

    important_events: list[MissionReportEventRecord] = Field(default_factory=list)
    standing_orders_applied: list[MissionReportStandingOrderRecord] = Field(
        default_factory=list
    )
    llm_triage_outcomes: list[MissionReportTriageRecord] = Field(default_factory=list)
    replanning_operations: list[MissionReportDecisionRecord] = Field(
        default_factory=list
    )
    rejected_decisions: list[MissionReportDecisionRecord] = Field(default_factory=list)
    accepted_plans: list[MissionReportDecisionRecord] = Field(default_factory=list)
    human_escalation_events: list[MissionReportEscalationRecord] = Field(
        default_factory=list
    )


class MissionReportBatteryRecord(SchemaBase):
    """Battery observation summary for a vehicle during the mission."""

    min_battery: float = Field(ge=0.0, le=100.0)
    max_battery: float = Field(ge=0.0, le=100.0)
    final_battery: float = Field(ge=0.0, le=100.0)
    sample_count: int = Field(ge=0)
    critical_low_battery_observed: bool = False


class MissionReportVehicleSummary(SchemaBase):
    """Vehicle summary section of the After-Action Report."""

    vehicle_id: str = "SCOUT01"
    vehicle_type: str = "ground"
    mode: str = "simulated"
    final_status: str
    final_connection: str
    connection_history: list[str] = Field(default_factory=list)
    battery_observations: MissionReportBatteryRecord
    task_participation_ids: list[str] = Field(default_factory=list)
    communication_failures: list[str] = Field(default_factory=list)
    operational_limitations: list[str] = Field(default_factory=list)


class MissionReportSafetyEvent(SchemaBase):
    """Safety event, validation failure, or critical fault record in the After-Action Report."""

    record_id: str
    timestamp: str
    category: Literal["EMERGENCY_STOP", "VALIDATION_FAILURE", "CRITICAL_FAULT", "SAFETY_WARNING"]
    description: str
    stop_dispatched: bool | None = None
    vehicle_acknowledged: bool | None = None
    physical_stop_confirmed: bool | None = None
    is_simulated: bool = True


class MissionReportSafetySummary(SchemaBase):
    """Safety summary section of the After-Action Report."""

    emergency_stop_events: list[MissionReportSafetyEvent] = Field(default_factory=list)
    safety_validation_failures: list[MissionReportSafetyEvent] = Field(
        default_factory=list
    )
    critical_faults: list[MissionReportSafetyEvent] = Field(default_factory=list)
    stop_dispatch_acknowledged_status: str
    unresolved_safety_issues: list[str] = Field(default_factory=list)


class MissionReportUnresolvedItems(SchemaBase):
    """Unresolved items, gaps, conflicts, and unknowns in the After-Action Report."""

    incomplete_tasks: list[str] = Field(default_factory=list)
    missing_observations: list[str] = Field(default_factory=list)
    stale_telemetry_or_observations: list[str] = Field(default_factory=list)
    conflicting_evidence: list[str] = Field(default_factory=list)
    unverified_sensor_signals: list[str] = Field(default_factory=list)
    unknown_outcomes: list[str] = Field(default_factory=list)


class MissionReportResponse(SchemaBase):
    """Dedicated structured After-Action Mission Report returned by GET /mission/{id}/report."""

    mission_id: IdentifierStr
    generated_at: datetime
    report_deterministic: bool = True
    llm_used_for_report: bool = False
    historical_snapshots_count: int = Field(default=0, ge=0)
    historical_state_available: bool = True
    summary_text: str
    overview: MissionReportOverview
    task_summary: MissionReportTaskSummary
    environmental_findings: MissionReportEnvironmentalFindings
    decision_history: MissionReportDecisionHistory
    vehicle_summary: MissionReportVehicleSummary
    safety_summary: MissionReportSafetySummary
    unresolved_items: MissionReportUnresolvedItems
    markdown_export: str

    @field_validator("generated_at", mode="after")
    @classmethod
    def _normalize_generated_at(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized


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
    hawk_geofence: FloorPlanGeofenceSchema | None = None


class MapUploadResponse(SchemaBase):
    """POST /upload-map/{vehicle_id} response schema."""

    status: str = "success"
    vehicle_id: str
    vehicle: str | None = None
    saved_files: list[str] = Field(default_factory=list)
    filenames: list[str] = Field(default_factory=list)
    saved: list[str] = Field(default_factory=list)
