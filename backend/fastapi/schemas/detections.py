"""Pydantic v2 schemas and coordinate-transform port for ROS 2 Humble object detections (`/detections_json`).

Implements strict validation and coordinate-frame preservation rules (Sections 1, 2, 5, 7, 8):
- `DetectionPosition3D`: finite `x`, `y`, `z` coordinates in metres.
- `DetectionObject`: frame-local `id` (>= 0), `class` (aliased to `class_name`), `score` ([0.0, 1.0]),
  `position` (`x`, `y`, `z` in metres), and `range_m` (>= 0.0 in metres).
- `DetectionFrame`: `stamp` (finite positive Unix epoch seconds, or converted from ROS 2
  `{"sec": ..., "nanosec": ...}`), `frame` (exact incoming string preserved verbatim, e.g.,
  `"oak_rgb_camera_optical_frame"`, `"oak_parent_frame"`, `"map"`, or any unrecognized frame),
  and `objects` (may legitimately be empty).
- `CoordinateTransformPort` & `UnvalidatedCoordinateTransformStub`: replaceable interface for
  future TF (`map -> odom -> oak_parent_frame`) and camera-to-LiDAR extrinsic transformations.
  Never silently transforms, shifts, clamps, or reinterprets camera-relative coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, field_validator
from schemas.entities import (
    ConfidenceScore,
    CoordinateFloat,
    NonEmptyStr,
    SchemaBase,
    ensure_utc_datetime,
)
from schemas.enums import (
    DetectionCoordinateFrameEnum,
    DetectionFrameClassificationEnum,
    RosConnectionStateEnum,
    TelemetrySourceEnum,
    VehicleIdEnum,
)

NonNegativeMetres = Annotated[
    float,
    Field(ge=0.0, allow_inf_nan=False, description="Non-negative range in metres"),
]
EpochSecondsFloat = Annotated[
    float,
    Field(gt=0.0, allow_inf_nan=False, description="Source timestamp in Unix epoch seconds"),
]
DetectionIdInt = Annotated[
    int,
    Field(ge=0, description="Non-negative frame-local integer object detection ID"),
]


class DetectionPosition3D(SchemaBase):
    """3D position coordinate in metres within the reported detection coordinate frame."""

    x: CoordinateFloat
    y: CoordinateFloat
    z: CoordinateFloat


class DetectionObject(SchemaBase):
    """Single detected object inside a `/detections_json` payload.

    Note: `id` is a frame-local object identifier within the reported detection frame
    and is NOT assumed globally unique or persistent across frames.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )

    id: DetectionIdInt
    class_name: NonEmptyStr = Field(alias="class", serialization_alias="class")
    score: ConfidenceScore
    position: DetectionPosition3D
    range_m: NonNegativeMetres


def _parse_ros_stamp_to_epoch_seconds(value: object) -> float:
    """Parse Unix epoch seconds from a float/int or a ROS 2 `{sec, nanosec}` dict."""
    if isinstance(value, dict):
        if "sec" in value:
            sec_raw = value.get("sec", 0)
            nanosec_raw = value.get("nanosec", value.get("nsec", 0))
            if isinstance(sec_raw, (int, float)) and isinstance(nanosec_raw, (int, float)):
                computed = float(sec_raw) + (float(nanosec_raw) / 1e9)
                if not math.isfinite(computed) or computed <= 0.0:
                    msg = f"Invalid ROS stamp sec/nanosec value: {value}"
                    raise ValueError(msg)
                return computed
        msg = f"Unsupported stamp object structure: {value}"
        raise ValueError(msg)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fval = float(value)
        if not math.isfinite(fval) or fval <= 0.0:
            msg = f"stamp must be a finite positive number, got {value}"
            raise ValueError(msg)
        return fval
    msg = f"stamp must be a finite positive float or ROS {{sec, nanosec}} dict, got {type(value).__name__}"
    raise ValueError(msg)


def classify_detection_frame(
    frame: str | None,
    *,
    map_frame_confirmed: bool = False,
) -> tuple[DetectionFrameClassificationEnum, bool]:
    """Classify an incoming coordinate frame and determine whether global map projection is permitted.

    Rules (Section 2 & Section 8):
    - `"oak_rgb_camera_optical_frame"` -> `(CAMERA_OPTICAL_RELATIVE, False)`
    - `"oak_parent_frame"` -> `(CAMERA_PARENT_RELATIVE, False)`
    - `"map"` -> `(CONFIRMED_MAP, True)` ONLY if `map_frame_confirmed=True`, else `(UNCONFIRMED_MAP, False)`
    - Any other non-empty string -> `(UNSUPPORTED_FRAME, False)`
    """
    if not frame:
        return DetectionFrameClassificationEnum.UNSUPPORTED_FRAME, False
    if frame == DetectionCoordinateFrameEnum.OAK_RGB_CAMERA_OPTICAL_FRAME.value:
        return DetectionFrameClassificationEnum.CAMERA_OPTICAL_RELATIVE, False
    if frame == DetectionCoordinateFrameEnum.OAK_PARENT_FRAME.value:
        return DetectionFrameClassificationEnum.CAMERA_PARENT_RELATIVE, False
    if frame == DetectionCoordinateFrameEnum.MAP.value:
        if map_frame_confirmed:
            return DetectionFrameClassificationEnum.CONFIRMED_MAP, True
        return DetectionFrameClassificationEnum.UNCONFIRMED_MAP, False
    return DetectionFrameClassificationEnum.UNSUPPORTED_FRAME, False


@dataclass(frozen=True)
class CoordinateTransformResult:
    """Outcome of a coordinate transformation request."""

    source_frame: str
    target_frame: str
    is_validated: bool
    position: DetectionPosition3D | None
    reason: str


@runtime_checkable
class CoordinateTransformPort(Protocol):
    """Replaceable interface for future TF (`map -> odom -> oak_parent_frame`) and extrinsic transforms.

    Until live `/tf`, `/tf_static`, and camera-to-LiDAR extrinsics are validated by the
    hardware team, implementations MUST NOT silently transform or project camera-relative
    coordinates onto the global floor map.
    """

    def is_transform_validated(self, source_frame: str, target_frame: str = "map") -> bool:
        """Return True only if the transform from `source_frame` to `target_frame` is calibrated and verified."""
        ...

    def transform_position(
        self,
        position: DetectionPosition3D,
        *,
        source_frame: str,
        target_frame: str = "map",
    ) -> CoordinateTransformResult:
        """Transform `position` from `source_frame` to `target_frame` if and only if validated."""
        ...


class UnvalidatedCoordinateTransformStub(CoordinateTransformPort):
    """Default safety stub that blocks unverified TF/extrinsic coordinate projections."""

    def __init__(self, *, map_frame_confirmed: bool = False) -> None:
        self._map_frame_confirmed = map_frame_confirmed

    def is_transform_validated(self, source_frame: str, target_frame: str = "map") -> bool:
        return (
            self._map_frame_confirmed
            and source_frame == DetectionCoordinateFrameEnum.MAP.value
            and target_frame == DetectionCoordinateFrameEnum.MAP.value
        )

    def transform_position(
        self,
        position: DetectionPosition3D,
        *,
        source_frame: str,
        target_frame: str = "map",
    ) -> CoordinateTransformResult:
        if self.is_transform_validated(source_frame, target_frame):
            return CoordinateTransformResult(
                source_frame=source_frame,
                target_frame=target_frame,
                is_validated=True,
                position=position,
                reason="Identity map-frame transform explicitly confirmed.",
            )
        return CoordinateTransformResult(
            source_frame=source_frame,
            target_frame=target_frame,
            is_validated=False,
            position=None,
            reason=(
                f"Transform from '{source_frame}' to '{target_frame}' is unvalidated "
                "(TF chain map->odom->oak_parent_frame and camera-to-LiDAR extrinsics are not confirmed)."
            ),
        )


class DetectionFrame(SchemaBase):
    """Validated payload parsed from `std_msgs/msg/String.data` on `/detections_json`.

    Preserves the exact incoming `frame` identifier string without rejecting unknown frames,
    so the backend and UI can explicitly report unrecognized frames rather than dropping
    or misinterpreting them.
    """

    stamp: EpochSecondsFloat
    frame: NonEmptyStr
    objects: list[DetectionObject] = Field(default_factory=list)

    @field_validator("stamp", mode="before")
    @classmethod
    def _coerce_stamp(cls, value: object) -> float:
        return _parse_ros_stamp_to_epoch_seconds(value)

    @property
    def source_timestamp(self) -> datetime:
        """Convert Unix epoch seconds (`stamp`) to a timezone-aware UTC datetime."""
        return datetime.fromtimestamp(self.stamp, tz=timezone.utc)


class DetectionsPushPayload(SchemaBase):
    """Payload for `type = 'detections'` dashboard WebSocket pushes and snapshot state."""

    vehicle_id: VehicleIdEnum
    configured_vehicle_id: str | None = None
    telemetry_source: TelemetrySourceEnum = TelemetrySourceEnum.ROSBRIDGE
    actuation_locked: bool = True
    ros_connection_state: RosConnectionStateEnum = RosConnectionStateEnum.DISCONNECTED
    topic: str = "/detections_json"
    message_type: str = "std_msgs/msg/String"
    stamp: EpochSecondsFloat | None = None
    source_timestamp: datetime | None = None
    received_at: datetime | None = None
    clock_offset_seconds: float | None = None
    frame: str | None = None
    frame_classification: DetectionFrameClassificationEnum | None = None
    map_projection_allowed: bool = False
    objects: list[DetectionObject] = Field(default_factory=list)
    is_stale: bool = False
    stale_after_seconds: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 2.0
    has_received_frame: bool = False
    out_of_order_count: int = Field(default=0, ge=0)
    invalid_message_count: int = Field(default=0, ge=0)

    # Evidence-grounded environmental intelligence fields for dashboard inspection (§4, §5, §9)
    tracked_objects: list[dict[str, Any]] = Field(default_factory=list)
    sensor_health: dict[str, dict[str, Any]] = Field(default_factory=dict)
    free_space: dict[str, Any] | None = None
    decision_traces: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("source_timestamp", "received_at", mode="after")
    @classmethod
    def _normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return ensure_utc_datetime(value)


def parse_std_msgs_string_detection_frame(
    raw_message: dict[str, Any] | str,
) -> tuple[DetectionFrame, dict[str, Any]]:
    """Parse and validate a ROS 2 `std_msgs/msg/String` message, ROSBridge publish envelope, or JSON string.

    Supports:
    1. `roslibpy` topic callback dict: `{"data": "<json_string>"}`
    2. Raw ROSBridge v2 JSON publish envelope: `{"op": "publish", "topic": "/detections_json", "msg": {"data": "<json_string>"}}`
       (or a stringified ROSBridge v2 publish envelope)
    3. Direct JSON string: `"{\"stamp\": ...}"`
    """
    if isinstance(raw_message, dict):
        if raw_message.get("op") == "publish" and isinstance(raw_message.get("msg"), dict):
            msg_dict = raw_message["msg"]
        else:
            msg_dict = raw_message

        if "data" not in msg_dict:
            msg = "ROS std_msgs/String message is missing required 'data' field."
            raise ValueError(msg)
        data_field = msg_dict["data"]
        if not isinstance(data_field, str):
            msg = "ROS std_msgs/String 'data' field must be a JSON string."
            raise ValueError(msg)
        raw_json_str = data_field
    elif isinstance(raw_message, str):
        raw_json_str = raw_message
    else:
        msg = f"Unsupported detection message container type: {type(raw_message).__name__}"
        raise ValueError(msg)

    try:
        parsed_obj = json.loads(raw_json_str)
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON inside detection message: {exc}"
        raise ValueError(msg) from exc

    if not isinstance(parsed_obj, dict):
        msg = "Detection JSON root must be a JSON object."
        raise ValueError(msg)

    # If the outer string was a serialized ROSBridge publish envelope `{"op": "publish", "msg": {"data": "..."}}`
    if parsed_obj.get("op") == "publish" and isinstance(parsed_obj.get("msg"), dict):
        inner_msg = parsed_obj["msg"]
        if "data" not in inner_msg or not isinstance(inner_msg["data"], str):
            msg = "ROSBridge publish envelope 'msg' is missing string 'data' field."
            raise ValueError(msg)
        try:
            parsed_obj = json.loads(inner_msg["data"])
        except json.JSONDecodeError as exc:
            msg = f"Invalid JSON inside ROSBridge publish msg.data: {exc}"
            raise ValueError(msg) from exc
        if not isinstance(parsed_obj, dict):
            msg = "Detection JSON root must be a JSON object."
            raise ValueError(msg)

    frame_model = DetectionFrame.model_validate(parsed_obj)
    return frame_model, parsed_obj


__all__ = [
    "CoordinateTransformPort",
    "CoordinateTransformResult",
    "DetectionCoordinateFrameEnum",
    "DetectionFrame",
    "DetectionFrameClassificationEnum",
    "DetectionIdInt",
    "DetectionObject",
    "DetectionPosition3D",
    "DetectionsPushPayload",
    "EpochSecondsFloat",
    "NonNegativeMetres",
    "UnvalidatedCoordinateTransformStub",
    "classify_detection_frame",
    "parse_std_msgs_string_detection_frame",
]
