"""Depth-camera perception ingestion pipeline (`/detections_json` — Section 3).

Responsibilities:
1. Parse and validate `/detections_json` (`std_msgs/String`) payloads containing
   `stamp`, `frame`, and `objects` (`id`, `class`, `score`, `position`, `range_m`).
2. Preserve the original coordinate frame (`oak_rgb_camera_optical_frame`,
   `oak_parent_frame`, `map`, etc.) verbatim without interpreting camera-relative
   coordinates as global map coordinates.
3. Keep sensor timestamp (`stamp`) and local ingestion timestamp (`ingested_at`)
   strictly separate.
4. Safely reject malformed JSON, missing fields, NaN/infinite coordinates, invalid
   scores/ranges, duplicate messages, and out-of-order timestamps while updating
   `SensorHealthReport` counters (`malformed_message_count`, `duplicate_message_count`,
   `out_of_order_count`, `message_frequency_hz`, `is_stale`).
5. Expose a replaceable `DepthImageStreamPort` extension point for a future
   confirmed depth-image stream without claiming raw depth images are currently
   processed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import time
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable
import uuid

from ultron.schemas.commands import Position2D
from ultron.schemas.mission import VehicleID
from ultron.schemas.observations import (
    ObservationValidationStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorSourceModeEnum,
    SensorTypeEnum,
    StructuredObservation,
)
from ultron.schemas.world import SensorHealthReport, SensorHealthStatusEnum


@runtime_checkable
class DepthImageStreamPort(Protocol):
    """Replaceable extension point for a future validated raw depth-image topic (§3).

    Note: Only `/detections_json` (`std_msgs/String`) is currently verified by the
    hardware team. Raw depth images are NOT claimed or processed until a depth-image
    topic and message type are confirmed.
    """

    @property
    def topic(self) -> str: ...

    @property
    def is_verified(self) -> bool: ...


@dataclass
class CameraIngestionOutcome:
    """Result of ingesting a single `/detections_json` message."""

    accepted: bool
    reason: str
    sensor_timestamp: Optional[float] = None
    ingested_at: float = 0.0
    coordinate_frame: Optional[str] = None
    is_stale: bool = False
    is_duplicate: bool = False
    is_out_of_order: bool = False
    observations: List[SensorObservation] = field(default_factory=list)
    sensor_health: Optional[SensorHealthReport] = None


def _extract_stamp(raw_stamp: Any) -> float:
    if isinstance(raw_stamp, dict):
        if "sec" in raw_stamp:
            sec = raw_stamp.get("sec", 0)
            nanosec = raw_stamp.get("nanosec", raw_stamp.get("nsec", 0))
            if isinstance(sec, (int, float)) and isinstance(nanosec, (int, float)):
                val = float(sec) + float(nanosec) / 1e9
                if math.isfinite(val) and val > 0.0:
                    return val
        raise ValueError(f"Invalid ROS stamp dict: {raw_stamp}")
    if isinstance(raw_stamp, (int, float)) and not isinstance(raw_stamp, bool):
        fval = float(raw_stamp)
        if math.isfinite(fval) and fval > 0.0:
            return fval
    raise ValueError(f"Invalid stamp value: {raw_stamp}")


class DepthCameraAdapter:
    """Adapter for the verified `/detections_json` (`std_msgs/String`) perception topic."""

    def __init__(
        self,
        *,
        sensor_id: str = "depth_camera",
        topic: str = "/detections_json",
        message_type: str = "std_msgs/msg/String",
        default_frame: str = "oak_rgb_camera_optical_frame",
        vehicle_id: Optional[str] = None,
        enabled: bool = True,
        source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED,
        stale_after_sec: float = 2.0,
        confidence_threshold: float = 0.0,
    ) -> None:
        self.sensor_id = sensor_id
        self.topic = topic
        self.message_type = message_type
        self.default_frame = default_frame
        self.vehicle_id = vehicle_id if (vehicle_id and vehicle_id.strip()) else None
        self.enabled = enabled
        self.source_mode = source_mode
        self.stale_after_sec = max(0.1, stale_after_sec)
        self.confidence_threshold = max(0.0, min(1.0, confidence_threshold))

        self._connected: bool = enabled
        self._last_stamp: Optional[float] = None
        self._last_ingested_at: Optional[float] = None
        self._last_signature: Optional[str] = None
        self._recent_ingest_times: List[float] = []
        self._valid_count: int = 0
        self._malformed_count: int = 0
        self._duplicate_count: int = 0
        self._out_of_order_count: int = 0
        self._error_state: Optional[str] = None

    def set_connection_state(
        self,
        connected: bool,
        *,
        error_state: Optional[str] = None,
    ) -> SensorHealthReport:
        """Update connection state explicitly; a disconnected sensor never appears healthy."""
        self._connected = connected
        self._error_state = error_state
        return self.get_health()

    def get_health(self, now: Optional[float] = None) -> SensorHealthReport:
        """Return current `SensorHealthReport` for the depth camera."""
        current_time = now if now is not None else time.time()
        is_stale = False
        if self._last_ingested_at is not None:
            if (current_time - self._last_ingested_at) > self.stale_after_sec:
                is_stale = True
        if self._last_stamp is not None and self._last_ingested_at is not None:
            if (self._last_ingested_at - self._last_stamp) > self.stale_after_sec:
                is_stale = True

        if not self.enabled:
            status = SensorHealthStatusEnum.UNAVAILABLE
        elif self._error_state:
            status = SensorHealthStatusEnum.ERROR
        elif not self._connected:
            status = SensorHealthStatusEnum.DISCONNECTED
        elif self._last_ingested_at is None:
            status = SensorHealthStatusEnum.DISCONNECTED
        elif is_stale:
            status = SensorHealthStatusEnum.STALE
        elif self.source_mode == SensorSourceModeEnum.SIMULATED:
            status = SensorHealthStatusEnum.SIMULATED
        else:
            status = SensorHealthStatusEnum.CONNECTED

        freq_hz = 0.0
        cutoff = current_time - 5.0
        recent = [t for t in self._recent_ingest_times if t >= cutoff]
        if len(recent) >= 2:
            span = recent[-1] - recent[0]
            if span > 0.0:
                freq_hz = round((len(recent) - 1) / span, 2)
        elif len(recent) == 1:
            freq_hz = 1.0

        return SensorHealthReport(
            sensor_id=self.sensor_id,
            sensor_type=SensorTypeEnum.DEPTH_CAMERA,
            enabled=self.enabled,
            status=status,
            source_mode=self.source_mode,
            topic=self.topic,
            message_type=self.message_type,
            coordinate_frame=self.default_frame,
            vehicle_id=self.vehicle_id,
            last_message_timestamp=self._last_stamp,
            last_ingested_at=self._last_ingested_at,
            message_frequency_hz=freq_hz,
            is_stale=is_stale,
            stale_after_sec=self.stale_after_sec,
            valid_message_count=self._valid_count,
            malformed_message_count=self._malformed_count,
            duplicate_message_count=self._duplicate_count,
            out_of_order_count=self._out_of_order_count,
            error_state=self._error_state,
            notes="Verified /detections_json object detection stream (oak_rgb_camera_optical_frame). Raw depth images not claimed.",
        )

    def ingest_message(
        self,
        raw_message: Dict[str, Any] | str,
        *,
        ingested_at: Optional[float] = None,
        source_mode: Optional[SensorSourceModeEnum] = None,
        vehicle_id: Optional[str] = None,
    ) -> CameraIngestionOutcome:
        """Validate and convert a `/detections_json` payload into `SensorObservation` records."""
        now = ingested_at if ingested_at is not None else time.time()
        effective_mode = source_mode or self.source_mode
        effective_vehicle = vehicle_id if vehicle_id is not None else self.vehicle_id

        if not self.enabled or not self._connected:
            return CameraIngestionOutcome(
                accepted=False,
                reason="sensor_disconnected_or_disabled",
                ingested_at=now,
                sensor_health=self.get_health(now),
            )

        try:
            payload_dict = self._unwrap_payload(raw_message)
            stamp = _extract_stamp(payload_dict.get("stamp"))
            frame = payload_dict.get("frame")
            if not isinstance(frame, str) or not frame.strip():
                raise ValueError("Missing or empty 'frame' field.")
            frame = frame.strip()

            objects_raw = payload_dict.get("objects")
            if not isinstance(objects_raw, list):
                raise ValueError("'objects' field must be a list.")

            # Canonical signature to detect exact duplicate messages
            canonical_sig = json.dumps(
                {"stamp": stamp, "frame": frame, "objects": objects_raw},
                sort_keys=True,
            )
            if self._last_signature == canonical_sig:
                self._duplicate_count += 1
                return CameraIngestionOutcome(
                    accepted=False,
                    reason="duplicate_message",
                    sensor_timestamp=stamp,
                    ingested_at=now,
                    coordinate_frame=frame,
                    is_duplicate=True,
                    sensor_health=self.get_health(now),
                )

            if self._last_stamp is not None and stamp < self._last_stamp:
                self._out_of_order_count += 1
                return CameraIngestionOutcome(
                    accepted=False,
                    reason="out_of_order_timestamp",
                    sensor_timestamp=stamp,
                    ingested_at=now,
                    coordinate_frame=frame,
                    is_out_of_order=True,
                    sensor_health=self.get_health(now),
                )

            observations: List[SensorObservation] = []
            for idx, item in enumerate(objects_raw):
                if not isinstance(item, dict):
                    raise ValueError(f"Object at index {idx} must be a dict.")
                obj_id = item.get("id")
                if not isinstance(obj_id, int) or isinstance(obj_id, bool) or obj_id < 0:
                    raise ValueError(f"Invalid object 'id' at index {idx}: {obj_id}")

                cls_name = item.get("class")
                if not isinstance(cls_name, str) or not cls_name.strip():
                    raise ValueError(f"Invalid object 'class' at index {idx}.")
                cls_clean = cls_name.strip()[:100]

                score_raw = item.get("score")
                if (
                    not isinstance(score_raw, (int, float))
                    or isinstance(score_raw, bool)
                    or not math.isfinite(float(score_raw))
                    or not (0.0 <= float(score_raw) <= 1.0)
                ):
                    raise ValueError(f"Invalid object 'score' at index {idx}: {score_raw}")
                score = float(score_raw)

                pos_raw = item.get("position")
                if not isinstance(pos_raw, dict):
                    raise ValueError(f"Missing 'position' dict at index {idx}.")
                for axis in ("x", "y", "z"):
                    if axis not in pos_raw or isinstance(pos_raw[axis], bool) or not isinstance(pos_raw[axis], (int, float)):
                        raise ValueError(f"Invalid position.{axis} at index {idx}.")
                px = float(pos_raw["x"])
                py = float(pos_raw["y"])
                pz = float(pos_raw["z"])
                if not (math.isfinite(px) and math.isfinite(py) and math.isfinite(pz)):
                    raise ValueError(f"Non-finite coordinates at index {idx}.")

                range_raw = item.get("range_m")
                if (
                    not isinstance(range_raw, (int, float))
                    or isinstance(range_raw, bool)
                    or not math.isfinite(float(range_raw))
                    or float(range_raw) < 0.0
                ):
                    raise ValueError(f"Invalid 'range_m' at index {idx}: {range_raw}")
                range_m = float(range_raw)

                if score < self.confidence_threshold:
                    continue

                is_hazard = cls_clean.lower() in {"fire", "smoke", "hazard", "gas_leak", "thermal_anomaly"}
                is_obstacle = cls_clean.lower() in {"chair", "bench", "box", "obstacle", "wall", "barrier", "table"}

                obs = SensorObservation(
                    observation_id=f"OBS-CAM-{uuid.uuid4().hex[:8].upper()}",
                    sensor_id=self.sensor_id,
                    sensor_type=SensorTypeEnum.DEPTH_CAMERA,
                    vehicle_id=effective_vehicle,
                    detector_object_id=obj_id,
                    sensor_timestamp=stamp,
                    ingested_at=now,
                    coordinate_frame=frame,
                    position=Position3DMeasurement(
                        x=px,
                        y=py,
                        z=pz,
                        range_m=range_m,
                        uncertainty_m=round(max(0.05, 0.08 * range_m), 3),
                    ),
                    classification=cls_clean,
                    confidence=score,
                    raw_reference={
                        "topic": self.topic,
                        "frame": frame,
                        "stamp": stamp,
                        "detector_object_id": obj_id,
                        "id_is_frame_local_only": True,
                        "raw_object": item,
                    },
                    validation_status=ObservationValidationStatusEnum.VALIDATED,
                    source_mode=effective_mode,
                    is_obstacle=is_obstacle,
                    is_hazard=is_hazard,
                )
                observations.append(obs)

            is_stale = (now - stamp) > self.stale_after_sec
            self._last_stamp = stamp
            self._last_ingested_at = now
            self._last_signature = canonical_sig
            self._default_frame_update(frame)
            self._valid_count += 1
            self._recent_ingest_times.append(now)
            if len(self._recent_ingest_times) > 50:
                self._recent_ingest_times = self._recent_ingest_times[-50:]

            return CameraIngestionOutcome(
                accepted=True,
                reason="accepted",
                sensor_timestamp=stamp,
                ingested_at=now,
                coordinate_frame=frame,
                is_stale=is_stale,
                observations=observations,
                sensor_health=self.get_health(now),
            )

        except Exception as exc:
            self._malformed_count += 1
            return CameraIngestionOutcome(
                accepted=False,
                reason=f"malformed_observation: {exc}",
                ingested_at=now,
                sensor_health=self.get_health(now),
            )

    def _default_frame_update(self, frame: str) -> None:
        self.default_frame = frame

    @staticmethod
    def _unwrap_payload(raw_message: Dict[str, Any] | str) -> Dict[str, Any]:
        msg_dict: Dict[str, Any]
        if isinstance(raw_message, str):
            parsed = json.loads(raw_message)
            if not isinstance(parsed, dict):
                raise ValueError("JSON root must be an object.")
            msg_dict = parsed
        elif isinstance(raw_message, dict):
            msg_dict = raw_message
        else:
            raise ValueError("Message must be a dict or JSON string.")

        if msg_dict.get("op") == "publish" and isinstance(msg_dict.get("msg"), dict):
            msg_dict = msg_dict["msg"]

        if "data" in msg_dict and "stamp" not in msg_dict:
            data_str = msg_dict["data"]
            if not isinstance(data_str, str):
                raise ValueError("std_msgs/String 'data' field must be a string.")
            inner = json.loads(data_str)
            if not isinstance(inner, dict):
                raise ValueError("Inner JSON payload must be an object.")
            if inner.get("op") == "publish" and isinstance(inner.get("msg"), dict):
                inner_msg = inner["msg"]
                if "data" in inner_msg and isinstance(inner_msg["data"], str):
                    inner2 = json.loads(inner_msg["data"])
                    if not isinstance(inner2, dict):
                        raise ValueError("Inner JSON payload must be an object.")
                    return inner2
            return inner

        return msg_dict


class VisionPipeline:
    """Laptop-side Vision Pipeline (TRD §13) with full `DepthCameraAdapter` backing."""

    def __init__(self, confidence_threshold: float = 0.5) -> None:
        self.confidence_threshold = confidence_threshold
        self.camera_adapter = DepthCameraAdapter(
            confidence_threshold=confidence_threshold,
            source_mode=SensorSourceModeEnum.SIMULATED,
        )

    def process_detection(
        self,
        source_vehicle: VehicleID,
        label: str,
        confidence: float,
        location: Position2D,
        event_type: str = "target_detected",
        *,
        coordinate_frame: str = "map",
        position_z: float = 0.0,
        range_m: Optional[float] = None,
        detector_object_id: Optional[int] = None,
        source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED,
        timestamp: Optional[float] = None,
    ) -> Optional[StructuredObservation]:
        """Filters by confidence and packages detection into a `StructuredObservation`."""
        if confidence < self.confidence_threshold:
            return None

        sanitized_label = str(label).strip()[:100]
        now = timestamp if timestamp is not None else time.time()

        return StructuredObservation(
            observation_id=f"OBS-VIS-{uuid.uuid4().hex[:8].upper()}",
            event=event_type,
            source_vehicle=source_vehicle,
            confidence=round(confidence, 4),
            location=location,
            position_z=position_z,
            range_m=range_m,
            coordinate_frame=coordinate_frame,
            detector_object_id=detector_object_id,
            sensor_id="depth_camera",
            sensor_type=SensorTypeEnum.DEPTH_CAMERA,
            source_mode=source_mode,
            visual_evidence=sanitized_label,
            timestamp=now,
            ingested_at=time.time(),
        )
