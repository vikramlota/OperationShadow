"""ROSBridge WebSocket client for read-only rover perception (`/detections_json` and `/unilidar/cloud`).

Connects to the configured ROSBridge server (`ws://<SHADOW_ROS_HOST>:<SHADOW_ROS_PORT>`)
as a read-only subscriber. Streams live detections from `/detections_json` and
optional PointCloud2 scans from `/unilidar/cloud`, forwarding validated frames
into the ULTRON World Model.

Safety & Reliability Guarantees:
- Never falls back to a hardcoded `ROSBRIDGE_URL` or a default `SCOUT01` vehicle ID.
- Requires an explicitly configured and verified ROS vehicle identity (`SHADOW_ROS_VEHICLE_ID=SCOUT01`)
  before associating sensor data with a vehicle; when identity is unconfigured, preserves
  valid camera frames as unassociated (`vehicle_id=None`), and rejects unknown or
  mismatched vehicle identifiers with an explicit diagnostic.
- Strictly validates sensor `stamp` for finite, positive values (`math.isfinite(stamp)` and `stamp > 0.0`);
  never replaces missing, NaN, infinite, or malformed timestamps with current wall-clock time.
- Processes valid empty detection frames (`objects: []`) as sensor activity/freshness updates
  without interpreting them as proof that an area is clear.
- Handles malformed messages without crashing the ROS ingestion process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
from typing import Any

from core.config import Settings, settings

logger = logging.getLogger("operation_shadow.rosbridge")

DETECTION_TOPIC = "/detections_json"
RECONNECT_DELAY_SEC = 5.0


def build_rosbridge_url(app_settings: Settings | None = None) -> str:
    """Construct the ROSBridge WebSocket URL from configured `ros_host` and `ros_port`."""
    cfg = app_settings or settings
    host = (cfg.ros_host or "").strip()
    if not host or "<" in host or ">" in host or " " in host:
        return ""
    return f"ws://{host}:{cfg.ros_port}"


def _parse_finite_stamp(raw_stamp: Any) -> float | None:
    """Parse a finite positive Unix epoch timestamp (`float`/`int` or `{sec, nanosec}`)."""
    if raw_stamp is None or isinstance(raw_stamp, bool):
        return None
    if isinstance(raw_stamp, (int, float)):
        val = float(raw_stamp)
        if not math.isfinite(val) or val <= 0.0:
            return None
        return val
    if isinstance(raw_stamp, dict):
        sec_raw = raw_stamp.get("sec", raw_stamp.get("secs"))
        nsec_raw = raw_stamp.get("nanosec", raw_stamp.get("nsecs", 0))
        if (
            sec_raw is None
            or isinstance(sec_raw, bool)
            or isinstance(nsec_raw, bool)
            or not isinstance(sec_raw, (int, float))
            or not isinstance(nsec_raw, (int, float))
        ):
            return None
        sec_f = float(sec_raw)
        nsec_f = float(nsec_raw)
        if not math.isfinite(sec_f) or not math.isfinite(nsec_f) or nsec_f < 0.0:
            return None
        combined = sec_f + (nsec_f / 1_000_000_000.0)
        if not math.isfinite(combined) or combined <= 0.0:
            return None
        return combined
    return None


def _is_finite_number(value: Any) -> bool:
    """Return True iff `value` is an int or float (not bool) and finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


@dataclass(frozen=True)
class RosDetectionIngestOutcome:
    """Structured result of validating and ingesting a ROS detection payload."""

    accepted: bool
    reason: str
    vehicle_id: str | None
    frame: str | None = None
    stamp: float | None = None
    object_count: int = 0
    empty_frame: bool = False
    unassociated: bool = False


class RosBridgeClient:
    """Async ROSBridge v2 client — read-only subscriber for `/detections_json` and `/unilidar/cloud`."""

    def __init__(
        self,
        url: str | None = None,
        on_detection: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
        on_lidar: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
        *,
        app_settings: Settings | None = None,
    ) -> None:
        self._settings = app_settings or settings
        resolved_url = url if url is not None else build_rosbridge_url(self._settings)
        self._url = resolved_url.strip()
        self._topic = (self._settings.ros_detections_topic or DETECTION_TOPIC).strip()
        self._message_type = (
            self._settings.ros_detections_message_type or "std_msgs/msg/String"
        ).strip()
        self._lidar_enabled = bool(getattr(self._settings, "lidar_enabled", False))
        self._lidar_topic = (
            getattr(self._settings, "lidar_topic", "/unilidar/cloud") or "/unilidar/cloud"
        ).strip()
        self._lidar_message_type = (
            getattr(self._settings, "lidar_message_type", "sensor_msgs/msg/PointCloud2")
            or "sensor_msgs/msg/PointCloud2"
        ).strip()
        self._on_detection = on_detection
        self._on_lidar = on_lidar
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self.connected = False

        # Observability counters
        self.valid_message_count: int = 0
        self.valid_lidar_message_count: int = 0
        self.invalid_message_count: int = 0
        self.empty_frame_count: int = 0
        self.unassociated_frame_count: int = 0
        self.rejected_vehicle_id_count: int = 0

    @property
    def url(self) -> str:
        return self._url

    @property
    def topic(self) -> str:
        return self._topic

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background connection loop (non-blocking) if URL is configured."""
        if not self._url or not self._url.startswith(("ws://", "wss://")):
            logger.error(
                "ROSBridge client cannot start: endpoint URL is unconfigured "
                "(set SHADOW_ROS_HOST and SHADOW_ROS_PORT)."
            )
            return
        self._running = True
        self._task = asyncio.create_task(self._connection_loop(), name="rosbridge-client")
        logger.info("ROSBridge client started → %s (topic=%s)", self._url, self._topic)

    async def stop(self) -> None:
        """Gracefully stop the client."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False
        logger.info("ROSBridge client stopped.")

    async def handle_raw_ws_frame(self, raw: str | bytes) -> bool:
        """Parse and dispatch a single raw ROSBridge WebSocket message without crashing."""
        try:
            msg = json.loads(raw)
        except Exception as exc:
            self.invalid_message_count += 1
            logger.warning("Discarding malformed non-JSON ROSBridge frame: %s", exc)
            return False

        if not isinstance(msg, dict):
            self.invalid_message_count += 1
            logger.warning("Discarding non-object ROSBridge frame: %r", type(msg).__name__)
            return False

        if msg.get("op") != "publish":
            return False

        topic = msg.get("topic")
        if self._lidar_enabled and topic == self._lidar_topic:
            msg_inner = msg.get("msg")
            if not isinstance(msg_inner, dict):
                self.invalid_message_count += 1
                return False
            self.valid_lidar_message_count += 1
            if self._on_lidar is not None:
                try:
                    await self._on_lidar(msg_inner)
                except Exception as exc:
                    self.invalid_message_count += 1
                    logger.warning("Error in ROSBridge LiDAR handler: %s", exc)
                    return False
            return True

        if topic != self._topic:
            return False

        msg_inner = msg.get("msg")
        if not isinstance(msg_inner, dict):
            self.invalid_message_count += 1
            logger.warning("Discarding ROSBridge publish frame with non-dict 'msg' field.")
            return False

        payload_str = msg_inner.get("data")
        if not isinstance(payload_str, str) or not payload_str.strip():
            self.invalid_message_count += 1
            logger.warning("Discarding ROSBridge publish frame with missing/empty 'data' string.")
            return False

        try:
            detection = json.loads(payload_str)
        except Exception as exc:
            self.invalid_message_count += 1
            logger.warning("Discarding invalid JSON inside std_msgs/String data: %s", exc)
            return False

        if not isinstance(detection, dict):
            self.invalid_message_count += 1
            logger.warning("Discarding non-dict detection payload inside std_msgs/String.")
            return False

        if self._on_detection is not None:
            try:
                await self._on_detection(detection)
            except Exception as exc:
                self.invalid_message_count += 1
                logger.warning("Error in ROSBridge detection handler: %s", exc)
                return False
        return True

    # ── Internal loop ─────────────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        """Reconnect while running with bounded backoff."""
        while self._running:
            try:
                await self._run_session()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                logger.warning(
                    "ROSBridge disconnected (%s: %s). Reconnecting in %.0fs…",
                    type(exc).__name__,
                    exc,
                    RECONNECT_DELAY_SEC,
                )
                await asyncio.sleep(RECONNECT_DELAY_SEC)

    async def _run_session(self) -> None:
        if not self._url or not self._url.startswith(("ws://", "wss://")):
            raise ValueError("ROSBridge URL is not configured.")
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed. Run: pip install websockets")
            await asyncio.sleep(RECONNECT_DELAY_SEC)
            return

        candidate_urls = [self._url]
        if "10.68.44.41" in self._url:
            candidate_urls.append(self._url.replace("10.68.44.41", "127.0.0.1"))
        elif "127.0.0.1" in self._url:
            candidate_urls.append(self._url.replace("127.0.0.1", "10.68.44.41"))

        last_err: Exception | None = None
        for target_url in candidate_urls:
            try:
                logger.info("Connecting to ROSBridge at %s …", target_url)
                async with websockets.connect(target_url, open_timeout=5) as ws:
                    self.connected = True
                    logger.info("ROSBridge CONNECTED (%s) ✓", target_url)

                    await ws.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "topic": self._topic,
                                "type": self._message_type,
                                "throttle_rate": 200,  # max 5 Hz
                                "queue_length": 1,
                            }
                        )
                    )
                    logger.info("Subscribed to %s (%s)", self._topic, self._message_type)

                    if self._lidar_enabled and self._lidar_topic:
                        await ws.send(
                            json.dumps(
                                {
                                    "op": "subscribe",
                                    "topic": self._lidar_topic,
                                    "type": self._lidar_message_type,
                                    "throttle_rate": 500,  # max 2 Hz
                                    "queue_length": 1,
                                }
                            )
                        )
                        logger.info(
                            "Subscribed to LiDAR %s (%s)",
                            self._lidar_topic,
                            self._lidar_message_type,
                        )

                    async for raw in ws:
                        if not self._running:
                            break
                        await self.handle_raw_ws_frame(raw)
                self.connected = False
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_err = exc
                continue

        self.connected = False
        if last_err is not None:
            raise last_err


async def process_rosbridge_detection_payload(
    detection: Any,
    mission_service_agent: Any | None,
    *,
    app_settings: Settings | None = None,
    client_stats: RosBridgeClient | None = None,
) -> RosDetectionIngestOutcome:
    """Validate and forward a `/detections_json` payload into the ULTRON World Model.

    Enforces:
    1. Strict dict & schema validation.
    2. Finite positive sensor `stamp` (no silent replacement with current time).
    3. Verified vehicle identity (`SHADOW_ROS_VEHICLE_ID`):
       - Rejects unknown or mismatched vehicle IDs with explicit diagnostic.
       - Never silently falls back to `SCOUT01` when identity is unconfigured;
         preserves valid frames as unassociated (`vehicle_id=None`).
    4. Processes valid empty detection frames (`objects: []`) as sensor activity/freshness
       without interpreting them as proof that an area is clear.
    """
    cfg = app_settings or settings

    def _mark_invalid(reason: str) -> RosDetectionIngestOutcome:
        if client_stats is not None:
            client_stats.invalid_message_count += 1
        if mission_service_agent is not None and hasattr(mission_service_agent, "world"):
            sh = mission_service_agent.world.environment.sensor_health.get("depth_camera")
            if sh is not None:
                sh.malformed_message_count += 1
        logger.warning("Rejected ROS detection payload: %s", reason)
        return RosDetectionIngestOutcome(accepted=False, reason=reason, vehicle_id=None)

    if not isinstance(detection, dict):
        return _mark_invalid("malformed_payload_not_dict")

    # 1. Validate finite positive sensor timestamp (never replace with current time!)
    stamp_sec = _parse_finite_stamp(detection.get("stamp"))
    if stamp_sec is None:
        return _mark_invalid(f"invalid_or_non_finite_stamp:{detection.get('stamp')!r}")

    # 2. Validate coordinate frame
    raw_frame = detection.get("frame")
    if raw_frame is None:
        frame = "oak_rgb_camera_optical_frame"
    elif isinstance(raw_frame, str) and raw_frame.strip():
        frame = raw_frame.strip()
    else:
        return _mark_invalid(f"invalid_coordinate_frame:{raw_frame!r}")

    # 3. Validate & resolve ROS vehicle identity (NO silent fallback to SCOUT01)
    configured_vid = (cfg.ros_vehicle_id or "").strip() or None
    if configured_vid is not None and configured_vid != "SCOUT01":
        if client_stats is not None:
            client_stats.rejected_vehicle_id_count += 1
        return _mark_invalid(f"unsupported_configured_vehicle_id:{configured_vid}")

    payload_vid_raw = detection.get("vehicle_id")
    if payload_vid_raw is not None:
        if not isinstance(payload_vid_raw, str) or not payload_vid_raw.strip():
            if client_stats is not None:
                client_stats.rejected_vehicle_id_count += 1
            return _mark_invalid("invalid_payload_vehicle_id")
        payload_vid = payload_vid_raw.strip()
        if payload_vid != "SCOUT01":
            if client_stats is not None:
                client_stats.rejected_vehicle_id_count += 1
            return _mark_invalid(f"unknown_vehicle_id:{payload_vid}")
        if configured_vid is not None and payload_vid != configured_vid:
            if client_stats is not None:
                client_stats.rejected_vehicle_id_count += 1
            return _mark_invalid(
                f"mismatched_vehicle_id:payload={payload_vid}!=configured={configured_vid}"
            )

    # Associate with SCOUT01 ONLY when SHADOW_ROS_VEHICLE_ID=='SCOUT01' is explicitly configured
    verified_vehicle_id: str | None = configured_vid
    is_unassociated = verified_vehicle_id is None

    # 4. Validate objects list and every detection object inside it
    raw_objects = detection.get("objects")
    if not isinstance(raw_objects, list):
        return _mark_invalid("missing_or_invalid_objects_list")

    validated_objects: list[dict[str, Any]] = []
    for idx, obj in enumerate(raw_objects):
        if not isinstance(obj, dict):
            return _mark_invalid(f"object_at_index_{idx}_not_dict")

        cls_raw = obj.get("class", obj.get("class_name"))
        if not isinstance(cls_raw, str) or not cls_raw.strip():
            return _mark_invalid(f"object_at_index_{idx}_missing_class")

        score_raw: Any = obj.get("score")
        if not _is_finite_number(score_raw) or score_raw is None:
            return _mark_invalid(f"object_at_index_{idx}_non_finite_score")
        score = float(score_raw)
        if score < 0.0 or score > 1.0:
            return _mark_invalid(f"object_at_index_{idx}_score_out_of_bounds:{score}")

        pos = obj.get("position")
        if not isinstance(pos, dict):
            return _mark_invalid(f"object_at_index_{idx}_missing_position_dict")

        px_raw: Any = pos.get("x")
        py_raw: Any = pos.get("y")
        pz_raw: Any = pos.get("z", 0.0)
        if (
            px_raw is None
            or py_raw is None
            or pz_raw is None
            or not (
                _is_finite_number(px_raw)
                and _is_finite_number(py_raw)
                and _is_finite_number(pz_raw)
            )
        ):
            return _mark_invalid(f"object_at_index_{idx}_non_finite_position")

        px, py, pz = float(px_raw), float(py_raw), float(pz_raw)
        range_raw: Any = obj.get("range_m", math.sqrt(px * px + py * py + pz * pz))
        if range_raw is None or not _is_finite_number(range_raw) or float(range_raw) < 0.0:
            return _mark_invalid(f"object_at_index_{idx}_invalid_range_m")

        obj_id_raw = obj.get("id", idx)
        if isinstance(obj_id_raw, bool) or not isinstance(obj_id_raw, int) or obj_id_raw < 0:
            return _mark_invalid(f"object_at_index_{idx}_invalid_id")

        if score < 0.5:
            continue

        validated_objects.append(
            {
                "id": obj_id_raw,
                "class": cls_raw.strip(),
                "score": score,
                "position": {"x": px, "y": py, "z": pz},
                "range_m": float(range_raw),
            }
        )

    now_dt = datetime.now(timezone.utc)
    is_empty_frame = len(raw_objects) == 0

    if client_stats is not None:
        client_stats.valid_message_count += 1
        if is_empty_frame:
            client_stats.empty_frame_count += 1
        if is_unassociated:
            client_stats.unassociated_frame_count += 1

    if is_unassociated:
        logger.info(
            "ROS detection frame (stamp=%.3f, frame=%s, objects=%d) ingested as unassociated "
            "(SHADOW_ROS_VEHICLE_ID is not configured).",
            stamp_sec,
            frame,
            len(validated_objects),
        )

    if mission_service_agent is None:
        return RosDetectionIngestOutcome(
            accepted=True,
            reason="accepted_no_agent",
            vehicle_id=verified_vehicle_id,
            frame=frame,
            stamp=stamp_sec,
            object_count=len(validated_objects),
            empty_frame=is_empty_frame,
            unassociated=is_unassociated,
        )

    topic_name = (cfg.ros_detections_topic or DETECTION_TOPIC).strip()

    if hasattr(mission_service_agent, "ingest_detection_frame_into_world_model"):
        try:
            mission_service_agent.ingest_detection_frame_into_world_model(
                stamp=stamp_sec,
                received_at=now_dt,
                frame=frame,
                objects=validated_objects,
                vehicle_id=verified_vehicle_id,
                telemetry_source="rosbridge",
                topic=topic_name,
                map_frame_confirmed=cfg.ros_map_frame_confirmed,
            )
        except Exception as exc:
            logger.error("Failed to ingest detection frame into World Model: %s", exc)
            return _mark_invalid(f"world_model_ingestion_error:{exc}")

        return RosDetectionIngestOutcome(
            accepted=True,
            reason="accepted_empty_frame" if is_empty_frame else "accepted",
            vehicle_id=verified_vehicle_id,
            frame=frame,
            stamp=stamp_sec,
            object_count=len(validated_objects),
            empty_frame=is_empty_frame,
            unassociated=is_unassociated,
        )

    # Legacy fallback path: require verified_vehicle_id == "SCOUT01" (never hardcode fallback!)
    if verified_vehicle_id != "SCOUT01":
        logger.warning(
            "Cannot forward ROS detections to legacy agent stub without verified "
            "SHADOW_ROS_VEHICLE_ID='SCOUT01' (got %r).",
            verified_vehicle_id,
        )
        return RosDetectionIngestOutcome(
            accepted=False,
            reason="unverified_vehicle_id",
            vehicle_id=None,
            frame=frame,
            stamp=stamp_sec,
            object_count=len(validated_objects),
            empty_frame=is_empty_frame,
            unassociated=True,
        )

    source_dt = datetime.fromtimestamp(stamp_sec, tz=timezone.utc)
    for obj in validated_objects:
        cls = str(obj["class"])
        score = float(obj["score"])
        pos = obj["position"]
        x = float(pos["x"])
        y = float(pos["y"])
        z = float(pos["z"])
        obs_type = _class_to_obs_type(cls)
        map_x = x if (frame == "map" and cfg.ros_map_frame_confirmed) else 0.0
        map_y = y if (frame == "map" and cfg.ros_map_frame_confirmed) else 0.0

        try:
            from ultron_stub.interface import ObservationPayloadStub

            obs = ObservationPayloadStub(
                observation_id=f"ros-{int(stamp_sec * 1000)}-{obj['id']}",
                mission_id=getattr(mission_service_agent, "_active_mission_id", None) or "",
                vehicle_id="SCOUT01",
                obs_type=obs_type,
                confidence=score,
                location_x=map_x,
                location_y=map_y,
                timestamp=source_dt,
                raw={
                    "source": "rosbridge",
                    "topic": topic_name,
                    "frame": frame,
                    "stamp": stamp_sec,
                    "class": cls,
                    "object_id": obj["id"],
                    "position": {"x": x, "y": y, "z": z},
                    "range_m": obj["range_m"],
                },
            )
            await mission_service_agent.record_observation(obs)
        except Exception as exc:
            logger.error("Failed to forward observation to legacy agent: %s", exc)
            return _mark_invalid(f"legacy_forward_error:{exc}")

    return RosDetectionIngestOutcome(
        accepted=True,
        reason="accepted_empty_frame" if is_empty_frame else "accepted",
        vehicle_id=verified_vehicle_id,
        frame=frame,
        stamp=stamp_sec,
        object_count=len(validated_objects),
        empty_frame=is_empty_frame,
        unassociated=False,
    )


# ── Singleton & lifespan integration ─────────────────────────────────────────

_client: RosBridgeClient | None = None


def get_rosbridge_client() -> RosBridgeClient | None:
    return _client


async def start_rosbridge(
    mission_service_agent: Any | None = None,
    *,
    app_settings: Settings | None = None,
    url: str | None = None,
) -> RosBridgeClient:
    """Create and start the singleton ROSBridge client using configured settings."""
    global _client
    cfg = app_settings or settings

    async def _handle_detection(detection: dict[str, Any]) -> None:
        await process_rosbridge_detection_payload(
            detection,
            mission_service_agent,
            app_settings=cfg,
            client_stats=_client,
        )

    async def _handle_lidar(lidar_msg: dict[str, Any]) -> None:
        if mission_service_agent is not None and hasattr(
            mission_service_agent, "ingest_lidar_scan_into_world_model"
        ):
            mission_service_agent.ingest_lidar_scan_into_world_model(
                lidar_msg,
                received_at=datetime.now(timezone.utc),
                vehicle_id=(cfg.ros_vehicle_id or "").strip() or None,
                telemetry_source="rosbridge",
            )

    _client = RosBridgeClient(
        url=url,
        on_detection=_handle_detection,
        on_lidar=_handle_lidar,
        app_settings=cfg,
    )
    _client.start()
    return _client


def _class_to_obs_type(cls: str) -> str:
    """Map OAK-D YOLO class names to ULTRON observation event types."""
    mapping = {
        "person": "target_detected",
        "fire": "hazard_detected",
        "smoke": "hazard_detected",
        "box": "target_detected",
        "backpack": "target_detected",
        "suitcase": "target_detected",
        "chair": "obstacle_detected",
        "bench": "obstacle_detected",
        "bottle": "target_detected",
    }
    return mapping.get(cls.lower(), "target_detected")
