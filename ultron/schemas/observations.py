import math
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from ultron.schemas.mission import VehicleID
from ultron.schemas.commands import Position2D


class SensorTypeEnum(str, Enum):
    DEPTH_CAMERA = "depth_camera"
    LIDAR = "lidar"
    TELEMETRY = "telemetry"
    SYNTHETIC = "synthetic"


class SensorSourceModeEnum(str, Enum):
    SIMULATED = "simulated"
    PHYSICAL = "physical"


class ObservationValidationStatusEnum(str, Enum):
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    UNVERIFIED = "UNVERIFIED"


class EpistemicStatusEnum(str, Enum):
    """Mandatory epistemic classification for every environmental statement or tracked entity (§4)."""

    OBSERVED = "OBSERVED"
    CORROBORATED = "CORROBORATED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    CONFLICTED = "CONFLICTED"


class Position3DMeasurement(BaseModel):
    """3D coordinate and range measurement in the reported coordinate frame."""

    x: float
    y: float
    z: float = 0.0
    range_m: Optional[float] = None
    uncertainty_m: float = Field(default=0.15, ge=0.0)

    @field_validator("x", "y", "z")
    @classmethod
    def _validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Coordinate values must be finite numbers.")
        return float(value)

    @field_validator("range_m")
    @classmethod
    def _validate_range(cls, value: Optional[float]) -> Optional[float]:
        if value is not None:
            if not math.isfinite(value) or value < 0.0:
                raise ValueError("range_m must be a finite non-negative number.")
            return float(value)
        return None

    def computed_range_m(self) -> float:
        if self.range_m is not None:
            return self.range_m
        return round(math.sqrt(self.x ** 2 + self.y ** 2 + self.z ** 2), 4)


class SensorObservation(BaseModel):
    """Structured, validated environmental observation with full provenance (§4)."""

    observation_id: str = Field(min_length=1, description="Unique observation ID")
    sensor_id: str = Field(min_length=1, description="Originating sensor identifier")
    sensor_type: SensorTypeEnum = Field(description="Sensor modality")
    vehicle_id: Optional[str] = Field(
        default=None,
        description="Verified vehicle ID (e.g. 'SCOUT01'), or None if unconfirmed",
    )
    detector_object_id: Optional[int] = Field(
        default=None,
        description="Frame-local detector object ID (never treated as persistent global ID)",
    )
    sensor_timestamp: float = Field(
        gt=0.0, description="Timestamp from the sensor in Unix epoch seconds"
    )
    ingested_at: float = Field(
        gt=0.0, description="Local ingestion timestamp in Unix epoch seconds"
    )
    coordinate_frame: str = Field(
        min_length=1,
        description="Original coordinate frame preserved verbatim (e.g. 'oak_rgb_camera_optical_frame')",
    )
    position: Position3DMeasurement = Field(
        description="3D position or geometric measurement in coordinate_frame"
    )
    classification: Optional[str] = Field(
        default=None,
        description="Sanitized object classification label (treated strictly as untrusted data)",
    )
    confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Sensor-supplied detection confidence [0.0, 1.0]",
    )
    raw_reference: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Raw payload or source message reference for traceability",
    )
    validation_status: ObservationValidationStatusEnum = Field(
        default=ObservationValidationStatusEnum.VALIDATED
    )
    source_mode: SensorSourceModeEnum = Field(
        default=SensorSourceModeEnum.SIMULATED,
        description="Explicit distinction between simulated and physical hardware observations",
    )
    is_obstacle: bool = Field(default=False)
    is_hazard: bool = Field(default=False)

    @field_validator("sensor_timestamp", "ingested_at")
    @classmethod
    def _finite_timestamp(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("Timestamps must be finite positive numbers.")
        return float(value)

    @field_validator("classification")
    @classmethod
    def _sanitize_classification(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("classification cannot be blank string")
        return cleaned[:100]


class LidarObstacleMeasurement(BaseModel):
    """Single obstacle or geometric boundary measurement from LiDAR."""

    measurement_id: str = Field(min_length=1)
    position: Position3DMeasurement
    radius_m: float = Field(default=0.25, gt=0.0)
    point_count: int = Field(default=1, ge=1)


class LidarScanObservation(BaseModel):
    """Validated LiDAR scan/point-cloud observation frame (§3)."""

    observation_id: str = Field(min_length=1)
    sensor_id: str = Field(default="lidar_primary")
    vehicle_id: Optional[str] = None
    stamp: float = Field(gt=0.0)
    ingested_at: float = Field(gt=0.0)
    frame: str = Field(min_length=1)
    source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED
    min_range_m: float = Field(default=0.05, gt=0.0)
    max_range_m: float = Field(default=30.0, gt=0.1)
    obstacles: List[LidarObstacleMeasurement] = Field(default_factory=list)
    free_space_radius_m: Optional[float] = Field(default=None, ge=0.0)
    raw_reference: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _check_ranges(self) -> "LidarScanObservation":
        if self.max_range_m <= self.min_range_m:
            raise ValueError("max_range_m must be greater than min_range_m")
        for obs in self.obstacles:
            r = obs.position.computed_range_m()
            if r < self.min_range_m or r > self.max_range_m:
                raise ValueError(
                    f"LiDAR obstacle range {r:.3f}m outside valid sensor bounds "
                    f"[{self.min_range_m}, {self.max_range_m}]m"
                )
        return self


class StructuredObservation(BaseModel):
    """Legacy-compatible structured observation model bridged into SensorObservation."""

    event: str = Field(
        description="Observation type: 'possible_hazard', 'target_detected', 'obstacle_blocked', or 'detection:<class>'"
    )
    source_vehicle: VehicleID = Field(
        description="Originating vehicle providing observation data"
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Vision detector or sensor confidence score"
    )
    location: Position2D = Field(
        description="Estimated 2D coordinate of the detected entity"
    )
    position_z: float = Field(
        default=0.0, description="Optional Z coordinate in reported frame"
    )
    range_m: Optional[float] = Field(
        default=None, ge=0.0, description="Measured range in meters"
    )
    coordinate_frame: str = Field(
        default="map",
        description="Coordinate frame of location (e.g. 'oak_rgb_camera_optical_frame', 'map')",
    )
    detector_object_id: Optional[int] = Field(
        default=None, description="Frame-local detector object ID"
    )
    observation_id: Optional[str] = Field(
        default=None, description="Unique observation identifier"
    )
    sensor_id: str = Field(default="oak_d_camera")
    sensor_type: SensorTypeEnum = Field(default=SensorTypeEnum.DEPTH_CAMERA)
    source_mode: SensorSourceModeEnum = Field(default=SensorSourceModeEnum.SIMULATED)
    ingested_at: Optional[float] = Field(default=None)
    temperature: Optional[float] = Field(
        default=None, description="Ambient or target temperature in Celsius"
    )
    gas_level: Optional[str] = Field(
        default=None, description="Gas anomaly state: 'normal', 'elevated', 'critical'"
    )
    visual_evidence: Optional[str] = Field(
        default=None, description="Detector label metadata (treated strictly as data)"
    )
    timestamp: Optional[float] = Field(
        default=None, description="Epoch timestamp of sensor observation"
    )
    raw_reference: Optional[Dict[str, Any]] = Field(default=None)
