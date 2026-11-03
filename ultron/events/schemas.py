"""Typed internal event contracts for ULTRON Phase 2 (`ultron/events/schemas.py`).

Kept strictly separate from `MissionPlan` (the Gemini planning response_schema) and
from public REST/WebSocket schemas. Enforces finite timestamps, single-vehicle SCOUT01
constraints, and explicit `UNKNOWN` / `None` preservation for missing metadata.
"""

from __future__ import annotations

from enum import Enum
import math
from typing import Any, Dict, List, Literal, Optional
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ultron.schemas.observations import EpistemicStatusEnum, SensorSourceModeEnum


class EventTypeEnum(str, Enum):
    """Supported normalized ULTRON internal event types."""

    HAZARD_DETECTED = "HAZARD_DETECTED"
    OBSTACLE_DETECTED = "OBSTACLE_DETECTED"
    TARGET_DETECTED = "TARGET_DETECTED"
    SENSOR_CONFLICT = "SENSOR_CONFLICT"
    SENSOR_DEGRADED = "SENSOR_DEGRADED"
    OBSERVATION_STALE = "OBSERVATION_STALE"
    VEHICLE_LOW_BATTERY = "VEHICLE_LOW_BATTERY"
    VEHICLE_DISCONNECTED = "VEHICLE_DISCONNECTED"
    VEHICLE_FAULT = "VEHICLE_FAULT"
    MISSION_STATE_CHANGED = "MISSION_STATE_CHANGED"


class EventSourceEnum(str, Enum):
    """Originating subsystem for a normalized ULTRON event."""

    CAMERA = "camera"
    ROSBRIDGE = "rosbridge"
    TELEMETRY = "telemetry"
    SENSOR_HEALTH = "sensor_health"
    VEHICLE_STATE = "vehicle_state"
    WORLD_MODEL = "world_model"
    MISSION_LIFECYCLE = "mission_lifecycle"


class EventPriorityEnum(str, Enum):
    """Deterministic event priority levels."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EventRouteTargetEnum(str, Enum):
    """Downstream handler classification selected by the Event Router."""

    STANDING_ORDER = "STANDING_ORDER"
    LLM_TRIAGE = "LLM_TRIAGE"
    LOG_ONLY = "LOG_ONLY"
    SUPPRESSED = "SUPPRESSED"
    REJECTED = "REJECTED"


_PRIORITY_RANKS: dict[EventPriorityEnum, int] = {
    EventPriorityEnum.CRITICAL: 4,
    EventPriorityEnum.HIGH: 3,
    EventPriorityEnum.MEDIUM: 2,
    EventPriorityEnum.LOW: 1,
}


def priority_rank(priority: EventPriorityEnum) -> int:
    """Return numeric rank (4=CRITICAL .. 1=LOW) for deterministic ordering."""
    return _PRIORITY_RANKS[priority]


def default_priority_for_event_type(event_type: EventTypeEnum) -> EventPriorityEnum:
    """Return the deterministic baseline priority for `event_type`."""
    if event_type == EventTypeEnum.VEHICLE_FAULT:
        return EventPriorityEnum.CRITICAL
    if event_type in (
        EventTypeEnum.HAZARD_DETECTED,
        EventTypeEnum.OBSTACLE_DETECTED,
        EventTypeEnum.SENSOR_CONFLICT,
        EventTypeEnum.VEHICLE_LOW_BATTERY,
    ):
        return EventPriorityEnum.HIGH
    if event_type in (
        EventTypeEnum.TARGET_DETECTED,
        EventTypeEnum.SENSOR_DEGRADED,
        EventTypeEnum.OBSERVATION_STALE,
        EventTypeEnum.VEHICLE_DISCONNECTED,
    ):
        return EventPriorityEnum.MEDIUM
    return EventPriorityEnum.LOW


class UltronEvent(BaseModel):
    """Normalized internal event contract for the Operation Shadow Event Router."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    event_id: str = Field(
        default_factory=lambda: f"UEVT-{uuid.uuid4().hex[:8].upper()}",
        min_length=1,
        max_length=64,
        description="Unique internal event ID",
    )
    event_type: EventTypeEnum = Field(description="Normalized event category")
    source: EventSourceEnum = Field(description="Originating sensor or subsystem")
    source_mode: SensorSourceModeEnum = Field(
        default=SensorSourceModeEnum.SIMULATED,
        description="Whether the underlying signal is physical or simulated (per SIGNAL_AUDIT.md)",
    )
    mission_id: Optional[str] = Field(
        default=None,
        description="Active mission ID if associated, or None when unassociated",
    )
    vehicle_id: Optional[str] = Field(
        default=None,
        description="Verified vehicle ID ('SCOUT01') or None if unassociated",
    )
    source_timestamp: Optional[float] = Field(
        default=None,
        description="Finite positive Unix epoch timestamp from sensor/source, or None if unavailable",
    )
    received_at: float = Field(
        description="Finite positive backend receive timestamp in Unix epoch seconds"
    )
    priority: Optional[EventPriorityEnum] = Field(
        default=None,
        description="Deterministic priority; populated from event_type if omitted",
    )
    observation_ids: List[str] = Field(
        default_factory=list,
        description="Associated validated observation IDs in the World Model",
    )
    tracked_object_ids: List[str] = Field(
        default_factory=list,
        description="Associated TrackedObject IDs in the World Model",
    )
    coordinate_frame: Optional[str] = Field(
        default=None,
        description="Coordinate frame of spatial evidence (preserved verbatim, never assumed 'map')",
    )
    confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional sensor/detector confidence [0.0, 1.0]",
    )
    epistemic_status: EpistemicStatusEnum = Field(
        default=EpistemicStatusEnum.UNKNOWN,
        description="Epistemic status of the underlying evidence",
    )
    evidence_payload: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured evidence metadata (positions, ranges, battery, conflict details)",
    )
    summary: str = Field(
        min_length=1,
        max_length=2000,
        description="Human-readable diagnostic summary of the event",
    )
    dedup_signature: Optional[str] = Field(
        default=None,
        description="Deterministic signature used for duplicate suppression",
    )

    @field_validator("event_id", "summary")
    @classmethod
    def _non_blank_str(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("Field cannot be blank.")
        return cleaned

    @field_validator("vehicle_id")
    @classmethod
    def _validate_vehicle_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            return None
        if cleaned != "SCOUT01":
            raise ValueError(
                f"Unsupported vehicle_id '{cleaned}'. Only 'SCOUT01' is active in this deployment."
            )
        return cleaned

    @field_validator("source_timestamp")
    @classmethod
    def _validate_source_timestamp(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("source_timestamp must be a finite positive number or None.")
        val = float(value)
        if not math.isfinite(val) or val <= 0.0:
            raise ValueError("source_timestamp must be finite and > 0.0.")
        return val

    @field_validator("received_at")
    @classmethod
    def _validate_received_at(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("received_at must be a finite positive number.")
        val = float(value)
        if not math.isfinite(val) or val <= 0.0:
            raise ValueError("received_at must be finite and > 0.0.")
        return val

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a finite number in [0.0, 1.0].")
        val = float(value)
        if not math.isfinite(val) or val < 0.0 or val > 1.0:
            raise ValueError("confidence must be finite and within [0.0, 1.0].")
        return val

    @model_validator(mode="after")
    def _populate_defaults_and_reject_estop(self) -> "UltronEvent":
        if self.priority is None:
            self.priority = default_priority_for_event_type(self.event_type)
        status_in_payload = str(self.evidence_payload.get("vehicle_status", "")).strip().upper()
        if status_in_payload == "EMERGENCY_STOPPED":
            raise ValueError(
                "Emergency Stop (EMERGENCY_STOPPED) is handled exclusively by the Emergency Stop Fast Path "
                "and must never enter the Event Router."
            )
        return self


class RoutedEventOutcome(BaseModel):
    """Structured result returned when an event is submitted to the Event Router."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    status: Literal[
        "routed",
        "suppressed_duplicate",
        "rejected_out_of_order",
        "rejected_stale",
        "rejected_malformed",
        "excluded_emergency_stop",
    ]
    route_target: EventRouteTargetEnum
    reason: str
    event: Optional[UltronEvent] = None
    dedup_signature: Optional[str] = None
    coalesced_count: int = 1
