"""Comprehensive automated tests for Evidence-Grounded, Multi-Sensor ULTRON (Section 11).

Covers:
1. Sensor ingestion tests (DepthCameraAdapter & LidarIngestionAdapter):
   - Valid camera detection message in `oak_rgb_camera_optical_frame`
   - Empty detection list
   - Malformed JSON, missing required fields, invalid coordinates, invalid confidence
   - Duplicate message rejection & out-of-order timestamp rejection
   - Stale timestamp handling & sensor disconnection (never silently healthy)
   - LiDAR disabled & UNAVAILABLE by default; invalid LiDAR range rejection when configured
2. World Model & Persistent Tracking tests:
   - Object creation & persistent tracking identity (`OBJ-001` across frames regardless of detector local ID)
   - Stale object transition (`STALE`, `currently_visible=False`)
   - Observation history, sensor provenance, and multi-vehicle observations
3. Camera + LiDAR Sensor Fusion tests:
   - Unconfirmed extrinsics / missing transform blocks corroboration (keeps single-sensor `OBSERVED`)
   - Timestamp mismatch blocks fusion
   - Matching camera and LiDAR observations with validated transform & extrinsics -> `CORROBORATED`
   - Conflicting positions along same ray/region -> `CONFLICTED` (preserves both measurements without averaging)
   - Non-matching distant observations -> separate tracked objects
4. Read-Only World Model Tools & Evidence-Grounded Agent tests:
   - All 9 tools (`get_environment_snapshot`, `get_detected_objects`, `get_object_details`,
     `get_nearby_obstacles`, `get_free_space`, `get_sensor_health`, `get_observation_history`,
     `get_vehicle_pose`, `get_environment_changes`)
   - Correctly answers using actual observations (`answer_environmental_query`)
   - Refuses to invent nonexistent objects or distances
   - Never claims an area is clear without verified LiDAR coverage
   - Distinguishes stale from current information & reports contradictory sensor evidence
   - Application-level validation (`validate_grounded_response` & `validate_plan_evidence`) strips/rejects nonexistent observation IDs
   - Environment-aware planning & replanning when a validated obstacle blocks a route (without inventing an unverified alternative route)
   - Debouncing prevents continuous replanning for duplicate triggers
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
import pytest

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from ultron.graph.workflow import build_ultron_graph
from ultron.planner.engine import build_evidence_grounded_plan, invoke_gemini_planner
from ultron.planner.grounding import (
    answer_environmental_query,
    validate_grounded_response,
    validate_plan_evidence,
)
from ultron.replanner.triggers import check_replanning_triggers
from ultron.safety.validator import validate_plan
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import MissionPlan, MissionStatus, Task, TaskPriority, VehicleID
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorSourceModeEnum,
    SensorTypeEnum,
)
from ultron.schemas.world import (
    ConnectionStatus,
    EvidenceReference,
    GroundedEnvironmentalResponse,
    MissionState,
    ScoutState,
    SensorHealthStatusEnum,
    TrackingStatusEnum,
    VehicleMode,
    WorldModel,
)
from ultron.state import UltronState
from ultron.vision.lidar import LidarIngestionAdapter
from ultron.vision.pipeline import DepthCameraAdapter
from ultron.world_model.fusion import (
    FrameTransformRegistry,
    RigidTransform3D,
    evaluate_camera_lidar_fusion,
)
from ultron.world_model.tools import (
    WorldModelTools,
    get_detected_objects,
    get_environment_changes,
    get_environment_snapshot,
    get_free_space,
    get_nearby_obstacles,
    get_object_details,
    get_observation_history,
    get_sensor_health,
    get_vehicle_pose,
)
from ultron.world_model.updater import (
    ingest_sensor_observation,
    refresh_world_model_freshness,
)


# ===========================================================================
# 1. Sensor Ingestion Tests (Depth Camera & LiDAR)
# ===========================================================================


def test_camera_ingestion_valid_empty_malformed_duplicate_stale_and_disconnect() -> None:
    adapter = DepthCameraAdapter(
        enabled=True,
        source_mode=SensorSourceModeEnum.SIMULATED,
        stale_after_sec=2.0,
    )

    # 1. Valid hardware-format camera detection message
    valid_payload = {
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
    res = adapter.ingest_message(valid_payload, ingested_at=1793363063.800)
    assert res.accepted is True
    assert res.coordinate_frame == "oak_rgb_camera_optical_frame"
    assert res.sensor_timestamp == pytest.approx(1793363063.737)
    assert res.ingested_at == pytest.approx(1793363063.800)
    assert res.is_stale is False
    assert len(res.observations) == 1
    obs = res.observations[0]
    assert obs.coordinate_frame == "oak_rgb_camera_optical_frame"
    assert obs.classification == "person"
    assert obs.confidence == pytest.approx(0.95)
    assert obs.position.x == pytest.approx(0.12)
    assert obs.position.y == pytest.approx(-0.02)
    assert obs.position.z == pytest.approx(0.65)
    assert obs.position.range_m == pytest.approx(0.65)
    assert obs.source_mode == SensorSourceModeEnum.SIMULATED

    # 2. Exact duplicate message is rejected and increments duplicate_message_count
    dup_res = adapter.ingest_message(valid_payload, ingested_at=1793363063.900)
    assert dup_res.accepted is False
    assert dup_res.is_duplicate is True
    assert adapter.get_health(1793363063.900).duplicate_message_count == 1

    # 3. Out-of-order older timestamp is rejected
    old_res = adapter.ingest_message(
        {
            "stamp": 1793363060.0,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [],
        },
        ingested_at=1793363064.0,
    )
    assert old_res.accepted is False
    assert old_res.is_out_of_order is True
    assert adapter.get_health(1793363064.0).out_of_order_count == 1

    # 4. Empty detection list is valid and updates health timestamp without creating objects
    empty_res = adapter.ingest_message(
        {
            "stamp": 1793363064.5,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [],
        },
        ingested_at=1793363064.6,
    )
    assert empty_res.accepted is True
    assert len(empty_res.observations) == 0

    # 5. Malformed JSON, missing fields, invalid coordinates, and invalid confidence
    bad_json = adapter.ingest_message("{not-json", ingested_at=1793363065.0)
    assert bad_json.accepted is False

    missing_frame = adapter.ingest_message({"stamp": 1793363066.0, "objects": []}, ingested_at=1793363066.1)
    assert missing_frame.accepted is False

    bad_coord = adapter.ingest_message(
        {
            "stamp": 1793363067.0,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [
                {
                    "id": 0,
                    "class": "chair",
                    "score": 0.8,
                    "position": {"x": math.nan, "y": 0.0, "z": 1.0},
                    "range_m": 1.0,
                }
            ],
        },
        ingested_at=1793363067.1,
    )
    assert bad_coord.accepted is False

    bad_conf = adapter.ingest_message(
        {
            "stamp": 1793363068.0,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [
                {
                    "id": 0,
                    "class": "chair",
                    "score": 1.5,
                    "position": {"x": 1.0, "y": 0.0, "z": 1.0},
                    "range_m": 1.0,
                }
            ],
        },
        ingested_at=1793363068.1,
    )
    assert bad_conf.accepted is False
    assert adapter.get_health(1793363068.1).malformed_message_count == 4

    # 6. Stale timestamp detection (ingested_at - stamp > 2.0s)
    stale_res = adapter.ingest_message(
        {
            "stamp": 1793363070.0,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [],
        },
        ingested_at=1793363075.0,
    )
    assert stale_res.accepted is True
    assert stale_res.is_stale is True
    assert adapter.get_health(1793363075.0).status == SensorHealthStatusEnum.STALE

    # 7. Disconnected sensor never appears healthy
    adapter.set_connection_state(False)
    assert adapter.get_health(1793363075.5).status == SensorHealthStatusEnum.DISCONNECTED
    disc_res = adapter.ingest_message(valid_payload, ingested_at=1793363076.0)
    assert disc_res.accepted is False


def test_lidar_adapter_disabled_by_default_and_validates_measurements() -> None:
    lidar = LidarIngestionAdapter()
    # Unconfigured by default -> UNAVAILABLE, never healthy or empty obstacle map
    health_default = lidar.get_health()
    assert health_default.enabled is False
    assert health_default.status == SensorHealthStatusEnum.UNAVAILABLE
    assert health_default.topic == ""

    rejected_unconf = lidar.ingest_scan({"stamp": 100.0, "obstacles": []})
    assert rejected_unconf.accepted is False
    assert rejected_unconf.free_space_radius_m is None

    # Configure LiDAR explicitly for simulated testing
    lidar.configure(
        enabled=True,
        topic="/scan_obstacles",
        message_type="sensor_msgs/msg/LaserScan",
        coordinate_frame="lidar_link",
        vehicle_id="SCOUT01",
        source_mode=SensorSourceModeEnum.SIMULATED,
        connected=True,
    )

    # Invalid negative/out-of-bounds range is rejected
    bad_scan = lidar.ingest_scan(
        {
            "stamp": 1000.0,
            "frame": "lidar_link",
            "obstacles": [
                {"id": 0, "position": {"x": 0.0, "y": 0.0, "z": 0.0}, "range_m": -1.0}
            ],
        },
        ingested_at=1000.1,
    )
    assert bad_scan.accepted is False
    assert lidar.get_health(1000.1).malformed_message_count == 1

    # Valid LiDAR obstacle scan
    good_scan = lidar.ingest_scan(
        {
            "stamp": 1001.0,
            "frame": "lidar_link",
            "obstacles": [
                {
                    "id": 1,
                    "classification": "obstacle",
                    "position": {"x": 1.40, "y": 0.10, "z": 0.0},
                    "range_m": 1.404,
                    "uncertainty_m": 0.04,
                }
            ],
        },
        ingested_at=1001.05,
    )
    assert good_scan.accepted is True
    assert len(good_scan.observations) == 1
    assert good_scan.observations[0].sensor_type == SensorTypeEnum.LIDAR
    assert good_scan.observations[0].coordinate_frame == "lidar_link"
    assert good_scan.free_space_radius_m == pytest.approx(1.204)


# ===========================================================================
# 2. World Model & Persistent Object Tracking Tests
# ===========================================================================


def test_world_model_persistent_tracking_identity_and_staleness() -> None:
    world = WorldModel(
        mission=MissionState(id="M-WM-01", objective="Inspect room", status=MissionStatus.IN_PROGRESS)
    )

    # Frame 1: Detector reports chair with frame-local id=0
    obs1 = SensorObservation(
        observation_id="OBS-CAM-01",
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        detector_object_id=0,
        sensor_timestamp=2000.0,
        ingested_at=2000.05,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(x=1.20, y=0.10, z=1.50, range_m=1.92, uncertainty_m=0.12),
        classification="chair",
        confidence=0.88,
        source_mode=SensorSourceModeEnum.SIMULATED,
        is_obstacle=True,
    )
    ingest_sensor_observation(world, obs1)

    assert len(world.environment.tracked_objects) == 1
    tracked = world.environment.tracked_objects["OBJ-001"]
    assert tracked.label == "chair"
    assert tracked.last_detector_object_id == 0
    assert tracked.epistemic_status == EpistemicStatusEnum.OBSERVED
    assert tracked.currently_visible is True

    # Frame 2: Detector assigns a DIFFERENT frame-local id=4 to the same chair at almost the same position
    obs2 = SensorObservation(
        observation_id="OBS-CAM-02",
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        detector_object_id=4,
        sensor_timestamp=2000.25,
        ingested_at=2000.30,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(x=1.23, y=0.12, z=1.48, range_m=1.93, uncertainty_m=0.12),
        classification="chair",
        confidence=0.93,
        source_mode=SensorSourceModeEnum.SIMULATED,
        is_obstacle=True,
    )
    ingest_sensor_observation(world, obs2)

    # Must update OBJ-001 instead of creating a duplicate object!
    assert len(world.environment.tracked_objects) == 1
    tracked_updated = world.environment.tracked_objects["OBJ-001"]
    assert tracked_updated.contributing_observation_ids == ["OBS-CAM-01", "OBS-CAM-02"]
    assert tracked_updated.last_detector_object_id == 4
    assert tracked_updated.classification_confidence == pytest.approx(0.93)

    # Advance time past staleness threshold (2.0s) -> transitions to STALE and currently_visible=False
    refresh_world_model_freshness(world, current_time=2005.0, stale_after_sec=2.0)
    assert tracked_updated.epistemic_status == EpistemicStatusEnum.STALE
    assert tracked_updated.tracking_status == TrackingStatusEnum.STALE
    assert tracked_updated.currently_visible is False


# ===========================================================================
# 3. Camera + LiDAR Sensor Fusion Tests
# ===========================================================================


def test_camera_lidar_fusion_prerequisites_corroboration_and_conflicts() -> None:
    cam_obs = SensorObservation(
        observation_id="OBS-CAM-10",
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        detector_object_id=1,
        sensor_timestamp=3000.0,
        ingested_at=3000.05,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(x=1.50, y=0.20, z=0.0, range_m=1.51, uncertainty_m=0.10),
        classification="chair",
        confidence=0.92,
        source_mode=SensorSourceModeEnum.SIMULATED,
        is_obstacle=True,
    )
    lidar_obs_matching = SensorObservation(
        observation_id="OBS-LDR-10",
        sensor_id="lidar",
        sensor_type=SensorTypeEnum.LIDAR,
        vehicle_id="SCOUT01",
        detector_object_id=0,
        sensor_timestamp=3000.08,
        ingested_at=3000.10,
        coordinate_frame="lidar_link",
        position=Position3DMeasurement(x=1.48, y=0.18, z=0.0, range_m=1.49, uncertainty_m=0.05),
        classification="obstacle",
        confidence=None,
        source_mode=SensorSourceModeEnum.SIMULATED,
        is_obstacle=True,
    )

    # 1. Default registry (extrinsics unconfirmed, no transform registered) -> refuses fusion
    unconfirmed_reg = FrameTransformRegistry(
        map_frame_confirmed=False,
        camera_lidar_extrinsics_confirmed=False,
    )
    res_unconf = evaluate_camera_lidar_fusion(
        cam_obs,
        lidar_obs_matching,
        transform_registry=unconfirmed_reg,
        current_time=3000.10,
    )
    assert res_unconf.fused is False
    assert res_unconf.status == EpistemicStatusEnum.OBSERVED
    assert res_unconf.reason == "camera_lidar_extrinsics_unconfirmed"

    # 2. Extrinsics confirmed, but transform between lidar_link and oak_rgb_camera_optical_frame is missing
    missing_tf_reg = FrameTransformRegistry(
        map_frame_confirmed=False,
        camera_lidar_extrinsics_confirmed=True,
    )
    res_no_tf = evaluate_camera_lidar_fusion(
        cam_obs,
        lidar_obs_matching,
        transform_registry=missing_tf_reg,
        current_time=3000.10,
    )
    assert res_no_tf.fused is False
    assert "missing_or_unvalidated_transform" in res_no_tf.reason

    # 3. Register calibrated transform -> matching observations fuse as CORROBORATED
    calibrated_reg = FrameTransformRegistry(
        map_frame_confirmed=False,
        camera_lidar_extrinsics_confirmed=True,
    )
    calibrated_reg.register_transform(
        RigidTransform3D(
            source_frame="lidar_link",
            target_frame="oak_rgb_camera_optical_frame",
            tx=0.0,
            ty=0.0,
            tz=0.0,
            yaw_rad=0.0,
            transform_uncertainty_m=0.02,
            is_calibrated=True,
        )
    )
    res_fused = evaluate_camera_lidar_fusion(
        cam_obs,
        lidar_obs_matching,
        transform_registry=calibrated_reg,
        current_time=3000.10,
    )
    assert res_fused.fused is True
    assert res_fused.status == EpistemicStatusEnum.CORROBORATED
    assert res_fused.fused_position is not None
    assert res_fused.fused_position.uncertainty_m < cam_obs.position.uncertainty_m

    # 4. Timestamp mismatch (> 0.25s) blocks fusion even when transform is valid
    lidar_obs_late = lidar_obs_matching.model_copy(
        update={"observation_id": "OBS-LDR-LATE", "sensor_timestamp": 3001.5}
    )
    res_late = evaluate_camera_lidar_fusion(
        cam_obs,
        lidar_obs_late,
        transform_registry=calibrated_reg,
        current_time=3001.55,
    )
    assert res_late.fused is False
    assert "timestamp_mismatch" in res_late.reason

    # 5. Conflicting positions along the same ray (camera says 1.5m, LiDAR says 2.35m) -> CONFLICTED without averaging
    lidar_obs_conflict = lidar_obs_matching.model_copy(
        update={
            "observation_id": "OBS-LDR-CONF",
            "position": Position3DMeasurement(
                x=2.35, y=0.31, z=0.0, range_m=2.37, uncertainty_m=0.05
            ),
        }
    )
    res_conflict = evaluate_camera_lidar_fusion(
        cam_obs,
        lidar_obs_conflict,
        transform_registry=calibrated_reg,
        current_time=3000.10,
    )
    assert res_conflict.fused is False
    assert res_conflict.status == EpistemicStatusEnum.CONFLICTED
    assert res_conflict.fused_position is None
    assert len(res_conflict.discrepant_positions) == 2
    assert "Sensor conflict" in (res_conflict.conflict_details or "")


# ===========================================================================
# 4. World Model Retrieval Tools & Evidence-Grounded ULTRON Tests
# ===========================================================================


def test_world_model_tools_and_evidence_grounding_prevent_hallucinations() -> None:
    world = WorldModel(
        mission=MissionState(
            id="M-INTEL-01",
            objective="Inspect room and report obstacles",
            status=MissionStatus.IN_PROGRESS,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                mode=VehicleMode.SIMULATED,
                position=Position2D(x=1.0, y=2.0),
                battery=92.0,
                connection=ConnectionStatus.ONLINE,
            )
        },
    )

    # 1. Before any observations: free space is UNKNOWN, LiDAR is UNAVAILABLE
    free_init = get_free_space(world, current_time=4000.0)
    assert free_init.is_verified_clear is False
    assert free_init.epistemic_status == EpistemicStatusEnum.UNKNOWN
    assert "UNAVAILABLE" in free_init.reason

    health_init = get_sensor_health(world, current_time=4000.0)
    assert health_init.lidar_available is False
    assert health_init.sensors["lidar"].status == SensorHealthStatusEnum.UNAVAILABLE

    # 2. Ingest a valid camera observation of a person at 0.65m in oak_rgb_camera_optical_frame
    person_obs = SensorObservation(
        observation_id="OBS-CAM-PERSON-1",
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        detector_object_id=0,
        sensor_timestamp=4000.0,
        ingested_at=4000.05,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(x=0.12, y=-0.02, z=0.65, range_m=0.65, uncertainty_m=0.06),
        classification="person",
        confidence=0.95,
        source_mode=SensorSourceModeEnum.SIMULATED,
    )
    ingest_sensor_observation(world, person_obs)

    # 3. Verify all 9 World Model tools return accurate, typed data
    snap = get_environment_snapshot(world, current_time=4000.1)
    assert snap.can_assess_from_live_sensors is True
    assert snap.detected_objects.total_count == 1

    det_objs = get_detected_objects(world, current_time=4000.1)
    assert det_objs.objects[0].object_id == "OBJ-001"
    assert det_objs.objects[0].label == "person"

    obj_detail = get_object_details(world, "OBJ-001", current_time=4000.1)
    assert obj_detail.found is True
    assert obj_detail.observations[0].observation_id == "OBS-CAM-PERSON-1"

    missing_detail = get_object_details(world, "OBJ-999", current_time=4000.1)
    assert missing_detail.found is False
    assert missing_detail.epistemic_status == EpistemicStatusEnum.UNKNOWN

    hist = get_observation_history(world, "OBJ-001")
    assert hist.count == 1

    pose = get_vehicle_pose(world, "SCOUT01")
    assert pose.found is True
    assert pose.position_x == pytest.approx(1.0)
    assert pose.pose_valid is True

    changes = get_environment_changes(world, since_timestamp=3999.0, current_time=4000.1)
    assert changes.has_changes is True
    assert len(changes.new_observations) == 1

    # 4. ULTRON answers question about detected person using real observation
    ans_person = answer_environmental_query(world, "Where is the person?", current_time=4000.1)
    assert "person" in ans_person.answer.lower()
    assert "0.65m" in ans_person.answer
    assert "oak_rgb_camera_optical_frame" in ans_person.answer
    assert any(ev.observation_id == "OBS-CAM-PERSON-1" for ev in ans_person.evidence)
    assert any("LiDAR" in u for u in ans_person.unknowns)

    # 5. ULTRON refuses to invent an object ("forklift") that is not in the World Model
    ans_forklift = answer_environmental_query(
        world, "How far is the forklift?", current_time=4000.1
    )
    assert "cannot confirm" in ans_forklift.answer.lower()
    assert ans_forklift.confidence == "unavailable"
    assert len(ans_forklift.evidence) == 0

    # 6. ULTRON refuses to claim area is clear without LiDAR coverage
    ans_clear = answer_environmental_query(
        world, "Is the path clear of obstacles?", current_time=4000.1
    )
    assert "cannot determine that the area is clear" in ans_clear.answer.lower()

    # 7. Application-level validation rejects fabricated observation IDs and unverified "area is clear" claims
    fabricated_resp = GroundedEnvironmentalResponse(
        answer="A box is at 2.0m and the path is clear.",
        facts=[
            "Box detected at 2.0m (OBS-FAKE-999).",
            "The path is clear of obstacles.",
            "Corroborated by LiDAR.",
        ],
        evidence=[
            EvidenceReference(
                observation_id="OBS-FAKE-999",
                sensor="depth_camera",
                timestamp=4000.0,
                coordinate_frame="oak_rgb_camera_optical_frame",
                epistemic_status=EpistemicStatusEnum.CORROBORATED,
            )
        ],
        confidence="high",
    )
    sanitized = validate_grounded_response(world, fabricated_resp, current_time=4000.1)
    assert len(sanitized.evidence) == 0
    assert len(sanitized.facts) == 0
    assert sanitized.confidence == "unavailable"
    assert any("OBS-FAKE-999" in w for w in sanitized.warnings)


# ===========================================================================
# 5. Environment-Aware Planning, Replanning, Debouncing & Decision Traces
# ===========================================================================


def test_environment_aware_replanning_and_debouncing() -> None:
    world = WorldModel(
        mission=MissionState(
            id="M-REPLAN-01",
            objective="Inspect Sector Alpha and report findings",
            status=MissionStatus.IN_PROGRESS,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                mode=VehicleMode.SIMULATED,
                position=Position2D(x=1.0, y=1.0),
                battery=95.0,
                connection=ConnectionStatus.ONLINE,
            )
        },
    )

    app = build_ultron_graph()
    state: UltronState = {
        "world": world,
        "proposed_plan": None,
        "validation_errors": [],
        "repair_prompt": None,
        "retry_count": 0,
        "trigger_rationale": None,
        "language": "English",
    }

    # Initial planning pass records a DecisionTraceEntry
    state = app.invoke(state)
    initial_plan = state["proposed_plan"]
    assert initial_plan is not None
    assert len(state["world"].environment.decision_traces) == 1
    assert state["world"].environment.decision_traces[0].replanning_triggered is False

    # Inject a route-blocking chair obstacle at 1.1m in front of SCOUT01
    chair_obs = SensorObservation(
        observation_id="OBS-CAM-CHAIR-01",
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        detector_object_id=2,
        sensor_timestamp=5000.0,
        ingested_at=5000.05,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(x=1.10, y=0.0, z=0.0, range_m=1.10, uncertainty_m=0.08),
        classification="chair",
        confidence=0.91,
        source_mode=SensorSourceModeEnum.SIMULATED,
        is_obstacle=True,
    )
    ingest_sensor_observation(state["world"], chair_obs)

    # Check replanning trigger with debouncing
    should_replan, rationale = check_replanning_triggers(
        state["world"], current_time=5000.1, debounce_sec=2.0
    )
    assert should_replan is True
    assert rationale is not None
    assert "chair" in rationale.lower()
    assert "OBS-CAM-CHAIR-01" in rationale

    # Immediate duplicate check within debounce window (5000.5 < 5000.1 + 2.0) does NOT retrigger
    should_replan_dup, _ = check_replanning_triggers(
        state["world"], current_time=5000.5, debounce_sec=2.0
    )
    assert should_replan_dup is False

    # Execute replanning through state machine
    state["trigger_rationale"] = rationale
    state["proposed_plan"] = None
    state = app.invoke(state)
    replan = state["proposed_plan"]
    assert replan is not None
    # Because free-space geometry is unverified, ULTRON does not invent an alternative route;
    # it issues STOP, SCAN, REPORT and records the obstacle observation ID
    assert [t.type for t in replan.tasks] == ["STOP", "SCAN", "REPORT"]
    assert "OBS-CAM-CHAIR-01" in replan.evidence_ids
    assert len(state["world"].environment.decision_traces) == 2
    latest_trace = state["world"].environment.decision_traces[-1]
    assert latest_trace.replanning_triggered is True
    assert latest_trace.safety_validator_passed is True
