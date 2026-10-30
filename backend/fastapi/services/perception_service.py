"""Rover perception and object-detection service (`/detections_json` — Sections 1–10).

Responsibilities:
1. Receive and validate ROS 2 Humble `std_msgs/msg/String` (and `std_msgs/String`)
   detection payloads from `RosBridgeAdapter`.
2. Preserve `SHADOW_ROS_VEHICLE_ID` unset (`None`) until explicitly confirmed by the
   hardware team, keeping telemetry source (`mock` vs. `rosbridge`), operating mode
   (`simulated` vs. `real`), and actuator availability (`SHADOW_HARDWARE_LOCKOUT=true`)
   strictly separate.
3. Preserve the exact incoming coordinate frame identifier (`oak_rgb_camera_optical_frame`,
   `oak_parent_frame`, `map`, or any unrecognized frame), classify it via
   `classify_detection_frame()` and `CoordinateTransformPort`, and NEVER silently
   transform, clamp, or project camera-relative or unvalidated coordinates onto the
   global floor map.
4. Track both source timestamp (`stamp` in Unix epoch seconds) and local receive
   timestamp (`received_at`), plus diagnostic-only `clock_offset_seconds`, rejecting
   out-of-order frames (`stamp < latest_stamp`).
5. Track detection freshness (`is_stale`) separately from vehicle heartbeat health so
   a stale detection stream never marks the vehicle `UNKNOWN`.
6. Forward accepted camera detections (including camera-relative frames) into the
   in-process ULTRON `WorldModel` so `TrackedObject`s, `SensorObservation`s, and
   `SensorHealthReport`s are continuously updated and queryable via `WorldModelTools`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

from core.config import Settings, settings
from core.logging import get_logger
from db.models import Mission, Observation
from pydantic import ValidationError
from schemas.detections import (
    CoordinateTransformPort,
    DetectionCoordinateFrameEnum,
    DetectionFrame,
    DetectionFrameClassificationEnum,
    DetectionObject,
    DetectionsPushPayload,
    UnvalidatedCoordinateTransformStub,
    classify_detection_frame,
    parse_std_msgs_string_detection_frame,
)
from schemas.enums import (
    MissionStatusEnum,
    RosConnectionStateEnum,
    TelemetrySourceEnum,
    VehicleIdEnum,
)
from services.rosbridge_adapter import (
    RosBridgeAdapter,
    RosBridgeAdapterConfig,
    RosClientFactory,
    RosTopicFactory,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ultron_stub import ClockPort, ObservationPayloadStub, SystemClock, UltronAgentPort
from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway

logger = get_logger("services.perception")


@dataclass(frozen=True)
class DetectionIngestionResult:
    """Outcome of processing a single inbound detection frame or raw ROS message."""

    accepted: bool
    reason: str
    vehicle_id: VehicleIdEnum
    frame: str | None = None
    frame_classification: DetectionFrameClassificationEnum | None = None
    map_projection_allowed: bool = False
    object_count: int = 0
    persisted_observations: int = 0
    out_of_order: bool = False


class _VehiclePerceptionTracker:
    """In-memory state tracker for a vehicle's detection stream."""

    def __init__(
        self,
        vehicle_id: VehicleIdEnum,
        *,
        configured_vehicle_id: str | None,
        telemetry_source: TelemetrySourceEnum,
        ros_connection_state: RosConnectionStateEnum,
        topic: str,
        message_type: str,
        stale_after_seconds: float,
    ) -> None:
        self.vehicle_id = vehicle_id
        self.configured_vehicle_id = configured_vehicle_id
        self.telemetry_source = telemetry_source
        self.ros_connection_state = ros_connection_state
        self.topic = topic
        self.message_type = message_type
        self.stale_after_seconds = stale_after_seconds

        self.latest_stamp: float | None = None
        self.latest_source_timestamp: datetime | None = None
        self.latest_received_at: datetime | None = None
        self.latest_clock_offset_seconds: float | None = None
        self.latest_frame: str | None = None
        self.latest_frame_classification: DetectionFrameClassificationEnum | None = None
        self.latest_map_projection_allowed: bool = False
        self.latest_objects: list[DetectionObject] = []
        self.has_received_frame: bool = False
        self.out_of_order_count: int = 0
        self.invalid_message_count: int = 0

        self.last_persisted_at: datetime | None = None
        self.last_persisted_signature: tuple[tuple[int, str, str], ...] | None = None


class PerceptionService:
    """Manages validated rover object detections, coordinate-frame safety, freshness, WS pushes, and persistence."""

    def __init__(
        self,
        *,
        app_settings: Settings | None = None,
        clock: ClockPort | None = None,
        gateway: DashboardGateway | None = None,
        agent: UltronAgentPort | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        ros_adapter: RosBridgeAdapter | None = None,
        ros_client_factory: RosClientFactory | None = None,
        ros_topic_factory: RosTopicFactory | None = None,
        transform_port: CoordinateTransformPort | None = None,
        persist_min_interval_seconds: float = 1.0,
    ) -> None:
        self._settings = app_settings or settings
        self._clock: ClockPort = clock or SystemClock()
        self._gateway: DashboardGateway = gateway or dashboard_gateway
        self._agent: UltronAgentPort | None = agent
        self._session_factory = session_factory
        self._persist_min_interval_seconds = max(0.0, persist_min_interval_seconds)
        self._transform_port: CoordinateTransformPort = (
            transform_port
            or UnvalidatedCoordinateTransformStub(
                map_frame_confirmed=self._settings.ros_map_frame_confirmed
            )
        )

        # Vehicle ID remains None until explicitly configured
        configured_vid_str = self._settings.ros_vehicle_id
        self._configured_vehicle_id: str | None = (
            configured_vid_str if configured_vid_str else None
        )
        self._ros_vehicle_id: VehicleIdEnum | None = (
            VehicleIdEnum(configured_vid_str)
            if (self._settings.ros_enabled and configured_vid_str == "SCOUT01")
            else None
        )

        self._trackers: dict[VehicleIdEnum, _VehiclePerceptionTracker] = {}
        for vid in (VehicleIdEnum.SCOUT01,):
            is_ros_target = self._settings.ros_enabled and self._ros_vehicle_id == vid
            self._trackers[vid] = _VehiclePerceptionTracker(
                vid,
                configured_vehicle_id=self._configured_vehicle_id,
                telemetry_source=(
                    TelemetrySourceEnum.ROSBRIDGE
                    if is_ros_target
                    else TelemetrySourceEnum.MOCK
                ),
                ros_connection_state=(
                    RosConnectionStateEnum.DISCONNECTED
                    if is_ros_target
                    else RosConnectionStateEnum.DISABLED
                ),
                topic=self._settings.ros_detections_topic,
                message_type=self._settings.ros_detections_message_type,
                stale_after_seconds=self._settings.ros_detections_stale_after_seconds,
            )

        if ros_adapter is not None:
            self._ros_adapter = ros_adapter
            self._ros_adapter.set_callbacks(
                on_detection_message=self._handle_adapter_detection_callback,
                on_connection_state_change=self.handle_ros_connection_state_change,
            )
        else:
            adapter_cfg = RosBridgeAdapterConfig(
                enabled=self._settings.ros_enabled,
                host=self._settings.ros_host,
                port=self._settings.ros_port,
                vehicle_id=self._settings.ros_vehicle_id,
                detections_topic=self._settings.ros_detections_topic,
                message_type=self._settings.ros_detections_message_type,
                queue_maxsize=self._settings.ros_queue_maxsize,
                reconnect_interval_seconds=self._settings.ros_reconnect_interval_seconds,
            )
            self._ros_adapter = RosBridgeAdapter(
                adapter_cfg,
                on_detection_message=self._handle_adapter_detection_callback,
                on_connection_state_change=self.handle_ros_connection_state_change,
                ros_client_factory=ros_client_factory,
                ros_topic_factory=ros_topic_factory,
            )

    @property
    def clock(self) -> ClockPort:
        return self._clock

    @property
    def ros_adapter(self) -> RosBridgeAdapter:
        return self._ros_adapter

    @property
    def ros_vehicle_id(self) -> VehicleIdEnum | None:
        return self._ros_vehicle_id

    @property
    def configured_vehicle_id(self) -> str | None:
        return self._configured_vehicle_id

    @property
    def transform_port(self) -> CoordinateTransformPort:
        return self._transform_port

    def set_gateway(self, gateway: DashboardGateway) -> None:
        self._gateway = gateway

    def set_agent(self, agent: UltronAgentPort | None) -> None:
        self._agent = agent

    def set_session_factory(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
    ) -> None:
        self._session_factory = session_factory

    async def start(self) -> None:
        """Start the underlying ROSBridge adapter if enabled."""
        await self._ros_adapter.start()

    async def stop(self) -> None:
        """Stop the underlying ROSBridge adapter cleanly."""
        await self._ros_adapter.stop()

    def is_detection_stream_stale(self, vehicle_id: VehicleIdEnum) -> bool:
        """Return True if a frame was previously received and its age exceeds `stale_after_seconds`."""
        tracker = self._trackers.get(vehicle_id)
        if tracker is None or not tracker.has_received_frame or tracker.latest_received_at is None:
            return False
        elapsed_sec = (self._clock.now() - tracker.latest_received_at).total_seconds()
        return elapsed_sec > tracker.stale_after_seconds

    def _build_intelligence_metadata(
        self,
        tracker: _VehiclePerceptionTracker,
        is_stale: bool,
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, Any] | None,
        list[dict[str, Any]],
    ]:
        """Extract tracked objects, sensor health, free space, and decision traces from the ULTRON agent."""
        now_ts = self._clock.now().timestamp()

        if self._agent is not None and hasattr(self._agent, "sync_camera_sensor_health"):
            self._agent.sync_camera_sensor_health(
                enabled=self._settings.ros_enabled or tracker.has_received_frame,
                ros_connection_state=tracker.ros_connection_state.value,
                telemetry_source=tracker.telemetry_source.value,
                topic=tracker.topic,
                message_type=tracker.message_type,
                frame=tracker.latest_frame,
                vehicle_id=tracker.configured_vehicle_id,
                stamp=tracker.latest_stamp,
                received_at=tracker.latest_received_at,
                is_stale=is_stale,
                stale_after_seconds=tracker.stale_after_seconds,
                invalid_message_count=tracker.invalid_message_count,
                out_of_order_count=tracker.out_of_order_count,
            )

        if self._agent is not None and hasattr(self._agent, "get_environment_snapshot_model"):
            snap = self._agent.get_environment_snapshot_model(current_time=now_ts)
            tracked_list = [
                obj.model_dump(mode="json")
                for obj in snap.detected_objects.objects
            ]
            sensor_health_dict = {
                k: v.model_dump(mode="json")
                for k, v in snap.sensor_health.sensors.items()
            }
            free_space_dict = snap.free_space.model_dump(mode="json")
            world_obj = getattr(self._agent, "world", None)
            traces_list = (
                [
                    dt.model_dump(mode="json")
                    for dt in world_obj.environment.decision_traces[-20:]
                ]
                if world_obj is not None
                else []
            )
            return tracked_list, sensor_health_dict, free_space_dict, traces_list

        # Fallback for StubUltronAgent so dashboard still receives structured sensor health & tracked objects
        cam_status = (
            "STALE"
            if is_stale
            else (
                "CONNECTED"
                if tracker.ros_connection_state == RosConnectionStateEnum.CONNECTED
                and tracker.has_received_frame
                else (
                    "DISCONNECTED"
                    if tracker.ros_connection_state
                    in (
                        RosConnectionStateEnum.DISCONNECTED,
                        RosConnectionStateEnum.CONNECTING,
                        RosConnectionStateEnum.RECONNECTING,
                    )
                    else "UNAVAILABLE"
                )
            )
        )
        fallback_health: dict[str, dict[str, Any]] = {
            "depth_camera": {
                "sensor_id": "depth_camera",
                "sensor_type": "depth_camera",
                "enabled": self._settings.ros_enabled or tracker.has_received_frame,
                "status": cam_status,
                "source_mode": (
                    "physical"
                    if tracker.telemetry_source == TelemetrySourceEnum.ROSBRIDGE
                    else "simulated"
                ),
                "topic": tracker.topic,
                "message_type": tracker.message_type,
                "coordinate_frame": tracker.latest_frame or "oak_rgb_camera_optical_frame",
                "vehicle_id": tracker.configured_vehicle_id,
                "last_message_timestamp": tracker.latest_stamp,
                "last_ingested_at": (
                    tracker.latest_received_at.timestamp()
                    if tracker.latest_received_at
                    else None
                ),
                "message_frequency_hz": 0.0,
                "is_stale": is_stale,
                "stale_after_sec": tracker.stale_after_seconds,
                "valid_message_count": 1 if tracker.has_received_frame else 0,
                "malformed_message_count": tracker.invalid_message_count,
                "duplicate_message_count": 0,
                "out_of_order_count": tracker.out_of_order_count,
                "error_state": None,
                "notes": "Depth camera object detection stream (/detections_json).",
            },
            "lidar": {
                "sensor_id": "lidar",
                "sensor_type": "lidar",
                "enabled": False,
                "status": "UNAVAILABLE",
                "source_mode": "simulated",
                "topic": "",
                "message_type": "",
                "coordinate_frame": "",
                "vehicle_id": None,
                "last_message_timestamp": None,
                "last_ingested_at": None,
                "message_frequency_hz": 0.0,
                "is_stale": False,
                "stale_after_sec": 2.0,
                "valid_message_count": 0,
                "malformed_message_count": 0,
                "duplicate_message_count": 0,
                "out_of_order_count": 0,
                "error_state": None,
                "notes": "LiDAR topic, message type, frame, and extrinsics are unconfirmed by hardware team; marked UNAVAILABLE.",
            },
        }
        fallback_tracked = [
            {
                "object_id": f"OBJ-{idx:03d}",
                "label": obj.class_name,
                "position": {
                    "x": obj.position.x,
                    "y": obj.position.y,
                    "z": obj.position.z,
                    "range_m": obj.range_m,
                    "uncertainty_m": round(max(0.05, 0.08 * obj.range_m), 3),
                },
                "coordinate_frame": tracker.latest_frame or "oak_rgb_camera_optical_frame",
                "position_uncertainty_m": round(max(0.05, 0.08 * obj.range_m), 3),
                "classification_confidence": obj.score,
                "first_observed_timestamp": tracker.latest_stamp or now_ts,
                "last_observed_timestamp": tracker.latest_stamp or now_ts,
                "last_ingested_at": (
                    tracker.latest_received_at.timestamp()
                    if tracker.latest_received_at
                    else now_ts
                ),
                "last_known_source": "depth_camera",
                "vehicle_id": tracker.configured_vehicle_id,
                "source_mode": (
                    "physical"
                    if tracker.telemetry_source == TelemetrySourceEnum.ROSBRIDGE
                    else "simulated"
                ),
                "contributing_observation_ids": [f"DET-{obj.id:03d}"],
                "contributing_sensor_types": ["depth_camera"],
                "last_detector_object_id": obj.id,
                "tracking_status": "STALE" if is_stale else "ACTIVE",
                "epistemic_status": "STALE" if is_stale else "OBSERVED",
                "currently_visible": not is_stale,
                "corroborated_by_multiple_sensors": False,
                "is_obstacle": obj.class_name.lower() in {"chair", "bench", "box", "obstacle", "wall"},
                "is_hazard": obj.class_name.lower() in {"fire", "smoke", "hazard"},
            }
            for idx, obj in enumerate(tracker.latest_objects, start=1)
        ]
        fallback_free_space = {
            "is_verified_clear": False,
            "free_space_radius_m": None,
            "epistemic_status": "UNKNOWN",
            "reason": "Cannot verify free space: 360° LiDAR geometric coverage is UNAVAILABLE.",
        }
        return fallback_tracked, fallback_health, fallback_free_space, []

    def get_vehicle_detection_state(
        self,
        vehicle_id: VehicleIdEnum | str = VehicleIdEnum.SCOUT01,
    ) -> DetectionsPushPayload:
        """Return the current validated `DetectionsPushPayload` for `vehicle_id`."""
        vid = VehicleIdEnum(str(vehicle_id))
        tracker = self._trackers[vid]
        is_stale = self.is_detection_stream_stale(vid)
        tracked_objs, sensor_health, free_space, decision_traces = (
            self._build_intelligence_metadata(tracker, is_stale)
        )
        return DetectionsPushPayload(
            vehicle_id=vid,
            configured_vehicle_id=tracker.configured_vehicle_id,
            telemetry_source=tracker.telemetry_source,
            actuation_locked=self._settings.hardware_lockout,
            ros_connection_state=tracker.ros_connection_state,
            topic=tracker.topic,
            message_type=tracker.message_type,
            stamp=tracker.latest_stamp,
            source_timestamp=tracker.latest_source_timestamp,
            received_at=tracker.latest_received_at,
            clock_offset_seconds=tracker.latest_clock_offset_seconds,
            frame=tracker.latest_frame,
            frame_classification=tracker.latest_frame_classification,
            map_projection_allowed=tracker.latest_map_projection_allowed,
            objects=list(tracker.latest_objects),
            is_stale=is_stale,
            stale_after_seconds=tracker.stale_after_seconds,
            has_received_frame=tracker.has_received_frame,
            out_of_order_count=tracker.out_of_order_count,
            invalid_message_count=tracker.invalid_message_count,
            tracked_objects=tracked_objs,
            sensor_health=sensor_health,
            free_space=free_space,
            decision_traces=decision_traces,
        )

    def get_all_detection_states(self) -> dict[VehicleIdEnum, DetectionsPushPayload]:
        """Return current detection state map for all tracked vehicles (for WS snapshot)."""
        return {
            vid: self.get_vehicle_detection_state(vid)
            for vid in self._trackers
        }

    async def handle_ros_connection_state_change(
        self,
        new_state: RosConnectionStateEnum,
    ) -> None:
        """Update connection state for the configured ROS vehicle and broadcast to dashboard."""
        target_vid = self._ros_vehicle_id or VehicleIdEnum.SCOUT01
        tracker = self._trackers.get(target_vid)
        if tracker is None:
            return
        if self._ros_vehicle_id is not None:
            tracker.telemetry_source = TelemetrySourceEnum.ROSBRIDGE
        tracker.ros_connection_state = new_state
        state_payload = self.get_vehicle_detection_state(target_vid)
        await self._gateway.broadcast_detections(state_payload)

    async def _handle_adapter_detection_callback(
        self,
        raw_message: dict[str, Any] | str,
    ) -> None:
        """Internal callback bridge returning None for RosBridgeAdapter."""
        await self.handle_raw_ros_message(raw_message)

    async def handle_raw_ros_message(
        self,
        raw_message: dict[str, Any] | str,
        *,
        vehicle_id: VehicleIdEnum | str | None = None,
        session: AsyncSession | None = None,
    ) -> DetectionIngestionResult:
        """Validate a raw `std_msgs/msg/String` message, update state, broadcast, and selectively persist."""
        target_vid = (
            VehicleIdEnum(str(vehicle_id))
            if vehicle_id is not None
            else (self._ros_vehicle_id or VehicleIdEnum.SCOUT01)
        )
        tracker = self._trackers[target_vid]

        try:
            frame_model, raw_dict = parse_std_msgs_string_detection_frame(raw_message)
        except (ValueError, ValidationError) as exc:
            tracker.invalid_message_count += 1
            logger.warning(
                f"Rejected invalid ROS detection message on {tracker.topic} "
                f"for {target_vid.value}: {exc}"
            )
            return DetectionIngestionResult(
                accepted=False,
                reason=f"invalid_payload: {exc}",
                vehicle_id=target_vid,
            )

        return await self.ingest_detection_frame(
            frame_model,
            vehicle_id=target_vid,
            raw_payload=raw_dict,
            session=session,
        )

    async def ingest_detection_frame(
        self,
        frame: DetectionFrame,
        *,
        vehicle_id: VehicleIdEnum | str | None = None,
        raw_payload: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> DetectionIngestionResult:
        """Process a validated `DetectionFrame`, enforce timestamp ordering, classify frame, update World Model, broadcast, and persist."""
        target_vid = (
            VehicleIdEnum(str(vehicle_id))
            if vehicle_id is not None
            else (self._ros_vehicle_id or VehicleIdEnum.SCOUT01)
        )
        tracker = self._trackers[target_vid]
        now = self._clock.now()

        # Classify coordinate frame and check if global map projection is validated
        classification, base_map_allowed = classify_detection_frame(
            frame.frame,
            map_frame_confirmed=self._settings.ros_map_frame_confirmed,
        )
        map_projection_allowed = (
            base_map_allowed
            and self._transform_port.is_transform_validated(frame.frame, "map")
        )

        # Reject out-of-order frames strictly older than the latest accepted source timestamp
        if tracker.latest_stamp is not None and frame.stamp < tracker.latest_stamp:
            tracker.out_of_order_count += 1
            logger.warning(
                f"Rejected out-of-order detection frame for {target_vid.value} "
                f"(stamp={frame.stamp} < latest_stamp={tracker.latest_stamp})."
            )
            return DetectionIngestionResult(
                accepted=False,
                reason="out_of_order_source_timestamp",
                vehicle_id=target_vid,
                frame=frame.frame,
                frame_classification=classification,
                map_projection_allowed=map_projection_allowed,
                object_count=len(frame.objects),
                out_of_order=True,
            )

        # Calculate diagnostic-only clock offset without rewriting source timestamp
        clock_offset_sec = round(now.timestamp() - frame.stamp, 6)

        tracker.latest_stamp = frame.stamp
        tracker.latest_source_timestamp = frame.source_timestamp
        tracker.latest_received_at = now
        tracker.latest_clock_offset_seconds = clock_offset_sec
        tracker.latest_frame = frame.frame
        tracker.latest_frame_classification = classification
        tracker.latest_map_projection_allowed = map_projection_allowed
        tracker.latest_objects = list(frame.objects)
        tracker.has_received_frame = True
        if tracker.ros_connection_state in (
            RosConnectionStateEnum.DISCONNECTED,
            RosConnectionStateEnum.CONNECTING,
            RosConnectionStateEnum.RECONNECTING,
        ):
            tracker.ros_connection_state = RosConnectionStateEnum.CONNECTED

        # Forward accepted camera detection frame into ULTRON's WorldModel (preserving exact frame & timestamps)
        if self._agent is not None and hasattr(
            self._agent, "ingest_detection_frame_into_world_model"
        ):
            self._agent.ingest_detection_frame_into_world_model(
                stamp=frame.stamp,
                received_at=now,
                frame=frame.frame,
                objects=list(frame.objects),
                vehicle_id=tracker.configured_vehicle_id,
                telemetry_source=tracker.telemetry_source.value,
                topic=tracker.topic,
                map_frame_confirmed=map_projection_allowed,
            )

        persisted_count = 0
        if frame.objects:
            if session is not None:
                persisted_count = await self._maybe_persist_observations(
                    session,
                    tracker=tracker,
                    frame=frame,
                    frame_classification=classification,
                    map_projection_allowed=map_projection_allowed,
                    received_at=now,
                    clock_offset_seconds=clock_offset_sec,
                    raw_payload=raw_payload,
                )
            elif self._session_factory is not None:
                async with self._session_factory() as db_session:
                    persisted_count = await self._maybe_persist_observations(
                        db_session,
                        tracker=tracker,
                        frame=frame,
                        frame_classification=classification,
                        map_projection_allowed=map_projection_allowed,
                        received_at=now,
                        clock_offset_seconds=clock_offset_sec,
                        raw_payload=raw_payload,
                    )

        push_payload = self.get_vehicle_detection_state(target_vid)
        await self._gateway.broadcast_detections(push_payload)

        return DetectionIngestionResult(
            accepted=True,
            reason="accepted",
            vehicle_id=target_vid,
            frame=frame.frame,
            frame_classification=classification,
            map_projection_allowed=map_projection_allowed,
            object_count=len(frame.objects),
            persisted_observations=persisted_count,
            out_of_order=False,
        )

    async def _maybe_persist_observations(
        self,
        session: AsyncSession,
        *,
        tracker: _VehiclePerceptionTracker,
        frame: DetectionFrame,
        frame_classification: DetectionFrameClassificationEnum,
        map_projection_allowed: bool,
        received_at: datetime,
        clock_offset_seconds: float,
        raw_payload: dict[str, Any] | None,
    ) -> int:
        """Selective persistence policy for detection frames (Section 6)."""
        signature = tuple(
            sorted((obj.id, obj.class_name, frame.frame) for obj in frame.objects)
        )
        if (
            tracker.last_persisted_at is not None
            and tracker.last_persisted_signature == signature
            and (received_at - tracker.last_persisted_at).total_seconds()
            < self._persist_min_interval_seconds
        ):
            return 0

        active_mission_stmt = (
            select(Mission.id)
            .where(
                Mission.status.in_(
                    [
                        MissionStatusEnum.ACTIVE.value,
                        MissionStatusEnum.IN_PROGRESS.value,
                    ]
                )
            )
            .order_by(Mission.created_at.desc(), Mission.id.desc())
            .limit(1)
        )
        active_mission_id = (
            await session.execute(active_mission_stmt)
        ).scalar_one_or_none()
        if active_mission_id is None:
            return 0

        is_confirmed_map = (
            frame.frame == DetectionCoordinateFrameEnum.MAP.value
            and map_projection_allowed
        )
        persisted = 0

        for obj in frame.objects:
            obs_id = f"DET-{uuid.uuid4().hex[:10].upper()}"
            loc_x = obj.position.x if is_confirmed_map else 0.0
            loc_y = obj.position.y if is_confirmed_map else 0.0
            raw_record: dict[str, Any] = {
                "source": "rosbridge",
                "topic": tracker.topic,
                "message_type": tracker.message_type,
                "configured_vehicle_id": tracker.configured_vehicle_id,
                "frame": frame.frame,
                "frame_classification": frame_classification.value,
                "coordinates_are_map_frame": is_confirmed_map,
                "map_projection_allowed": map_projection_allowed,
                "stamp": frame.stamp,
                "source_timestamp": frame.source_timestamp.isoformat(),
                "received_at": received_at.isoformat(),
                "clock_offset_seconds": clock_offset_seconds,
                "object_id": obj.id,
                "id_is_frame_local_only": True,
                "class": obj.class_name,
                "score": obj.score,
                "position": {
                    "x": obj.position.x,
                    "y": obj.position.y,
                    "z": obj.position.z,
                },
                "range_m": obj.range_m,
            }
            if raw_payload is not None:
                raw_record["raw_frame"] = raw_payload

            obs_row = Observation(
                id=obs_id,
                mission_id=active_mission_id,
                vehicle_id=tracker.vehicle_id.value,
                obs_type=f"detection:{obj.class_name}",
                confidence=obj.score,
                location_x=loc_x,
                location_y=loc_y,
                timestamp=frame.source_timestamp,
                raw=raw_record,
            )
            session.add(obs_row)
            persisted += 1

            # Forward validated map-frame detections to generic UltronAgentPort
            if is_confirmed_map and self._agent is not None:
                with_stub = ObservationPayloadStub(
                    observation_id=obs_id,
                    mission_id=active_mission_id,
                    vehicle_id="SCOUT01",
                    obs_type=f"detection:{obj.class_name}",
                    confidence=obj.score,
                    location_x=loc_x,
                    location_y=loc_y,
                    timestamp=frame.source_timestamp,
                    raw=raw_record,
                )
                await self._agent.record_observation(with_stub)

        if persisted > 0:
            await session.commit()
            tracker.last_persisted_at = received_at
            tracker.last_persisted_signature = signature

        return persisted


perception_service = PerceptionService()
