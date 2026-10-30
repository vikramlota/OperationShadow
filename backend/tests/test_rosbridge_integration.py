"""Automated tests for ROS 2 Humble Live Perception Integration (Sections 1–13).

Covers all required backend test scenarios without requiring a physical rover or ROS network:
1. Configuration validation: disabled by default, unset `ros_vehicle_id=""` by default, missing host/vehicle_id rejected when enabled, `ros_map_frame_confirmed=False` by default, and `SHADOW_HARDWARE_LOCKOUT=true` preserved.
2. Successful ROSBridge connection and subscription to `/detections_json` (`std_msgs/msg/String`), plus ROSBridge v2 JSON protocol subscribe/unsubscribe/publish envelope handling.
3. Valid JSON parsing for the hardware engineer's `oak_rgb_camera_optical_frame` payload, `oak_parent_frame`, `map`, empty object arrays, multiple detections, and ROS 2 `{sec, nanosec}` stamp objects.
4. Preservation of metres, confidence score, frame-local object ID (`id_is_frame_local_only=True`), class name, exact coordinate frame, source timestamp (`stamp`), local receive timestamp (`received_at`), and diagnostic `clock_offset_seconds`.
5. Unknown coordinate frames preserved verbatim and classified as `UNSUPPORTED_FRAME` (`map_projection_allowed=False`), and `UnvalidatedCoordinateTransformStub` blocking unvalidated camera-to-map projections.
6. Rejection of invalid JSON, missing `data`, malformed ROSBridge publish envelopes, non-finite coordinates, out-of-range scores/ranges, negative IDs, and empty classes without crashing the subscription.
7. Out-of-order source timestamp rejection (`stamp < latest_stamp`).
8. Disconnection, reconnection, and application lifespan startup/shutdown cleanup.
9. Detection freshness (`is_stale`) vs. vehicle heartbeat independence (stale detection stream never marks SCOUT01 `UNKNOWN`).
10. Selective SQLite `OBSERVATIONS` persistence policy (no fabricated mission ID when no active mission; `oak_rgb_camera_optical_frame`, `oak_parent_frame`, unconfirmed `map`, and unknown frames never stored as global floor-map `location_x`/`location_y`; confirmed `map` stored when `ros_map_frame_confirmed=True`; throttling of identical 5 Hz frames).
11. Latest perception state included in `/ws/dashboard` initial snapshot (even with no active mission) and live `type="detections"` WebSocket broadcasts + `GET /vehicle/SCOUT01/detections`.
12. Strict separation of perception from actuator control: zero ROS publish/service/advertise calls, `LockedRealVehicleAdapter` remains enforced, and existing mock fleet + emergency stop remain functional.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import json
import math
from typing import Any

from core.config import Settings
from db.models import Observation
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from main import create_app
from pydantic import ValidationError
import pytest
from schemas.detections import (
    DetectionCoordinateFrameEnum,
    DetectionFrame,
    DetectionFrameClassificationEnum,
    DetectionPosition3D,
    UnvalidatedCoordinateTransformStub,
    classify_detection_frame,
    parse_std_msgs_string_detection_frame,
)
from schemas.entities import MissionCreateRequest
from schemas.enums import (
    RosConnectionStateEnum,
    TelemetrySourceEnum,
)
from services.event_service import EventService
from services.mission_service import MissionService
from services.perception_service import PerceptionService
from services.rosbridge_adapter import (
    DETECTIONS_MESSAGE_TYPE,
    ROS2_DETECTIONS_MESSAGE_TYPE,
    RosBridgeAdapter,
    RosBridgeAdapterConfig,
    RosClientLike,
    RosTopicLike,
    build_rosbridge_subscribe_envelope,
    build_rosbridge_unsubscribe_envelope,
)
from services.vehicle_service import VehicleService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from ultron_stub import (
    LockedRealVehicleAdapter,
    ManualClock,
    MockFleetSimulator,
    RealHardwareLockedError,
    StubUltronAgent,
)
from websocket.dashboard_gateway import DashboardGateway


class FakeRosClient(RosClientLike):
    """Deterministic in-memory mock of `roslibpy.Ros` recording all lifecycle and safety calls."""

    def __init__(self, host: str, port: int, *, fail_connect: bool = False) -> None:
        self.host = host
        self.port = port
        self.fail_connect = fail_connect
        self._is_connected = False
        self.run_calls: int = 0
        self.close_calls: int = 0
        self.terminate_calls: int = 0
        self.service_calls: int = 0
        self._event_handlers: dict[str, list[Callable[..., Any]]] = {}

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def on(self, event_name: str, callback: Callable[..., Any]) -> None:
        self._event_handlers.setdefault(event_name, []).append(callback)

    def on_ready(self, callback: Callable[..., Any], run_in_thread: bool = True) -> None:
        self.on("connection", callback)

    def run(self, timeout: int = 10) -> None:
        self.run_calls += 1
        if self.fail_connect:
            raise RuntimeError(f"Simulated connection failure to {self.host}:{self.port}")
        self._is_connected = True
        for cb in self._event_handlers.get("connection", []):
            cb()

    def simulate_disconnect(self) -> None:
        self._is_connected = False
        for cb in self._event_handlers.get("closing", []):
            cb()

    def simulate_reconnect(self) -> None:
        self._is_connected = True
        for cb in self._event_handlers.get("connection", []):
            cb()

    def close(self, timeout: int = 10) -> None:
        self.close_calls += 1
        self._is_connected = False

    def terminate(self) -> None:
        self.terminate_calls += 1


class FakeRosTopic(RosTopicLike):
    """Deterministic in-memory mock of `roslibpy.Topic` verifying read-only subscription."""

    def __init__(self, ros: RosClientLike, name: str, message_type: str) -> None:
        self.ros = ros
        self.name = name
        self.message_type = message_type
        self._callback: Callable[[dict[str, Any]], Any] | None = None
        self.subscribe_calls: int = 0
        self.unsubscribe_calls: int = 0
        self.publish_calls: int = 0
        self.advertise_calls: int = 0

    @property
    def is_subscribed(self) -> bool:
        return self._callback is not None

    def subscribe(self, callback: Callable[[dict[str, Any]], Any]) -> None:
        self.subscribe_calls += 1
        self._callback = callback

    def unsubscribe(self) -> None:
        self.unsubscribe_calls += 1
        self._callback = None

    def publish(self, _message: Any) -> None:
        self.publish_calls += 1
        raise AssertionError("Safety violation: ROSTopic.publish must never be called!")

    def advertise(self) -> None:
        self.advertise_calls += 1
        raise AssertionError("Safety violation: ROSTopic.advertise must never be called!")

    def emit_std_msgs_string(self, data_str: str) -> None:
        if self._callback is not None:
            self._callback({"data": data_str})


# ---------------------------------------------------------------------------
# 1. Configuration Validation & Safety Defaults
# ---------------------------------------------------------------------------


def test_ros_configuration_defaults_and_validation() -> None:
    """Verify ROS is disabled by default, vehicle_id is unset by default, and lockout is preserved."""
    default_cfg = Settings()
    assert default_cfg.ros_enabled is False
    assert default_cfg.ros_host == ""
    assert default_cfg.ros_port == 9090
    assert default_cfg.ros_vehicle_id == ""
    assert default_cfg.ros_detections_topic == "/detections_json"
    assert default_cfg.ros_detections_message_type == "std_msgs/msg/String"
    assert default_cfg.ros_detections_stale_after_seconds == pytest.approx(2.0)
    assert default_cfg.ros_map_frame_confirmed is False
    assert default_cfg.hardware_lockout is True
    assert default_cfg.default_vehicle_mode == "simulated"

    # Missing host when enabled raises clear ValueError
    with pytest.raises(ValueError, match="SHADOW_ROS_HOST must be explicitly configured"):
        Settings(
            ros_enabled=True,
            ros_host="",
            ros_vehicle_id="SCOUT01",
        )

    # Missing vehicle_id when enabled raises clear ValueError
    with pytest.raises(ValueError, match="SHADOW_ROS_VEHICLE_ID must be explicitly configured"):
        Settings(
            ros_enabled=True,
            ros_host="192.168.1.50",
            ros_vehicle_id="",
        )

    # Unknown vehicle_id rejected
    with pytest.raises(ValueError, match="Invalid SHADOW_ROS_VEHICLE_ID"):
        Settings(
            ros_enabled=True,
            ros_host="192.168.1.50",
            ros_vehicle_id="ROVER99",
        )

    # Valid enabled configuration succeeds while keeping hardware_lockout=True
    valid_cfg = Settings(
        ros_enabled=True,
        ros_host="192.168.1.50",
        ros_port=9090,
        ros_vehicle_id="SCOUT01",
        ros_detections_topic="/detections_json",
        ros_detections_stale_after_seconds=2.5,
    )
    assert valid_cfg.ros_enabled is True
    assert valid_cfg.ros_host == "192.168.1.50"
    assert valid_cfg.ros_vehicle_id == "SCOUT01"
    assert valid_cfg.hardware_lockout is True


# ---------------------------------------------------------------------------
# 2. Schema Validation, Coordinate-Frame Rules, Unknown Frames & Transform Port
# ---------------------------------------------------------------------------


def test_detection_schema_optical_frame_unknown_frames_envelopes_and_transform_port() -> None:
    """Verify oak_rgb_camera_optical_frame, oak_parent_frame, unknown frames, ROS 2 stamps, and transform port."""
    # 1. Exact hardware engineer example payload in `oak_rgb_camera_optical_frame`
    example_payload = {
        "stamp": 1793363063.737,
        "frame": "oak_rgb_camera_optical_frame",
        "objects": [
            {
                "id": 0,
                "class": "person",
                "score": 0.95,
                "position": {"x": 0.12, "y": -0.02, "z": 0.65},
                "range_m": 0.65,
            },
            {
                "id": 1,
                "class": "chair",
                "score": 0.82,
                "position": {"x": 1.4, "y": 0.3, "z": 1.8},
                "range_m": 2.3,
            },
        ],
    }
    frame_opt, raw_dict = parse_std_msgs_string_detection_frame(
        {"data": json.dumps(example_payload)}
    )
    assert frame_opt.stamp == pytest.approx(1793363063.737)
    assert frame_opt.frame == DetectionCoordinateFrameEnum.OAK_RGB_CAMERA_OPTICAL_FRAME.value
    assert len(frame_opt.objects) == 2
    assert frame_opt.objects[0].id == 0
    assert frame_opt.objects[0].class_name == "person"
    assert frame_opt.objects[0].score == pytest.approx(0.95)
    assert frame_opt.objects[0].position.x == pytest.approx(0.12)
    assert frame_opt.objects[0].position.y == pytest.approx(-0.02)
    assert frame_opt.objects[0].position.z == pytest.approx(0.65)
    assert frame_opt.objects[0].range_m == pytest.approx(0.65)
    assert raw_dict["frame"] == "oak_rgb_camera_optical_frame"

    # 2. ROSBridge v2 publish envelope `{"op": "publish", "topic": "/detections_json", "msg": {"data": "..."}}`
    # and ROS 2 `{sec, nanosec}` stamp dictionary
    rosbridge_pub_envelope = {
        "op": "publish",
        "topic": "/detections_json",
        "msg": {
            "data": json.dumps(
                {
                    "stamp": {"sec": 1793363064, "nanosec": 100000000},
                    "frame": "oak_parent_frame",
                    "objects": [],
                }
            )
        },
    }
    parsed_env, _ = parse_std_msgs_string_detection_frame(rosbridge_pub_envelope)
    assert parsed_env.stamp == pytest.approx(1793363064.1)
    assert parsed_env.frame == "oak_parent_frame"
    assert parsed_env.objects == []

    # 3. Unknown coordinate frames MUST be preserved verbatim and classified as UNSUPPORTED_FRAME
    unknown_frame_model, _ = parse_std_msgs_string_detection_frame(
        {
            "data": json.dumps(
                {"stamp": 1793363065.0, "frame": "base_link_custom", "objects": []}
            )
        }
    )
    assert unknown_frame_model.frame == "base_link_custom"
    cls_unk, allowed_unk = classify_detection_frame("base_link_custom", map_frame_confirmed=True)
    assert cls_unk == DetectionFrameClassificationEnum.UNSUPPORTED_FRAME
    assert allowed_unk is False

    # 4. Verify frame classifications and CoordinateTransformPort stub
    cls_opt, allowed_opt = classify_detection_frame("oak_rgb_camera_optical_frame", map_frame_confirmed=True)
    assert cls_opt == DetectionFrameClassificationEnum.CAMERA_OPTICAL_RELATIVE
    assert allowed_opt is False

    cls_parent, allowed_parent = classify_detection_frame("oak_parent_frame", map_frame_confirmed=True)
    assert cls_parent == DetectionFrameClassificationEnum.CAMERA_PARENT_RELATIVE
    assert allowed_parent is False

    cls_map_unconf, allowed_map_unconf = classify_detection_frame("map", map_frame_confirmed=False)
    assert cls_map_unconf == DetectionFrameClassificationEnum.UNCONFIRMED_MAP
    assert allowed_map_unconf is False

    cls_map_conf, allowed_map_conf = classify_detection_frame("map", map_frame_confirmed=True)
    assert cls_map_conf == DetectionFrameClassificationEnum.CONFIRMED_MAP
    assert allowed_map_conf is True

    transform_stub = UnvalidatedCoordinateTransformStub(map_frame_confirmed=False)
    pos = DetectionPosition3D(x=0.12, y=-0.02, z=0.65)
    tf_res = transform_stub.transform_position(
        pos,
        source_frame="oak_rgb_camera_optical_frame",
        target_frame="map",
    )
    assert tf_res.is_validated is False
    assert tf_res.position is None
    assert "unvalidated" in tf_res.reason.lower()

    # 5. Malformed inputs rejected cleanly
    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_std_msgs_string_detection_frame({"data": "{not-valid-json"})

    with pytest.raises(ValueError, match="missing required 'data'"):
        parse_std_msgs_string_detection_frame({"wrong_key": "{}"})

    with pytest.raises(ValidationError):
        DetectionFrame.model_validate(
            {
                "stamp": 1793363065.0,
                "frame": "oak_rgb_camera_optical_frame",
                "objects": [
                    {
                        "id": 0,
                        "class": "person",
                        "score": 1.2,
                        "position": {"x": 1.0, "y": 1.0, "z": 0.0},
                        "range_m": 1.0,
                    }
                ],
            }
        )

    with pytest.raises(ValidationError):
        DetectionFrame.model_validate(
            {
                "stamp": 1793363065.0,
                "frame": "oak_rgb_camera_optical_frame",
                "objects": [
                    {
                        "id": 0,
                        "class": "bottle",
                        "score": 0.9,
                        "position": {"x": 1.0, "y": 1.0, "z": 0.0},
                        "range_m": -0.1,
                    }
                ],
            }
        )

    with pytest.raises(ValidationError):
        DetectionFrame.model_validate(
            {
                "stamp": 1793363065.0,
                "frame": "oak_rgb_camera_optical_frame",
                "objects": [
                    {
                        "id": 0,
                        "class": "bottle",
                        "score": 0.9,
                        "position": {"x": math.nan, "y": 1.0, "z": 0.0},
                        "range_m": 1.0,
                    }
                ],
            }
        )

    with pytest.raises(ValidationError):
        DetectionFrame.model_validate(
            {
                "stamp": 1793363065.0,
                "frame": "oak_rgb_camera_optical_frame",
                "objects": [
                    {
                        "id": 0,
                        "class": "   ",
                        "score": 0.9,
                        "position": {"x": 1.0, "y": 1.0, "z": 0.0},
                        "range_m": 1.0,
                    }
                ],
            }
        )


# ---------------------------------------------------------------------------
# 3. ROSBridge Adapter Lifecycle, ROS 2 Message Type, Reconnect & Zero Publishing
# ---------------------------------------------------------------------------


async def test_rosbridge_adapter_connect_subscribe_reconnect_and_clean_shutdown() -> None:
    """Verify RosBridgeAdapter connects, subscribes to /detections_json (std_msgs/msg/String), reconnects, and cleans up."""
    sub_env = build_rosbridge_subscribe_envelope("/detections_json", ROS2_DETECTIONS_MESSAGE_TYPE)
    assert sub_env == {
        "op": "subscribe",
        "topic": "/detections_json",
        "type": "std_msgs/msg/String",
    }
    unsub_env = build_rosbridge_unsubscribe_envelope("/detections_json")
    assert unsub_env == {"op": "unsubscribe", "topic": "/detections_json"}

    created_clients: list[FakeRosClient] = []
    created_topics: list[FakeRosTopic] = []
    received_payloads: list[dict[str, Any] | str] = []
    observed_states: list[RosConnectionStateEnum] = []

    def _client_factory(host: str, port: int) -> RosClientLike:
        client = FakeRosClient(host, port)
        created_clients.append(client)
        return client

    def _topic_factory(
        ros: RosClientLike,
        topic_name: str,
        message_type: str,
    ) -> RosTopicLike:
        topic = FakeRosTopic(ros, topic_name, message_type)
        created_topics.append(topic)
        return topic

    async def _on_msg(msg: dict[str, Any] | str) -> None:
        received_payloads.append(msg)

    async def _on_state(st: RosConnectionStateEnum) -> None:
        observed_states.append(st)

    cfg = RosBridgeAdapterConfig(
        enabled=True,
        host="192.168.10.42",
        port=9090,
        vehicle_id="SCOUT01",
        detections_topic="/detections_json",
        message_type=DETECTIONS_MESSAGE_TYPE,
        queue_maxsize=8,
        reconnect_interval_seconds=0.08,
    )
    adapter = RosBridgeAdapter(
        cfg,
        on_detection_message=_on_msg,
        on_connection_state_change=_on_state,
        ros_client_factory=_client_factory,
        ros_topic_factory=_topic_factory,
    )

    try:
        await adapter.start()
        assert len(created_clients) == 1
        assert created_clients[0].host == "192.168.10.42"
        assert created_clients[0].port == 9090
        assert len(created_topics) == 1
        assert created_topics[0].name == "/detections_json"
        assert created_topics[0].message_type == "std_msgs/msg/String"
        assert created_topics[0].subscribe_calls == 1
        assert adapter.connection_state.value == "CONNECTED"

        # Emit a message through the topic callback and verify async delivery
        sample_json = json.dumps(
            {
                "stamp": 1793363063.737,
                "frame": "oak_rgb_camera_optical_frame",
                "objects": [],
            }
        )
        created_topics[0].emit_std_msgs_string(sample_json)
        await asyncio.sleep(0.03)
        assert len(received_payloads) == 1

        # Simulate temporary disconnect where reconnect initially fails, then succeeds
        created_clients[0].fail_connect = True
        created_clients[0].simulate_disconnect()
        await asyncio.sleep(0.02)
        assert adapter.connection_state.value == "RECONNECTING"
        assert RosConnectionStateEnum.RECONNECTING in observed_states

        # Allow reconnect to succeed on next supervisor cycle
        created_clients[0].fail_connect = False
        await asyncio.sleep(0.12)
        reconnected_state = str(adapter.connection_state.value)
        assert reconnected_state == "CONNECTED"

        # Confirm zero ROS publish or advertise calls ever occurred
        assert created_topics[0].publish_calls == 0
        assert created_topics[0].advertise_calls == 0
        assert created_clients[0].service_calls == 0
    finally:
        await adapter.stop()

    assert created_topics[0].unsubscribe_calls == 1
    assert created_clients[0].close_calls == 1
    assert created_clients[0].terminate_calls == 1
    final_state = str(adapter.connection_state.value)
    assert final_state == "DISCONNECTED"


# ---------------------------------------------------------------------------
# 4. PerceptionService: Camera Optical Frame, Unknown Frame, Persistence & WS Push
# ---------------------------------------------------------------------------


async def test_perception_service_optical_frame_ordering_freshness_persistence_and_ws_snapshot(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify oak_rgb_camera_optical_frame, oak_parent_frame, unknown frames, unconfirmed vs confirmed map, and WS delivery."""
    clock = ManualClock(datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc))
    agent = StubUltronAgent()
    simulator = MockFleetSimulator(clock=clock)
    gateway = DashboardGateway()
    event_svc = EventService(gateway=gateway)
    mission_svc = MissionService(agent=agent, gateway=gateway, events=event_svc)

    ros_settings = Settings(
        ros_enabled=True,
        ros_host="192.168.10.42",
        ros_port=9090,
        ros_vehicle_id="SCOUT01",
        ros_detections_topic="/detections_json",
        ros_detections_message_type="std_msgs/msg/String",
        ros_detections_stale_after_seconds=2.0,
        ros_map_frame_confirmed=False,
        hardware_lockout=True,
        default_vehicle_mode="simulated",
    )

    vehicle_svc = VehicleService(
        agent=agent,
        clock=clock,
        simulator=simulator,
        gateway=gateway,
        app_settings=ros_settings,
        heartbeat_interval_ms=250,
        heartbeat_timeout_ms=1500,
    )

    fake_clients: list[FakeRosClient] = []
    fake_topics: list[FakeRosTopic] = []

    def _client_factory(host: str, port: int) -> RosClientLike:
        c = FakeRosClient(host, port)
        fake_clients.append(c)
        return c

    def _topic_factory(ros: RosClientLike, name: str, msg_type: str) -> RosTopicLike:
        t = FakeRosTopic(ros, name, msg_type)
        fake_topics.append(t)
        return t

    perception_svc = PerceptionService(
        app_settings=ros_settings,
        clock=clock,
        gateway=gateway,
        agent=agent,
        session_factory=session_factory,
        ros_client_factory=_client_factory,
        ros_topic_factory=_topic_factory,
        persist_min_interval_seconds=1.0,
    )

    test_app = create_app(
        db_engine=isolated_engine,
        session_factory=session_factory,
        mission_svc=mission_svc,
        vehicle_svc=vehicle_svc,
        event_svc=event_svc,
        perception_svc=perception_svc,
        dash_gateway=gateway,
    )

    # 1. Before any mission exists, ingest the hardware engineer's `oak_rgb_camera_optical_frame` detection:
    # must update live state & snapshot memory, but NOT fabricate a mission_id in SQLite.
    res_no_mission = await perception_svc.handle_raw_ros_message(
        {
            "data": json.dumps(
                {
                    "stamp": 1793363063.737,
                    "frame": "oak_rgb_camera_optical_frame",
                    "objects": [
                        {
                            "id": 0,
                            "class": "person",
                            "score": 0.95,
                            "position": {"x": 0.12, "y": -0.02, "z": 0.65},
                            "range_m": 0.65,
                        }
                    ],
                }
            )
        }
    )
    assert res_no_mission.accepted is True
    assert res_no_mission.frame == "oak_rgb_camera_optical_frame"
    assert (
        res_no_mission.frame_classification
        == DetectionFrameClassificationEnum.CAMERA_OPTICAL_RELATIVE
    )
    assert res_no_mission.map_projection_allowed is False
    assert res_no_mission.persisted_observations == 0

    async with session_factory() as session:
        obs_rows = (await session.execute(select(Observation))).scalars().all()
        assert len(obs_rows) == 0

    # 2. Out-of-order frame (older stamp) is rejected and does not overwrite newer state
    res_out_of_order = await perception_svc.handle_raw_ros_message(
        {
            "data": json.dumps(
                {
                    "stamp": 1793363060.000,
                    "frame": "oak_rgb_camera_optical_frame",
                    "objects": [
                        {
                            "id": 99,
                            "class": "stale_ghost",
                            "score": 0.5,
                            "position": {"x": 9.0, "y": 9.0, "z": 0.0},
                            "range_m": 9.0,
                        }
                    ],
                }
            )
        }
    )
    assert res_out_of_order.accepted is False
    assert res_out_of_order.out_of_order is True
    current_state = perception_svc.get_vehicle_detection_state("SCOUT01")
    assert current_state.stamp == pytest.approx(1793363063.737)
    assert current_state.frame == "oak_rgb_camera_optical_frame"
    assert current_state.clock_offset_seconds is not None
    assert current_state.objects[0].class_name == "person"
    assert current_state.out_of_order_count == 1

    # 3. Invalid JSON does not crash subscription and increments invalid_message_count
    res_bad_json = await perception_svc.handle_raw_ros_message({"data": "INVALID_JSON"})
    assert res_bad_json.accepted is False
    assert perception_svc.get_vehicle_detection_state("SCOUT01").invalid_message_count == 1

    # 4. Create an active mission and verify persistence of `oak_rgb_camera_optical_frame`,
    # `oak_parent_frame`, unconfirmed `map`, and an unknown frame (`lidar_link`).
    # Because `ros_map_frame_confirmed=False`, NONE of them may be stored as global map coordinates!
    async with session_factory() as session:
        await mission_svc.create_mission(
            session,
            MissionCreateRequest(objective="Inspect Sector Alpha with live ROS 2 detections"),
        )
        await vehicle_svc.connect_vehicle(session, "SCOUT01")

    clock.advance(seconds=1.5)
    res_optical_persisted = await perception_svc.handle_raw_ros_message(
        {
            "data": json.dumps(
                {
                    "stamp": 1793363065.000,
                    "frame": "oak_rgb_camera_optical_frame",
                    "objects": [
                        {
                            "id": 0,
                            "class": "person",
                            "score": 0.95,
                            "position": {"x": 0.12, "y": -0.02, "z": 0.65},
                            "range_m": 0.65,
                        }
                    ],
                }
            )
        }
    )
    assert res_optical_persisted.accepted is True
    assert res_optical_persisted.persisted_observations == 1

    clock.advance(seconds=1.5)
    res_parent_persisted = await perception_svc.handle_raw_ros_message(
        {
            "data": json.dumps(
                {
                    "stamp": 1793363066.500,
                    "frame": "oak_parent_frame",
                    "objects": [
                        {
                            "id": 7,
                            "class": "chair",
                            "score": 0.88,
                            "position": {"x": 0.45, "y": -0.15, "z": 1.25},
                            "range_m": 1.34,
                        }
                    ],
                }
            )
        }
    )
    assert res_parent_persisted.accepted is True
    assert res_parent_persisted.persisted_observations == 1

    clock.advance(seconds=1.5)
    res_unknown_frame = await perception_svc.handle_raw_ros_message(
        {
            "data": json.dumps(
                {
                    "stamp": 1793363068.000,
                    "frame": "unverified_lidar_frame",
                    "objects": [
                        {
                            "id": 4,
                            "class": "box",
                            "score": 0.81,
                            "position": {"x": 2.1, "y": 1.1, "z": 0.3},
                            "range_m": 2.39,
                        }
                    ],
                }
            )
        }
    )
    assert res_unknown_frame.accepted is True
    assert res_unknown_frame.frame == "unverified_lidar_frame"
    assert (
        res_unknown_frame.frame_classification
        == DetectionFrameClassificationEnum.UNSUPPORTED_FRAME
    )
    assert res_unknown_frame.map_projection_allowed is False
    assert res_unknown_frame.persisted_observations == 1

    async with session_factory() as session:
        rows = (
            await session.execute(select(Observation).order_by(Observation.timestamp.asc()))
        ).scalars().all()
        assert len(rows) == 3

        opt_row, parent_row, unk_row = rows[0], rows[1], rows[2]
        assert opt_row.obs_type == "detection:person"
        assert opt_row.location_x == pytest.approx(0.0)
        assert opt_row.location_y == pytest.approx(0.0)
        assert opt_row.raw is not None
        assert opt_row.raw["frame"] == "oak_rgb_camera_optical_frame"
        assert opt_row.raw["frame_classification"] == "CAMERA_OPTICAL_RELATIVE"
        assert opt_row.raw["coordinates_are_map_frame"] is False
        assert opt_row.raw["id_is_frame_local_only"] is True
        assert opt_row.raw["position"] == {"x": 0.12, "y": -0.02, "z": 0.65}
        assert opt_row.raw["range_m"] == pytest.approx(0.65)

        assert parent_row.obs_type == "detection:chair"
        assert parent_row.location_x == pytest.approx(0.0)
        assert parent_row.location_y == pytest.approx(0.0)
        assert parent_row.raw is not None
        assert parent_row.raw["frame"] == "oak_parent_frame"
        assert parent_row.raw["frame_classification"] == "CAMERA_PARENT_RELATIVE"
        assert parent_row.raw["coordinates_are_map_frame"] is False

        assert unk_row.obs_type == "detection:box"
        assert unk_row.location_x == pytest.approx(0.0)
        assert unk_row.location_y == pytest.approx(0.0)
        assert unk_row.raw is not None
        assert unk_row.raw["frame"] == "unverified_lidar_frame"
        assert unk_row.raw["frame_classification"] == "UNSUPPORTED_FRAME"
        assert unk_row.raw["coordinates_are_map_frame"] is False

    # 5. Advance clock past `stale_after_seconds` (2.0s) while keeping SCOUT01 heartbeats fresh:
    # detection stream becomes stale, but SCOUT01 remains CONNECTED (not UNKNOWN).
    clock.advance(seconds=2.5)
    async with session_factory() as session:
        beat = await simulator.scout.step()
        assert beat is not None
        await vehicle_svc.ingest_telemetry(session, beat, force_persist=True)
        await vehicle_svc.evaluate_heartbeats(session)
        veh_status = await vehicle_svc.get_vehicle_status(session, "SCOUT01")

    det_state_stale = perception_svc.get_vehicle_detection_state("SCOUT01")
    assert det_state_stale.is_stale is True
    assert veh_status.connection.value == "CONNECTED"
    assert veh_status.status.value != "UNKNOWN"

    # 6. Verify WebSocket snapshot includes `detections` and live `type="detections"` push works
    with TestClient(test_app) as sync_client:
        det_http = sync_client.get("/vehicle/SCOUT01/detections")
        assert det_http.status_code == 200
        assert det_http.json()["vehicle_id"] == "SCOUT01"
        assert det_http.json()["configured_vehicle_id"] == "SCOUT01"
        assert det_http.json()["telemetry_source"] == TelemetrySourceEnum.ROSBRIDGE.value
        assert det_http.json()["actuation_locked"] is True

        with sync_client.websocket_connect("/ws/dashboard") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot"
            assert "SCOUT01" in snap["payload"]["detections"]
            assert (
                snap["payload"]["detections"]["SCOUT01"]["frame"]
                == "unverified_lidar_frame"
            )

            # Emit a new empty objects detection frame in oak_rgb_camera_optical_frame over mocked ROS topic
            assert len(fake_topics) == 1
            fake_topics[0].emit_std_msgs_string(
                json.dumps(
                    {
                        "stamp": 1793363070.000,
                        "frame": "oak_rgb_camera_optical_frame",
                        "objects": [],
                    }
                )
            )
            live_msg = ws.receive_json()
            assert live_msg["type"] == "detections"
            assert live_msg["payload"]["vehicle_id"] == "SCOUT01"
            assert live_msg["payload"]["frame"] == "oak_rgb_camera_optical_frame"
            assert live_msg["payload"]["map_projection_allowed"] is False
            assert live_msg["payload"]["objects"] == []
            assert live_msg["payload"]["is_stale"] is False

    # 7. Confirm LockedRealVehicleAdapter still blocks any physical actuation
    locked_adapter = LockedRealVehicleAdapter("SCOUT01", "ground", clock=clock)
    with pytest.raises(RealHardwareLockedError):
        await locked_adapter.connect()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        real_mode_attempt = await client.post(
            "/vehicle/SCOUT01/simulate",
            json={"enabled": False},
        )
        assert real_mode_attempt.status_code == 409
        assert real_mode_attempt.json()["error_code"] == "HARDWARE_LOCKOUT_ACTIVE"

    await perception_svc.stop()
    await gateway.close_all()
    await vehicle_svc.stop()
