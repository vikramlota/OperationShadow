from enum import Enum
from typing import Any, Dict, List, Optional, Literal, Union
from pydantic import BaseModel, Field
from ultron.schemas.mission import MissionStatus, VehicleID
from ultron.schemas.commands import Position2D, Position3D
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorSourceModeEnum,
    SensorTypeEnum,
)


class ConnectionStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class VehicleMode(str, Enum):
    REAL = "real"
    SIMULATED = "simulated"


class SensorHealthStatusEnum(str, Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    ERROR = "ERROR"
    SIMULATED = "SIMULATED"


class TrackingStatusEnum(str, Enum):
    ACTIVE = "ACTIVE"
    REMEMBERED = "REMEMBERED"
    STALE = "STALE"
    CONFLICTED = "CONFLICTED"
    LOST = "LOST"


class ScoutState(BaseModel):
    robot_id: Literal["SCOUT01"] = "SCOUT01"
    vehicle_type: Literal["ground"] = "ground"
    mode: VehicleMode = VehicleMode.SIMULATED
    position: Position2D = Position2D(x=0.0, y=0.0)
    heading: float = 0.0
    pose_valid: bool = True
    coordinate_frame: str = "map"
    battery: float = Field(ge=0.0, le=100.0, default=100.0)
    status: str = "IDLE"
    connection: ConnectionStatus = ConnectionStatus.ONLINE
    temperature: Optional[float] = None
    gas_level: Optional[str] = "normal"
    current_task: Optional[str] = None
    last_heartbeat_timestamp: Optional[float] = None


class HazardEntity(BaseModel):
    id: str
    location: Position2D
    coordinate_frame: str = "map"
    confidence: float
    source_vehicle: VehicleID
    type: str = "possible_hazard"
    handled: bool = False
    observation_id: Optional[str] = None
    timestamp: Optional[float] = None
    epistemic_status: EpistemicStatusEnum = EpistemicStatusEnum.OBSERVED
    source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED


class ObjectEntity(BaseModel):
    id: str
    location: Position2D
    coordinate_frame: str = "map"
    label: str
    confidence: float
    source_vehicle: VehicleID
    handled: bool = False
    observation_id: Optional[str] = None
    timestamp: Optional[float] = None
    epistemic_status: EpistemicStatusEnum = EpistemicStatusEnum.OBSERVED
    source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED


class TrackedObject(BaseModel):
    """Persistent tracked object or obstacle in the World Model (§4, §5).

    Maintains a stable internal `object_id` distinct from frame-local detector IDs,
    tracks multi-sensor provenance, uncertainty, freshness, and epistemic status.
    """

    object_id: str = Field(description="Stable internal World Model object ID (e.g. 'OBJ-001')")
    label: str = Field(description="Sanitized classification or 'obstacle'")
    position: Position3DMeasurement = Field(description="Current 3D position in coordinate_frame")
    coordinate_frame: str = Field(description="Coordinate frame of position")
    map_position: Optional[Position2D] = Field(
        default=None,
        description="Global map position ONLY when coordinate_frame=='map' and transform is validated",
    )
    position_uncertainty_m: float = Field(default=0.15, ge=0.0)
    classification_confidence: float = Field(ge=0.0, le=1.0, default=0.8)
    first_observed_timestamp: float = Field(gt=0.0)
    last_observed_timestamp: float = Field(gt=0.0)
    last_ingested_at: float = Field(gt=0.0)
    last_known_source: str = Field(description="Sensor ID of most recent observation")
    vehicle_id: Optional[str] = Field(default=None)
    source_mode: SensorSourceModeEnum = Field(default=SensorSourceModeEnum.SIMULATED)
    contributing_observation_ids: List[str] = Field(default_factory=list)
    contributing_sensor_types: List[str] = Field(default_factory=list)
    last_detector_object_id: Optional[int] = Field(default=None)
    tracking_status: TrackingStatusEnum = Field(default=TrackingStatusEnum.ACTIVE)
    epistemic_status: EpistemicStatusEnum = Field(default=EpistemicStatusEnum.OBSERVED)
    currently_visible: bool = Field(default=True)
    corroborated_by_multiple_sensors: bool = Field(default=False)
    is_obstacle: bool = Field(default=False)
    is_hazard: bool = Field(default=False)
    fusion_timestamp: Optional[float] = None
    association_method: Optional[str] = None
    association_score: Optional[float] = None
    conflict_details: Optional[str] = None
    discrepant_positions: List[Dict[str, Any]] = Field(default_factory=list)
    handled: bool = False


class SensorHealthReport(BaseModel):
    """Per-sensor health, freshness, frequency, and error state (§3)."""

    sensor_id: str
    sensor_type: SensorTypeEnum
    enabled: bool = False
    status: SensorHealthStatusEnum = SensorHealthStatusEnum.UNAVAILABLE
    source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED
    topic: str = ""
    message_type: str = ""
    coordinate_frame: str = ""
    vehicle_id: Optional[str] = None
    last_message_timestamp: Optional[float] = None
    last_ingested_at: Optional[float] = None
    message_frequency_hz: float = Field(default=0.0, ge=0.0)
    is_stale: bool = False
    stale_after_sec: float = Field(default=2.0, gt=0.0)
    valid_message_count: int = Field(default=0, ge=0)
    malformed_message_count: int = Field(default=0, ge=0)
    duplicate_message_count: int = Field(default=0, ge=0)
    out_of_order_count: int = Field(default=0, ge=0)
    error_state: Optional[str] = None
    notes: Optional[str] = None


class EvidenceReference(BaseModel):
    """Structured citation linking a claim or decision to a specific observation (§7)."""

    observation_id: str
    object_id: Optional[str] = None
    sensor: str
    sensor_type: str = "depth_camera"
    timestamp: float
    coordinate_frame: str
    confidence: Optional[float] = None
    epistemic_status: EpistemicStatusEnum = EpistemicStatusEnum.OBSERVED
    source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED


class GroundedEnvironmentalResponse(BaseModel):
    """Structured evidence-grounded response from ULTRON (§7)."""

    answer: str
    facts: List[str] = Field(default_factory=list)
    inferences: List[str] = Field(default_factory=list)
    unknowns: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    evidence: List[EvidenceReference] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low", "unavailable"] = "medium"
    source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED


class DecisionTraceEntry(BaseModel):
    """Structured audit record for ULTRON planning and replanning decisions (§8, §9)."""

    trace_id: str
    mission_id: str
    timestamp: float
    trigger: str
    environmental_facts_used: List[str] = Field(default_factory=list)
    inferences_made: List[str] = Field(default_factory=list)
    unknowns_and_uncertainties: List[str] = Field(default_factory=list)
    sensor_sources: List[str] = Field(default_factory=list)
    evidence_references: List[EvidenceReference] = Field(default_factory=list)
    decision_summary: str
    rationale: str
    safety_validator_passed: bool = True
    safety_validator_modified_or_rejected: bool = False
    validation_errors: List[str] = Field(default_factory=list)
    replanning_triggered: bool = False
    resulting_task_ids: List[str] = Field(default_factory=list)
    confidence_level: Literal["high", "medium", "low", "unavailable"] = "medium"
    is_simulated: bool = True


def _default_active_tasks() -> Dict[VehicleID, Optional[str]]:
    return {VehicleID.SCOUT: None}


def _default_vehicles() -> Dict[str, ScoutState]:
    return {"SCOUT01": ScoutState()}


def _default_sensor_health() -> Dict[str, SensorHealthReport]:
    return {
        "depth_camera": SensorHealthReport(
            sensor_id="depth_camera",
            sensor_type=SensorTypeEnum.DEPTH_CAMERA,
            enabled=False,
            status=SensorHealthStatusEnum.DISCONNECTED,
            source_mode=SensorSourceModeEnum.SIMULATED,
            topic="/detections_json",
            message_type="std_msgs/msg/String",
            coordinate_frame="oak_rgb_camera_optical_frame",
            vehicle_id=None,
            stale_after_sec=2.0,
            notes="Depth camera object detection stream (/detections_json). Raw depth images are not subscribed.",
        ),
        "lidar": SensorHealthReport(
            sensor_id="lidar",
            sensor_type=SensorTypeEnum.LIDAR,
            enabled=False,
            status=SensorHealthStatusEnum.UNAVAILABLE,
            source_mode=SensorSourceModeEnum.SIMULATED,
            topic="",
            message_type="",
            coordinate_frame="",
            vehicle_id=None,
            stale_after_sec=2.0,
            notes="LiDAR topic, message type, frame, and extrinsics are unconfirmed by hardware team; marked UNAVAILABLE.",
        ),
    }


class TaskTracker(BaseModel):
    completed: List[str] = Field(default_factory=list)
    active: Dict[VehicleID, Optional[str]] = Field(
        default_factory=_default_active_tasks
    )
    pending: List[str] = Field(default_factory=list)


class MissionState(BaseModel):
    id: str
    objective: str
    status: MissionStatus = MissionStatus.IDLE
    tasks: TaskTracker = Field(default_factory=TaskTracker)


class EnvironmentState(BaseModel):
    explored_areas: List[str] = Field(default_factory=list)
    known_obstacles: List[Position2D] = Field(default_factory=list)
    hazards: List[HazardEntity] = Field(default_factory=list)
    objects: List[ObjectEntity] = Field(default_factory=list)

    # Evidence-grounded tracking, observation history, sensor health, and decision traces
    tracked_objects: Dict[str, TrackedObject] = Field(default_factory=dict)
    observation_history: List[SensorObservation] = Field(default_factory=list)
    sensor_health: Dict[str, SensorHealthReport] = Field(
        default_factory=_default_sensor_health
    )
    decision_traces: List[DecisionTraceEntry] = Field(default_factory=list)
    last_updated_timestamp: Optional[float] = None
    last_replan_timestamp: Optional[float] = None
    last_replan_signature: Optional[str] = None
    transform_map_validated: bool = False
    extrinsics_camera_lidar_validated: bool = False
    free_space_coverage_m: Optional[float] = None


class WorldModel(BaseModel):
    mission: MissionState
    vehicles: Dict[str, ScoutState] = Field(
        default_factory=_default_vehicles
    )
    environment: EnvironmentState = Field(default_factory=EnvironmentState)
