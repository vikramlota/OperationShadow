"""Configurable LiDAR ingestion interface (`ultron/vision/lidar.py` — Section 3).

Architectural & Safety Guarantees:
1. Because the hardware team has NOT yet confirmed the LiDAR topic name, message
   type, coordinate frame, publishing frequency, or extrinsics:
   - Topic (`""`), message type (`""`), and frame (`""`) are unconfigured by default.
   - The adapter is disabled by default (`enabled=False`).
   - Unavailable LiDAR is explicitly reported as `SensorHealthStatusEnum.UNAVAILABLE`,
     NEVER as an empty obstacle map or healthy sensor.
   - In physical mode (`SensorSourceModeEnum.PHYSICAL`), synthetic or unconfigured
     observations are strictly rejected.
2. When explicitly configured and enabled (for simulated testing or once hardware
   parameters are supplied), validates incoming measurements (LaserScan ranges or
   PointCloud2 / obstacle clusters), rejects invalid/non-finite/out-of-bounds ranges,
   and emits standardized `SensorObservation` records (`SensorTypeEnum.LIDAR`).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import math
import struct
import time
from typing import Any, Dict, List, Optional
import uuid

from ultron.schemas.observations import (
    ObservationValidationStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorSourceModeEnum,
    SensorTypeEnum,
)
from ultron.schemas.world import SensorHealthReport, SensorHealthStatusEnum


def parse_pointcloud2_to_lidar_payload(
    raw_message: Dict[str, Any],
    *,
    default_frame: str = "unilidar_lidar",
    min_range_m: float = 0.05,
    max_range_m: float = 30.0,
) -> Dict[str, Any]:
    """Convert a ROS 2 `sensor_msgs/msg/PointCloud2` dict (e.g. `/unilidar/cloud`) into a normalized LiDAR scan dict."""
    msg = (
        raw_message["msg"]
        if raw_message.get("op") == "publish" and isinstance(raw_message.get("msg"), dict)
        else raw_message
    )
    header = msg.get("header") if isinstance(msg.get("header"), dict) else {}
    stamp = header.get("stamp", msg.get("stamp"))
    frame = str(header.get("frame_id") or msg.get("frame") or default_frame or "unilidar_lidar").strip()

    width = int(msg.get("width", 0) or 0)
    height = int(msg.get("height", 1) or 1)
    point_step = int(msg.get("point_step", 0) or 0)
    row_step = int(msg.get("row_step", width * point_step) or (width * point_step))
    is_bigendian = bool(msg.get("is_bigendian", False))
    fields_raw = msg.get("fields", [])
    data_raw = msg.get("data", b"")

    if isinstance(data_raw, str):
        raw_bytes = base64.b64decode(data_raw) if data_raw else b""
    elif isinstance(data_raw, (bytes, bytearray)):
        raw_bytes = bytes(data_raw)
    elif isinstance(data_raw, list):
        raw_bytes = bytes(int(b) & 0xFF for b in data_raw)
    else:
        raise ValueError(f"Unsupported PointCloud2 data type: {type(data_raw).__name__}")

    total_points = width * height
    if total_points <= 0 or point_step <= 0 or len(raw_bytes) < point_step:
        return {
            "stamp": stamp,
            "frame": frame,
            "obstacles": [],
            "free_space_radius_m": round(max_range_m, 3),
        }

    field_offsets: Dict[str, tuple[int, int]] = {}
    if isinstance(fields_raw, list):
        for f in fields_raw:
            if isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("offset"), int):
                field_offsets[f["name"]] = (int(f["offset"]), int(f.get("datatype", 7)))

    x_off, x_dt = field_offsets.get("x", (0, 7))
    y_off, y_dt = field_offsets.get("y", (4, 7))
    z_off, z_dt = field_offsets.get("z", (8, 7))
    endian = ">" if is_bigendian else "<"
    fmt_map = {7: "f", 8: "d"}
    if x_dt not in fmt_map or y_dt not in fmt_map or z_dt not in fmt_map:
        raise ValueError("PointCloud2 x/y/z fields must be FLOAT32 (7) or FLOAT64 (8).")

    x_fmt = endian + fmt_map[x_dt]
    y_fmt = endian + fmt_map[y_dt]
    z_fmt = endian + fmt_map[z_dt]

    max_samples = 600
    stride = max(1, total_points // max_samples)
    sector_closest: Dict[int, tuple[float, float, float, float]] = {}
    min_any_range: Optional[float] = None
    buf_len = len(raw_bytes)

    def _angle_to_sector_name(angle_rad: float) -> str:
        if -math.pi / 6.0 <= angle_rad <= math.pi / 6.0:
            return "front"
        if math.pi / 6.0 < angle_rad <= math.pi / 2.0:
            return "front_left"
        if -math.pi / 2.0 <= angle_rad < -math.pi / 6.0:
            return "front_right"
        if math.pi / 2.0 < angle_rad <= (5.0 * math.pi / 6.0):
            return "left"
        if -(5.0 * math.pi / 6.0) <= angle_rad < -math.pi / 2.0:
            return "right"
        return "rear"

    idx = 0
    for r_idx in range(height):
        row_start = r_idx * row_step
        for c_idx in range(width):
            if idx % stride == 0:
                pt_start = row_start + c_idx * point_step
                if pt_start + max(x_off + 4, y_off + 4, z_off + 4) <= buf_len:
                    x = float(struct.unpack_from(x_fmt, raw_bytes, pt_start + x_off)[0])
                    y = float(struct.unpack_from(y_fmt, raw_bytes, pt_start + y_off)[0])
                    z = float(struct.unpack_from(z_fmt, raw_bytes, pt_start + z_off)[0])
                    if math.isfinite(x) and math.isfinite(y) and math.isfinite(z):
                        # Filter out floor/ceiling returns outside rover vertical envelope
                        if -0.18 <= z <= 1.6:
                            rng = math.sqrt(x * x + y * y + z * z)
                            if min_range_m <= rng <= max_range_m:
                                if min_any_range is None or rng < min_any_range:
                                    min_any_range = rng
                                angle = math.atan2(y, x)
                                sector = int(((angle + math.pi) / (2.0 * math.pi)) * 8.0) % 8
                                prev = sector_closest.get(sector)
                                if prev is None or rng < prev[3]:
                                    sector_closest[sector] = (x, y, z, rng)
            idx += 1

    obstacles: List[Dict[str, Any]] = []
    for sec_idx, (ox, oy, oz, orng) in sorted(sector_closest.items()):
        sec_angle = math.atan2(oy, ox)
        sec_name = _angle_to_sector_name(sec_angle)
        obstacles.append(
            {
                "id": sec_idx,
                "classification": "lidar_obstacle",
                "confidence": 0.9,
                "sector": sec_name,
                "position": {
                    "x": round(ox, 4),
                    "y": round(oy, 4),
                    "z": round(oz, 4),
                },
                "range_m": round(orng, 4),
                "uncertainty_m": 0.05,
            }
        )

    free_space = (
        round(max(0.0, min_any_range - 0.2), 3)
        if min_any_range is not None
        else round(max_range_m, 3)
    )
    if stamp is None or (isinstance(stamp, dict) and not stamp.get("sec") and not stamp.get("secs")):
        stamp = time.time()
    return {
        "stamp": stamp,
        "frame": frame,
        "obstacles": obstacles,
        "free_space_radius_m": free_space,
    }


@dataclass
class LidarIngestionOutcome:
    """Result of ingesting a LiDAR scan or obstacle measurement message."""

    accepted: bool
    reason: str
    sensor_timestamp: Optional[float] = None
    ingested_at: float = 0.0
    coordinate_frame: Optional[str] = None
    is_stale: bool = False
    observations: List[SensorObservation] = field(default_factory=list)
    free_space_radius_m: Optional[float] = None
    sensor_health: Optional[SensorHealthReport] = None


class LidarIngestionAdapter:
    """Configurable LiDAR adapter exposing a consistent internal observation interface."""

    def __init__(
        self,
        *,
        sensor_id: str = "lidar",
        enabled: bool = False,
        topic: str = "",
        message_type: str = "",
        coordinate_frame: str = "",
        vehicle_id: Optional[str] = None,
        source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED,
        min_range_m: float = 0.05,
        max_range_m: float = 30.0,
        stale_after_sec: float = 2.0,
    ) -> None:
        self.sensor_id = sensor_id
        self.enabled = enabled
        self.topic = topic.strip()
        self.message_type = message_type.strip()
        self.coordinate_frame = coordinate_frame.strip()
        self.vehicle_id = vehicle_id if (vehicle_id and vehicle_id.strip()) else None
        self.source_mode = source_mode
        self.min_range_m = min_range_m
        self.max_range_m = max_range_m
        self.stale_after_sec = max(0.1, stale_after_sec)

        self._connected: bool = False
        self._last_stamp: Optional[float] = None
        self._last_ingested_at: Optional[float] = None
        self._recent_ingest_times: List[float] = []
        self._valid_count: int = 0
        self._malformed_count: int = 0
        self._duplicate_count: int = 0
        self._out_of_order_count: int = 0
        self._error_state: Optional[str] = None
        self._last_signature: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        """Return True only when topic, message_type, and coordinate_frame are explicitly provided."""
        return bool(
            self.topic
            and self.topic.startswith("/")
            and self.message_type
            and self.coordinate_frame
        )

    def configure(
        self,
        *,
        enabled: bool,
        topic: str,
        message_type: str,
        coordinate_frame: str,
        vehicle_id: Optional[str] = None,
        source_mode: Optional[SensorSourceModeEnum] = None,
        connected: bool = True,
    ) -> SensorHealthReport:
        """Configure LiDAR parameters dynamically without restructuring the perception pipeline."""
        self.enabled = enabled
        self.topic = topic.strip()
        self.message_type = message_type.strip()
        self.coordinate_frame = coordinate_frame.strip()
        if vehicle_id is not None:
            self.vehicle_id = vehicle_id.strip() or None
        if source_mode is not None:
            self.source_mode = source_mode
        self._connected = connected and self.enabled and self.is_configured
        return self.get_health()

    def set_connection_state(
        self,
        connected: bool,
        *,
        error_state: Optional[str] = None,
    ) -> SensorHealthReport:
        self._connected = connected
        self._error_state = error_state
        return self.get_health()

    def get_health(self, now: Optional[float] = None) -> SensorHealthReport:
        """Return current `SensorHealthReport` for LiDAR.

        Unavailable or unconfigured LiDAR is explicitly reported as `UNAVAILABLE`.
        """
        current_time = now if now is not None else time.time()
        is_stale = False
        if self._last_ingested_at is not None:
            if (current_time - self._last_ingested_at) > self.stale_after_sec:
                is_stale = True
        if self._last_stamp is not None and self._last_ingested_at is not None:
            if (self._last_ingested_at - self._last_stamp) > self.stale_after_sec:
                is_stale = True

        if not self.enabled or not self.is_configured:
            status = SensorHealthStatusEnum.UNAVAILABLE
        elif self._error_state:
            status = SensorHealthStatusEnum.ERROR
        elif not self._connected or self._last_ingested_at is None:
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

        notes = (
            "LiDAR topic, message type, frame, and extrinsics are unconfirmed by hardware team; marked UNAVAILABLE."
            if status == SensorHealthStatusEnum.UNAVAILABLE
            else f"LiDAR adapter active on {self.topic} ({self.message_type}) in frame '{self.coordinate_frame}'."
        )

        return SensorHealthReport(
            sensor_id=self.sensor_id,
            sensor_type=SensorTypeEnum.LIDAR,
            enabled=self.enabled and self.is_configured,
            status=status,
            source_mode=self.source_mode,
            topic=self.topic,
            message_type=self.message_type,
            coordinate_frame=self.coordinate_frame,
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
            notes=notes,
        )

    def ingest_scan(
        self,
        payload: Dict[str, Any],
        *,
        ingested_at: Optional[float] = None,
        source_mode: Optional[SensorSourceModeEnum] = None,
    ) -> LidarIngestionOutcome:
        """Validate and ingest a LiDAR scan or obstacle cluster payload."""
        now = ingested_at if ingested_at is not None else time.time()
        effective_mode = source_mode or self.source_mode

        if not self.enabled or not self.is_configured:
            return LidarIngestionOutcome(
                accepted=False,
                reason="lidar_disabled_or_unconfigured",
                ingested_at=now,
                sensor_health=self.get_health(now),
            )

        if not self._connected:
            return LidarIngestionOutcome(
                accepted=False,
                reason="lidar_disconnected",
                ingested_at=now,
                sensor_health=self.get_health(now),
            )

        try:
            if not isinstance(payload, dict):
                raise ValueError("LiDAR payload must be a dictionary.")

            if "obstacles" not in payload and (
                "point_step" in payload
                or "fields" in payload
                or (
                    payload.get("op") == "publish"
                    and isinstance(payload.get("msg"), dict)
                )
            ):
                payload = parse_pointcloud2_to_lidar_payload(
                    payload,
                    default_frame=self.coordinate_frame or "unilidar_lidar",
                    min_range_m=self.min_range_m,
                    max_range_m=self.max_range_m,
                )

            stamp_raw = payload.get("stamp")
            if isinstance(stamp_raw, dict) and ("sec" in stamp_raw or "secs" in stamp_raw):
                sec_v = stamp_raw.get("sec", stamp_raw.get("secs", 0))
                nsec_v = stamp_raw.get("nanosec", stamp_raw.get("nsecs", 0))
                stamp = float(sec_v) + float(nsec_v) / 1e9
            elif isinstance(stamp_raw, (int, float)) and not isinstance(stamp_raw, bool):
                stamp = float(stamp_raw)
            else:
                raise ValueError(f"Invalid LiDAR stamp: {stamp_raw}")

            if not math.isfinite(stamp) or stamp <= 0.0:
                raise ValueError("LiDAR stamp must be a finite positive number.")

            frame = str(payload.get("frame") or self.coordinate_frame).strip()
            if not frame:
                raise ValueError("LiDAR frame cannot be empty.")

            if self._last_stamp is not None and stamp < self._last_stamp:
                self._out_of_order_count += 1
                return LidarIngestionOutcome(
                    accepted=False,
                    reason="out_of_order_timestamp",
                    sensor_timestamp=stamp,
                    ingested_at=now,
                    coordinate_frame=frame,
                    sensor_health=self.get_health(now),
                )

            obstacles_raw = payload.get("obstacles", [])
            if not isinstance(obstacles_raw, list):
                raise ValueError("'obstacles' must be a list.")

            observations: List[SensorObservation] = []
            min_detected_range: Optional[float] = None

            for idx, item in enumerate(obstacles_raw):
                if not isinstance(item, dict):
                    raise ValueError(f"LiDAR obstacle at index {idx} must be a dict.")
                pos = item.get("position")
                if not isinstance(pos, dict):
                    raise ValueError(f"LiDAR obstacle at index {idx} missing 'position'.")
                x = float(pos.get("x", math.nan))
                y = float(pos.get("y", math.nan))
                z = float(pos.get("z", 0.0))
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    raise ValueError(f"Non-finite LiDAR obstacle coordinates at index {idx}.")

                range_raw = item.get("range_m")
                if range_raw is not None:
                    if isinstance(range_raw, bool) or not isinstance(range_raw, (int, float)):
                        raise ValueError(f"Invalid LiDAR range_m at index {idx}.")
                    r = float(range_raw)
                else:
                    r = math.sqrt(x * x + y * y + z * z)

                if not math.isfinite(r) or r < self.min_range_m or r > self.max_range_m:
                    raise ValueError(
                        f"LiDAR range {r}m at index {idx} outside valid bounds "
                        f"[{self.min_range_m}, {self.max_range_m}]m."
                    )

                if min_detected_range is None or r < min_detected_range:
                    min_detected_range = r

                sector_str = str(item.get("sector") or "").strip().lower()
                if not sector_str:
                    ang = math.atan2(y, x)
                    if -math.pi / 6.0 <= ang <= math.pi / 6.0:
                        sector_str = "front"
                    elif math.pi / 6.0 < ang <= math.pi / 2.0:
                        sector_str = "front_left"
                    elif -math.pi / 2.0 <= ang < -math.pi / 6.0:
                        sector_str = "front_right"
                    elif math.pi / 2.0 < ang <= (5.0 * math.pi / 6.0):
                        sector_str = "left"
                    elif -(5.0 * math.pi / 6.0) <= ang < -math.pi / 2.0:
                        sector_str = "right"
                    else:
                        sector_str = "rear"

                obs = SensorObservation(
                    observation_id=str(
                        item.get("observation_id")
                        or f"OBS-LDR-{uuid.uuid4().hex[:8].upper()}"
                    ),
                    sensor_id=self.sensor_id,
                    sensor_type=SensorTypeEnum.LIDAR,
                    vehicle_id=self.vehicle_id,
                    detector_object_id=item.get("id") if isinstance(item.get("id"), int) else idx,
                    sensor_timestamp=stamp,
                    ingested_at=now,
                    coordinate_frame=frame,
                    position=Position3DMeasurement(
                        x=x,
                        y=y,
                        z=z,
                        range_m=round(r, 4),
                        uncertainty_m=float(item.get("uncertainty_m", 0.05)),
                    ),
                    classification=str(item.get("classification", "obstacle")),
                    confidence=float(item["confidence"]) if "confidence" in item and item["confidence"] is not None else None,
                    raw_reference={
                        "topic": self.topic,
                        "message_type": self.message_type,
                        "frame": frame,
                        "stamp": stamp,
                        "sector": sector_str,
                        "raw_obstacle": item,
                    },
                    validation_status=ObservationValidationStatusEnum.VALIDATED,
                    source_mode=effective_mode,
                    is_obstacle=bool(item.get("is_obstacle", sector_str != "rear")),
                    is_hazard=False,
                )
                observations.append(obs)

            free_space_raw = payload.get("free_space_radius_m")
            if free_space_raw is not None:
                free_space_m = float(free_space_raw)
                if not math.isfinite(free_space_m) or free_space_m < 0.0:
                    raise ValueError("Invalid free_space_radius_m.")
            elif min_detected_range is not None:
                free_space_m = round(max(0.0, min_detected_range - 0.2), 3)
            else:
                free_space_m = round(self.max_range_m, 3)

            is_stale = (
                False
                if effective_mode == SensorSourceModeEnum.PHYSICAL
                else (now - stamp) > self.stale_after_sec
            )
            self._last_stamp = stamp
            self._last_ingested_at = now
            self._valid_count += 1
            self._recent_ingest_times.append(now)
            if len(self._recent_ingest_times) > 50:
                self._recent_ingest_times = self._recent_ingest_times[-50:]

            return LidarIngestionOutcome(
                accepted=True,
                reason="accepted",
                sensor_timestamp=stamp,
                ingested_at=now,
                coordinate_frame=frame,
                is_stale=is_stale,
                observations=observations,
                free_space_radius_m=free_space_m,
                sensor_health=self.get_health(now),
            )

        except Exception as exc:
            self._malformed_count += 1
            return LidarIngestionOutcome(
                accepted=False,
                reason=f"invalid_lidar_measurement: {exc}",
                ingested_at=now,
                sensor_health=self.get_health(now),
            )
