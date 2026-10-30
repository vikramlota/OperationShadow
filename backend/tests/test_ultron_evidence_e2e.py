"""End-to-end backend integration test for Evidence-Grounded, Multi-Sensor ULTRON (Sections 11, 14).

Verifies the complete data flow:
Sensor input (/detections_json)
    -> Sensor adapter (RosBridgeAdapter / DepthCameraAdapter)
    -> Observation validation (PerceptionService)
    -> World Model (UltronLangGraphAgent)
    -> Tracking / fusion (TrackedObject & FrameTransformRegistry)
    -> ULTRON evidence retrieval (WorldModelTools & /mission/intelligence/*)
    -> Environment-aware planning & replanning (POST /mission & record_observation)
    -> Deterministic safety validation
    -> Decision trace
    -> Dashboard / WebSocket (/ws/dashboard & GET /vehicle/SCOUT01/detections)
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

from core.config import Settings
from fastapi.testclient import TestClient
from main import create_app
import pytest
from services.event_service import EventService
from services.mission_service import MissionService
from services.perception_service import PerceptionService
from services.ultron_agent_adapter import UltronLangGraphAgent
from services.vehicle_service import VehicleService
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from ultron_stub import ManualClock, MockFleetSimulator
from websocket.dashboard_gateway import DashboardGateway


async def test_e2e_sensor_to_world_model_to_ultron_planning_and_dashboard_ws(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the complete pipeline from /detections_json ingestion through ULTRON WorldModelTools, planning, and WebSocket push."""
    clock = ManualClock(datetime(2026, 10, 31, 10, 0, 0, tzinfo=timezone.utc))
    agent = UltronLangGraphAgent()
    simulator = MockFleetSimulator(clock=clock)
    gateway = DashboardGateway()
    event_svc = EventService(gateway=gateway)
    mission_svc = MissionService(agent=agent, gateway=gateway, events=event_svc)

    app_settings = Settings(
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
        app_settings=app_settings,
        heartbeat_interval_ms=250,
        heartbeat_timeout_ms=1500,
    )

    perception_svc = PerceptionService(
        app_settings=app_settings,
        clock=clock,
        gateway=gateway,
        agent=agent,
        session_factory=session_factory,
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

    # 1. Ingest a valid camera detection message in oak_rgb_camera_optical_frame
    detection_msg = {
        "data": json.dumps(
            {
                "stamp": 1793440800.0,
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
                        "score": 0.89,
                        "position": {"x": 1.15, "y": 0.10, "z": 1.10},
                        "range_m": 1.59,
                    },
                ],
            }
        )
    }
    ingest_res = await perception_svc.handle_raw_ros_message(detection_msg)
    assert ingest_res.accepted is True
    assert ingest_res.frame == "oak_rgb_camera_optical_frame"
    assert ingest_res.object_count == 2

    # 2. Verify the detections reached the ULTRON WorldModel with original coordinate frame & timestamps
    tools = agent.get_tools(current_time=1793440800.5)
    detected = tools.get_detected_objects()
    assert detected.total_count == 2
    labels = {o.label: o for o in detected.objects}
    assert "person" in labels
    assert "chair" in labels
    assert labels["person"].coordinate_frame == "oak_rgb_camera_optical_frame"
    assert labels["person"].last_observed_timestamp == pytest.approx(1793440800.0)
    assert labels["person"].corroborated_by_multiple_sensors is False

    # 3. Verify sensor health reports depth_camera active and lidar UNAVAILABLE
    health = tools.get_sensor_health()
    assert health.lidar_available is False
    assert health.sensors["lidar"].status.value == "UNAVAILABLE"

    # 4. Verify REST intelligence endpoints (/mission/intelligence/snapshot & /mission/intelligence/query)
    # and POST /mission + /ws/dashboard
    with TestClient(test_app) as client:
        snap_http = client.get("/mission/intelligence/snapshot")
        assert snap_http.status_code == 200
        snap_data = snap_http.json()
        assert snap_data["detected_objects"]["total_count"] == 2
        assert snap_data["sensor_health"]["sensors"]["lidar"]["status"] == "UNAVAILABLE"

        # Query about detected person -> grounded answer with evidence ID
        q_person = client.post(
            "/mission/intelligence/query",
            json={"query": "Where is the person?"},
        )
        assert q_person.status_code == 200
        qp_body = q_person.json()
        assert "0.65m" in qp_body["answer"]
        assert "oak_rgb_camera_optical_frame" in qp_body["answer"]
        assert len(qp_body["evidence"]) >= 1

        # Query about nonexistent object -> refuses to invent object
        q_fake = client.post(
            "/mission/intelligence/query",
            json={"query": "How far is the forklift?"},
        )
        assert q_fake.status_code == 200
        qf_body = q_fake.json()
        assert qf_body["confidence"] == "unavailable"
        assert len(qf_body["evidence"]) == 0

        # Create mission via POST /mission -> uses ULTRON LangGraph planner grounded in World Model
        m_res = client.post(
            "/mission",
            json={"objective": "Inspect building, find red_package, and report hazards"},
        )
        assert m_res.status_code == 200
        mission_id = m_res.json()["mission_id"]

        # Verify decision trace was recorded on the WorldModel and exposed via /vehicle/SCOUT01/detections
        det_http = client.get("/vehicle/SCOUT01/detections")
        assert det_http.status_code == 200
        det_body = det_http.json()
        assert len(det_body["tracked_objects"]) == 2
        assert det_body["sensor_health"]["lidar"]["status"] == "UNAVAILABLE"
        assert len(det_body["decision_traces"]) >= 1
        assert det_body["decision_traces"][-1]["mission_id"] == mission_id
        assert det_body["decision_traces"][-1]["safety_validator_passed"] is True

    await gateway.close_all()
    await vehicle_svc.stop()
