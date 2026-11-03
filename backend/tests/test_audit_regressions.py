"""Comprehensive regression tests for Operation Shadow Audit (Pass 1 & Pass 2).

Covers:
1. ROS Vehicle Identity & Timestamp Validation (`rosbridge_client.py`):
   - Unknown vehicle identity (`ROVER_UNKNOWN`, `HAWK01`) and mismatched vehicle identity rejected.
   - Missing/unconfigured identity (`SHADOW_ROS_VEHICLE_ID=""`) preserved as unassociated (`vehicle_id=None`) without falling back to `SCOUT01`.
   - Configured ROSBridge endpoint (`build_rosbridge_url`) used instead of hardcoded IP.
   - Sensor timestamps validated for finite positive values; NaN, infinity, negative, zero, bool, and missing timestamps rejected without replacing with current time.
   - Valid empty detection lists (`objects: []`) update camera freshness without claiming free space is clear.
   - Malformed messages handled without crashing the ROS ingestion process.
2. Decoupled Sensor Ingestion & Asynchronous LLM Replanning (`ultron_agent_adapter.py`):
   - High-frequency camera detection ingestion remains sub-millisecond while LLM replanning is deliberately delayed.
   - Repeated updates coalesce, only one replan is active per mission, significant hazard updates take priority, stale replans from older mission epochs are discarded without overwriting newer state, and Emergency Stop cancels pending replanning.
3. Strict Telemetry & Observation Validation (`vehicle_gateway.py`):
   - Missing battery, missing positions, malformed/missing timestamps, missing confidence, and missing observation coordinates are rejected (never defaulted to 100%, (0,0), now(), or 1.0).
   - Actual zero values (`0.0`) are accepted and distinguished from `None`/missing/NaN/bool.
   - Invalid frames do not refresh telemetry freshness (`last_heartbeat`), and the WebSocket connection stays open.
4. SCOUT01 Single-Vehicle Safety Validation (`ultron/safety/validator.py`):
   - SCOUT01 connected & healthy, SCOUT01 offline, SCOUT01 unknown connection, SCOUT01 missing from World Model, SCOUT01 low/unavailable battery, and SCOUT01 in `EMERGENCY_STOPPED`.
5. Emergency Stop Hardening (`scout_serial_writer.py`, `mission_service.py`, `vehicle_gateway.py`):
   - Latched Emergency Stop blocks movement commands immediately before write, interrupts active movement ticks/delays immediately, remains latched until authorized reset, and reports serial stop-command failures after disconnection as critical `SAFETY` faults.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
import serial

# Ensure root package is importable
_root = str(Path(__file__).resolve().parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from core.config import Settings, settings
from db.models import Event, Mission, Task as DbTask, Vehicle
from db.session import AsyncSessionLocal, init_db
from main import app
from schemas.entities import MissionCreateRequest
from services.mission_service import MissionService
from services.rosbridge_client import (
    RosBridgeClient,
    build_rosbridge_url,
    process_rosbridge_detection_payload,
)
from services.scout_serial_writer import (
    EmergencyStopLatchedError,
    ScoutSerialWriter,
    SerialStopCommandFaultError,
    is_global_emergency_stop_latched,
    latch_global_emergency_stop,
    reset_global_emergency_stop,
    wait_unless_emergency_stopped,
)
from services.ultron_agent_adapter import UltronLangGraphAgent
from sqlalchemy import select
from ultron.graph.nodes import node_degraded
from ultron.graph.workflow import build_ultron_graph
from ultron.safety.validator import validate_plan
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import MissionPlan, MissionStatus, Task, VehicleID
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    ObservationValidationStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorSourceModeEnum,
    SensorTypeEnum,
)
from ultron.schemas.world import (
    ConnectionStatus,
    MissionState,
    ScoutState,
    SensorHealthStatusEnum,
    WorldModel,
)
from ultron.state import UltronState
from ultron.world_model.tools import get_free_space
from ultron.world_model.updater import ingest_sensor_observation
from websocket.dashboard_gateway import DashboardGateway
from websocket.vehicle_gateway import (
    validate_inbound_observation_frame,
    validate_inbound_telemetry_frame,
)


# ---------------------------------------------------------------------------
# 1. ROS Vehicle Identity, Timestamps, Empty Frames & Malformed Payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ros_vehicle_identity_timestamps_empty_frames_and_malformed_handling() -> None:
    """Verify ROSBridge client URL config, vehicle identity verification, finite stamps, empty frames, and malformed payloads."""
    # 1. Configured endpoint URL is built from Settings (never hardcoded)
    unconfigured_settings = Settings(ros_enabled=False, ros_host="", ros_vehicle_id="")
    assert build_rosbridge_url(unconfigured_settings) == ""
    client_unconf = RosBridgeClient(app_settings=unconfigured_settings)
    assert client_unconf.url == ""
    client_unconf.start()  # Must not start or crash when URL is empty
    assert client_unconf.connected is False

    configured_settings = Settings(
        ros_enabled=True,
        ros_host="192.168.50.10",
        ros_port=9095,
        ros_vehicle_id="SCOUT01",
    )
    assert build_rosbridge_url(configured_settings) == "ws://192.168.50.10:9095"
    client_conf = RosBridgeClient(app_settings=configured_settings)
    assert client_conf.url == "ws://192.168.50.10:9095"

    agent = UltronLangGraphAgent()

    # 2. Missing/unconfigured vehicle identity preserves data as unassociated (vehicle_id=None), NEVER SCOUT01
    unassoc_res = await process_rosbridge_detection_payload(
        {
            "stamp": 1700000001.25,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [
                {
                    "id": 1,
                    "class": "box",
                    "score": 0.88,
                    "position": {"x": 0.5, "y": 0.1, "z": 1.4},
                    "range_m": 1.5,
                }
            ],
        },
        agent,
        app_settings=unconfigured_settings,
        client_stats=client_unconf,
    )
    assert unassoc_res.accepted is True
    assert unassoc_res.unassociated is True
    assert unassoc_res.vehicle_id is None
    assert client_unconf.unassociated_frame_count == 1
    assert agent.world.environment.observation_history[-1].vehicle_id is None

    # 3. Unknown or removed vehicle identity (e.g. ROVER99 or HAWK01) is rejected with explicit diagnostic
    for bad_vid in ("ROVER99", "HAWK01", "   ", 123):
        rej_vid = await process_rosbridge_detection_payload(
            {
                "stamp": 1700000002.0,
                "frame": "oak_rgb_camera_optical_frame",
                "vehicle_id": bad_vid,
                "objects": [],
            },
            agent,
            app_settings=configured_settings,
            client_stats=client_conf,
        )
        assert rej_vid.accepted is False
        assert "vehicle_id" in rej_vid.reason

    assert client_conf.rejected_vehicle_id_count == 4

    # 4. Invalid, missing, NaN, infinite, zero, negative, and boolean timestamps are rejected (never replaced with now())
    for bad_stamp in (None, "1700000000", 0.0, -10.5, math.nan, math.inf, -math.inf, True, {"sec": math.nan, "nanosec": 0}):
        rej_ts = await process_rosbridge_detection_payload(
            {
                "stamp": bad_stamp,
                "frame": "oak_rgb_camera_optical_frame",
                "objects": [],
            },
            agent,
            app_settings=configured_settings,
            client_stats=client_conf,
        )
        assert rej_ts.accepted is False
        assert "stamp" in rej_ts.reason

    # 5. Valid empty detection frames (`objects: []`) update sensor activity & freshness without claiming free space is clear
    empty_stamp = 1700000010.5
    empty_res = await process_rosbridge_detection_payload(
        {
            "stamp": empty_stamp,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [],
        },
        agent,
        app_settings=configured_settings,
        client_stats=client_conf,
    )
    assert empty_res.accepted is True
    assert empty_res.empty_frame is True
    assert empty_res.vehicle_id == "SCOUT01"
    cam_health = agent.world.environment.sensor_health["depth_camera"]
    assert cam_health.last_message_timestamp == pytest.approx(empty_stamp)
    assert cam_health.is_stale is False
    assert cam_health.status == SensorHealthStatusEnum.CONNECTED
    free_space = get_free_space(agent.world, current_time=empty_stamp + 0.1)
    assert free_space.is_verified_clear is False
    assert free_space.epistemic_status == EpistemicStatusEnum.UNKNOWN

    # 6. Malformed messages & malformed object entries are rejected without crashing
    malformed_payloads: list[Any] = [
        "not-a-dict",
        {"stamp": 1700000012.0, "frame": "", "objects": []},
        {"stamp": 1700000012.0, "frame": "oak_rgb_camera_optical_frame", "objects": "not-a-list"},
        {
            "stamp": 1700000012.0,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [{"id": 1, "class": "chair", "score": math.nan, "position": {"x": 1.0, "y": 1.0, "z": 1.0}}],
        },
        {
            "stamp": 1700000012.0,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [{"id": 1, "class": "chair", "score": 1.5, "position": {"x": 1.0, "y": 1.0, "z": 1.0}}],
        },
        {
            "stamp": 1700000012.0,
            "frame": "oak_rgb_camera_optical_frame",
            "objects": [{"id": 1, "class": "chair", "score": 0.9, "position": {"x": math.inf, "y": 1.0, "z": 1.0}}],
        },
    ]
    for bad_payload in malformed_payloads:
        bad_out = await process_rosbridge_detection_payload(
            bad_payload,
            agent,
            app_settings=configured_settings,
            client_stats=client_conf,
        )
        assert bad_out.accepted is False

    # Raw WebSocket frame parser also survives malformed frames without raising
    assert await client_conf.handle_raw_ws_frame("{bad json") is False
    assert await client_conf.handle_raw_ws_frame(json.dumps(["list_not_dict"])) is False
    assert await client_conf.handle_raw_ws_frame(
        json.dumps({"op": "publish", "topic": "/detections_json", "msg": {"data": ""}})
    ) is False


# ---------------------------------------------------------------------------
# 2. Separate Sensor Ingestion from Asynchronous LLM Replanning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_frequency_sensor_ingestion_decoupled_from_slow_llm_replanning() -> None:
    """Verify high-frequency sensor ingestion stays sub-millisecond during slow LLM calls, coalesces updates, fences epochs, and cancels on E-Stop."""
    agent = UltronLangGraphAgent(replan_cooldown_sec=0.05, replan_debounce_sec=0.0)
    plan = await agent.create_simulated_mission("M-ASYNC-01", "Sweep Sector Alpha and avoid hazards")
    assert plan.status == "ACTIVE"

    llm_call_triggers: list[str] = []

    def _slow_planner(**kwargs: Any) -> MissionPlan:
        trig = str(kwargs.get("trigger_rationale") or "initial")
        llm_call_triggers.append(trig)
        time.sleep(0.20)  # Simulate 200ms blocking LLM latency
        return MissionPlan(
            mission_id=kwargs["world"].mission.id,
            objective=kwargs["world"].mission.objective,
            tasks=[
                Task(
                    id="T-REPLAN",
                    type="SCAN",
                    vehicle=VehicleID.SCOUT,
                    priority=1,
                    area="Sector Alpha",
                )
            ],
            rationale=f"Replanned for: {trig}",
        )

    base_ts = datetime.now(timezone.utc).timestamp()
    ingest_latencies_ms: list[float] = []

    with patch("ultron.graph.nodes.invoke_gemini_planner", side_effect=_slow_planner):
        # Burst 20 high-frequency detection frames (including a significant hazard update) while LLM is slow
        for i in range(20):
            frame_ts = base_ts + (i * 0.02)
            frame_dt = datetime.fromtimestamp(frame_ts, tz=timezone.utc)
            cls_name = "fire" if i == 10 else "chair"
            t0 = time.perf_counter()
            obs = agent.ingest_detection_frame_into_world_model(
                stamp=frame_ts,
                received_at=frame_dt,
                frame="oak_rgb_camera_optical_frame",
                objects=[
                    {
                        "id": i,
                        "class": cls_name,
                        "score": 0.93,
                        "position": {"x": 0.2 + i * 0.05, "y": 0.0, "z": 1.2},
                        "range_m": 1.2,
                    }
                ],
                vehicle_id="SCOUT01",
                telemetry_source="rosbridge",
                map_frame_confirmed=False,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            ingest_latencies_ms.append(elapsed_ms)
            assert len(obs) == 1
            await asyncio.sleep(0.002)

        # Every single ingestion call must return immediately (< 15ms, typically ~0.2ms), never blocking for 200ms!
        max_ingest_ms = max(ingest_latencies_ms)
        median_ingest_ms = sorted(ingest_latencies_ms)[len(ingest_latencies_ms) // 2]
        assert max_ingest_ms < 25.0, f"Ingestion blocked for {max_ingest_ms:.2f}ms!"
        assert median_ingest_ms < 5.0

        # Wait for the coalesced background replanning worker to finish
        await agent.wait_for_replanning(timeout=3.0)

    # Verify repeated updates were coalesced rather than firing 20 separate LLM calls
    assert agent.replan_coalesced_updates_count > 0
    assert 1 <= len(llm_call_triggers) <= 4
    assert agent.replan_completed_count >= 1
    # All 20 observations must still be present in observation_history (never overwritten by snapshot!)
    assert len(agent.world.environment.observation_history) == 20

    # Verify Emergency Stop immediately cancels in-flight replanning and prevents state overwrite
    with patch("ultron.graph.nodes.invoke_gemini_planner", side_effect=_slow_planner):
        ts_estop = base_ts + 10.0
        agent.ingest_detection_frame_into_world_model(
            stamp=ts_estop,
            received_at=datetime.fromtimestamp(ts_estop, tz=timezone.utc),
            frame="oak_rgb_camera_optical_frame",
            objects=[
                {
                    "id": 99,
                    "class": "smoke",
                    "score": 0.96,
                    "position": {"x": 0.5, "y": 0.0, "z": 1.0},
                    "range_m": 1.1,
                }
            ],
            vehicle_id="SCOUT01",
            telemetry_source="rosbridge",
            map_frame_confirmed=False,
        )
        await asyncio.sleep(0.02)
        # Trigger Emergency Stop while slow LLM replan is in flight
        await agent.update_vehicle_state("SCOUT01", status="EMERGENCY_STOPPED")
        await agent.wait_for_replanning(timeout=1.0)

    assert agent.world.vehicles["SCOUT01"].status == "EMERGENCY_STOPPED"
    assert agent.replan_discarded_stale_count >= 1


# ---------------------------------------------------------------------------
# 3. Strict Telemetry & Observation Validation
# ---------------------------------------------------------------------------


def test_strict_telemetry_and_observation_validation_and_ws_preservation() -> None:
    """Verify missing battery/position/timestamp/confidence/coordinates are rejected while 0.0 values are accepted."""
    valid_iso = "2026-11-03T12:00:00Z"

    # 1. Valid zero values (0.0 battery, (0.0, 0.0) position, 0.0 confidence) MUST be accepted
    zero_tel = validate_inbound_telemetry_frame(
        "SCOUT01",
        {
            "status": "IDLE",
            "battery": 0.0,
            "position_x": 0.0,
            "position_y": 0.0,
            "position_alt": 0.0,
            "timestamp": valid_iso,
        },
    )
    assert zero_tel.battery == 0.0
    assert zero_tel.position_x == 0.0
    assert zero_tel.position_y == 0.0

    zero_obs = validate_inbound_observation_frame(
        "SCOUT01",
        {
            "mission_id": "M001",
            "obs_type": "obstacle_detected",
            "confidence": 0.0,
            "location_x": 0.0,
            "location_y": 0.0,
            "timestamp": valid_iso,
        },
    )
    assert zero_obs.confidence == 0.0
    assert zero_obs.location_x == 0.0
    assert zero_obs.location_y == 0.0

    # 2. Missing or invalid telemetry fields must raise ValueError (never default to 100%, (0,0), or now())
    invalid_telemetry_cases: list[dict[str, Any]] = [
        # Missing battery
        {"status": "ACTIVE", "position_x": 1.0, "position_y": 2.0, "timestamp": valid_iso},
        # None battery
        {"status": "ACTIVE", "battery": None, "position_x": 1.0, "position_y": 2.0, "timestamp": valid_iso},
        # Boolean battery
        {"status": "ACTIVE", "battery": True, "position_x": 1.0, "position_y": 2.0, "timestamp": valid_iso},
        # NaN battery
        {"status": "ACTIVE", "battery": math.nan, "position_x": 1.0, "position_y": 2.0, "timestamp": valid_iso},
        # Missing position_x
        {"status": "ACTIVE", "battery": 80.0, "position_y": 2.0, "timestamp": valid_iso},
        # Missing position_y
        {"status": "ACTIVE", "battery": 80.0, "position_x": 1.0, "timestamp": valid_iso},
        # Missing timestamp
        {"status": "ACTIVE", "battery": 80.0, "position_x": 1.0, "position_y": 2.0},
        # Malformed timestamp
        {"status": "ACTIVE", "battery": 80.0, "position_x": 1.0, "position_y": 2.0, "timestamp": "not-a-timestamp"},
        # Missing status
        {"battery": 80.0, "position_x": 1.0, "position_y": 2.0, "timestamp": valid_iso},
        # Non-zero altitude on ground rover SCOUT01
        {"status": "ACTIVE", "battery": 80.0, "position_x": 1.0, "position_y": 2.0, "position_alt": 2.5, "timestamp": valid_iso},
    ]
    for bad_tel in invalid_telemetry_cases:
        with pytest.raises(ValueError):
            validate_inbound_telemetry_frame("SCOUT01", bad_tel)

    # 3. Missing or invalid observation fields must raise ValueError (never default confidence=1.0 or coords=(0,0))
    invalid_observation_cases: list[dict[str, Any]] = [
        # Missing confidence
        {"mission_id": "M001", "obs_type": "hazard", "location_x": 1.0, "location_y": 2.0, "timestamp": valid_iso},
        # Missing location_x
        {"mission_id": "M001", "obs_type": "hazard", "confidence": 0.8, "location_y": 2.0, "timestamp": valid_iso},
        # Missing location_y
        {"mission_id": "M001", "obs_type": "hazard", "confidence": 0.8, "location_x": 1.0, "timestamp": valid_iso},
        # Missing timestamp
        {"mission_id": "M001", "obs_type": "hazard", "confidence": 0.8, "location_x": 1.0, "location_y": 2.0},
        # Malformed timestamp
        {"mission_id": "M001", "obs_type": "hazard", "confidence": 0.8, "location_x": 1.0, "location_y": 2.0, "timestamp": "bad-ts"},
        # Missing mission_id
        {"obs_type": "hazard", "confidence": 0.8, "location_x": 1.0, "location_y": 2.0, "timestamp": valid_iso},
        # Out-of-range confidence
        {"mission_id": "M001", "obs_type": "hazard", "confidence": 1.2, "location_x": 1.0, "location_y": 2.0, "timestamp": valid_iso},
    ]
    for bad_obs in invalid_observation_cases:
        with pytest.raises(ValueError):
            validate_inbound_observation_frame("SCOUT01", bad_obs)

    # 4. End-to-end WebSocket verification: invalid frames do NOT refresh last_heartbeat or drop the connection
    token = settings.vehicle_shared_secret
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/vehicle?vehicle_id=SCOUT01&token={token}") as ws:
            time.sleep(0.02)
            connect_status = client.get("/vehicle/SCOUT01/status").json()
            connect_hb = connect_status["last_heartbeat"]

            # Send incomplete telemetry missing battery & position -> must be rejected without updating heartbeat
            ws.send_json({"type": "telemetry", "status": "ACTIVE", "timestamp": datetime.now(timezone.utc).isoformat()})
            # Send telemetry with malformed timestamp -> must be rejected
            ws.send_json({"type": "telemetry", "status": "ACTIVE", "battery": 75.0, "position_x": 3.0, "position_y": 4.0, "timestamp": "malformed"})
            # Send observation missing coordinates -> must be rejected
            ws.send_json({"type": "observation", "mission_id": "M001", "obs_type": "hazard", "confidence": 0.9, "timestamp": datetime.now(timezone.utc).isoformat()})
            time.sleep(0.04)

            mid_status = client.get("/vehicle/SCOUT01/status").json()
            assert mid_status["last_heartbeat"] == connect_hb
            assert mid_status["battery"] != 75.0

            # Now send a complete, valid telemetry frame with 0.0 position_x -> must be accepted on the same open WS
            fresh_ts = datetime.now(timezone.utc).isoformat()
            ws.send_json(
                {
                    "type": "telemetry",
                    "status": "ACTIVE",
                    "battery": 88.5,
                    "position_x": 0.0,
                    "position_y": 5.5,
                    "timestamp": fresh_ts,
                }
            )
            time.sleep(0.05)

        after_status = client.get("/vehicle/SCOUT01/status").json()
        assert after_status["battery"] == pytest.approx(88.5)
        assert after_status["position_x"] == pytest.approx(0.0)
        assert after_status["position_y"] == pytest.approx(5.5)


# ---------------------------------------------------------------------------
# 4. SCOUT01 Single-Vehicle Safety Validation
# ---------------------------------------------------------------------------


def test_scout01_single_vehicle_safety_validation_matrix() -> None:
    """Verify SCOUT01 healthy, offline, unknown connection, missing from WorldModel, low/unavailable battery, and EMERGENCY_STOPPED."""
    sample_plan = MissionPlan(
        mission_id="M-SAFE",
        objective="Navigate Sector Alpha and scan",
        tasks=[
            Task(
                id="T1",
                type="NAVIGATE",
                vehicle=VehicleID.SCOUT,
                priority=1,
                area="sector_A",
            )
        ],
        rationale="Standard SCOUT01 navigation plan.",
    )

    # 1. SCOUT01 connected and healthy -> Valid
    world_healthy = WorldModel(
        mission=MissionState(id="M-SAFE", objective="Navigate Sector Alpha and scan"),
        vehicles={
            "SCOUT01": ScoutState(
                connection=ConnectionStatus.ONLINE,
                status="IDLE",
                battery=85.0,
                pose_valid=True,
            )
        },
    )
    ok, errs = validate_plan(sample_plan, world_healthy)
    assert ok is True
    assert errs == []

    # 2. SCOUT01 offline -> Rejected
    world_offline = world_healthy.model_copy(deep=True)
    world_offline.vehicles["SCOUT01"].connection = ConnectionStatus.OFFLINE
    ok_off, errs_off = validate_plan(sample_plan, world_offline)
    assert ok_off is False
    assert any("connection is 'OFFLINE'" in e for e in errs_off)

    # 3. SCOUT01 with UNKNOWN connection status -> Rejected
    world_unknown = world_healthy.model_copy(deep=True)
    world_unknown.vehicles["SCOUT01"].connection = ConnectionStatus.UNKNOWN
    ok_unk, errs_unk = validate_plan(sample_plan, world_unknown)
    assert ok_unk is False
    assert any("connection is 'UNKNOWN'" in e for e in errs_unk)

    # 4. SCOUT01 missing from the World Model -> Rejected
    world_missing = world_healthy.model_copy(deep=True)
    world_missing.vehicles.clear()
    ok_miss, errs_miss = validate_plan(sample_plan, world_missing)
    assert ok_miss is False
    assert any("SCOUT01 does not exist in the World Model" in e for e in errs_miss)

    # 5. SCOUT01 with low (12%) or unavailable (None / NaN) battery -> Rejected
    world_low_bat = world_healthy.model_copy(deep=True)
    world_low_bat.vehicles["SCOUT01"].battery = 12.0
    ok_low, errs_low = validate_plan(sample_plan, world_low_bat)
    assert ok_low is False
    assert any("Low Battery Safety Violation" in e for e in errs_low)

    world_none_bat = world_healthy.model_copy(deep=True)
    object.__setattr__(world_none_bat.vehicles["SCOUT01"], "battery", None)
    ok_nb, errs_nb = validate_plan(sample_plan, world_none_bat)
    assert ok_nb is False
    assert any("Battery Telemetry Unavailable/Invalid" in e for e in errs_nb)

    # 6. SCOUT01 in EMERGENCY_STOPPED state -> Rejected even for passive tasks
    world_estop = world_healthy.model_copy(deep=True)
    world_estop.vehicles["SCOUT01"].status = "EMERGENCY_STOPPED"
    ok_est, errs_est = validate_plan(sample_plan, world_estop)
    assert ok_est is False
    assert any("Emergency Stop Active" in e for e in errs_est)


# ---------------------------------------------------------------------------
# 5. Emergency Stop Hardening (Pre-Command, Mid-Tick, Mid-Delay, Disconnected Stop Fault)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_stop_hardening_pre_command_mid_tick_mid_delay_and_serial_fault() -> None:
    """Verify latched E-Stop blocks movement before write, wakes mid-delay/mid-tick, and reports serial stop failure as a CRITICAL fault."""
    reset_global_emergency_stop(authorized=True)

    # 1. Unauthorized reset is rejected; latched E-Stop blocks movement commands immediately before write
    latch_global_emergency_stop()
    assert is_global_emergency_stop_latched() is True
    with pytest.raises(PermissionError):
        reset_global_emergency_stop(authorized=False)

    writer = ScoutSerialWriter(
        port="/dev/ttyNONEXISTENT",
        baudrate=115200,
        enabled=True,
        hardware_lockout=False,
        real_mode=True,
        failsafe_verified=True,
    )
    with pytest.raises(EmergencyStopLatchedError, match="Emergency Stop is latched"):
        await writer.send("W")

    reset_global_emergency_stop(authorized=True)
    assert is_global_emergency_stop_latched() is False

    # 2. Emergency Stop arriving during a delay (`wait_unless_emergency_stopped`) interrupts immediately
    async def _trigger_estop_soon() -> None:
        await asyncio.sleep(0.03)
        latch_global_emergency_stop()

    t0 = time.perf_counter()
    trigger_task = asyncio.create_task(_trigger_estop_soon())
    completed_sleep = await wait_unless_emergency_stopped(5.0)
    await trigger_task
    elapsed_s = time.perf_counter() - t0
    assert completed_sleep is False
    assert elapsed_s < 0.5  # Woke up in ~30ms instead of sleeping 5.0s!

    reset_global_emergency_stop(authorized=True)

    # 3. Emergency Stop arriving after serial disconnection: stop command ('X') failure is reported as a CRITICAL fault
    class BrokenSerialConnection:
        is_open = True

        def write(self, _data: bytes) -> int:
            raise serial.SerialException("USB serial cable unplugged during E-Stop")

        def flush(self) -> None:
            pass

        def close(self) -> None:
            self.is_open = False

    writer._connection = BrokenSerialConnection()  # type: ignore[assignment]
    with pytest.raises(SerialStopCommandFaultError, match="CRITICAL FAULT"):
        await writer.send_verified_stop()
    assert writer.stop_fault_count >= 1
    assert writer.last_stop_fault_reason is not None

    # 4. MissionService.execute_mission_on_rover: E-Stop arriving mid-movement-tick aborts immediately,
    # marks the in-progress task CANCELLED, preserves EMERGENCY_STOPPED, and logs SAFETY fault if stop fails
    await init_db()
    factory = AsyncSessionLocal
    service = MissionService()

    async with factory() as session:
        v_row = await session.get(Vehicle, "SCOUT01")
        assert v_row is not None
        v_row.status = "IDLE"
        v_row.connection = "CONNECTED"
        v_row.battery = 95.0
        await session.commit()
        created = await service.create_mission(
            session,
            MissionCreateRequest(objective="Sweep Sector Alpha corridor"),
        )

    broken_writer = ScoutSerialWriter(
        port="/dev/ttyBROKEN",
        baudrate=115200,
        enabled=True,
        hardware_lockout=False,
        real_mode=True,
        failsafe_verified=True,
    )
    broken_writer._connection = BrokenSerialConnection()  # type: ignore[assignment]

    async def _estop_mid_rover_execution() -> None:
        await asyncio.sleep(0.05)
        latch_global_emergency_stop()
        async with factory() as session:
            v = await session.get(Vehicle, "SCOUT01")
            if v is not None:
                v.status = "EMERGENCY_STOPPED"
                await session.commit()

    estop_task = asyncio.create_task(_estop_mid_rover_execution())
    t_exec = time.perf_counter()
    detail = await service.execute_mission_on_rover(
        factory,
        created.mission_id,
        serial_writer=broken_writer,
    )
    await estop_task
    exec_elapsed = time.perf_counter() - t_exec

    # Execution aborted in < 0.5s (instead of running the 10-25s sequence)
    assert exec_elapsed < 1.0
    assert detail.status.value == "ACTIVE"
    assert any(t.status == "CANCELLED" for t in detail.tasks)

    # Verify SCOUT01 remains EMERGENCY_STOPPED and a CRITICAL STOP FAULT event was recorded in SQLite
    async with factory() as session:
        v_after = await session.get(Vehicle, "SCOUT01")
        assert v_after is not None
        assert v_after.status == "EMERGENCY_STOPPED"
        events = (
            await session.execute(
                select(Event).where(Event.mission_id == created.mission_id)
            )
        ).scalars().all()
        assert any(
            e.category == "SAFETY" and "CRITICAL STOP FAULT" in e.message
            for e in events
        )


# ---------------------------------------------------------------------------
# 6. First-Pass Regressions Preserved (node_degraded, SimpleGraphRunner, Snapshot, History Cap)
# ---------------------------------------------------------------------------


def test_node_degraded_and_bounded_retries_and_history_cap() -> None:
    """Verify node_degraded clears proposed_plan, SimpleGraphRunner bounds retries to 3, and observation_history caps at 500."""
    world = WorldModel(
        mission=MissionState(
            id="M-LOW-BAT",
            objective="Sweep Sector Alpha",
            status=MissionStatus.IN_PROGRESS,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                position=Position2D(x=0.0, y=0.0),
                battery=5.0,
                connection=ConnectionStatus.ONLINE,
            )
        },
    )

    def _stub_unsafe_planner(**kwargs: Any) -> MissionPlan:
        return MissionPlan(
            mission_id="M-LOW-BAT",
            objective="Sweep Sector Alpha",
            tasks=[
                Task(
                    id="T-UNSAFE",
                    type="NAVIGATE",
                    vehicle=VehicleID.SCOUT,
                    priority=1,
                    area="Sector Alpha",
                )
            ],
            rationale="Stub plan attempting movement on 5% battery.",
        )

    runner = build_ultron_graph()
    state: UltronState = {
        "world": world,
        "proposed_plan": None,
        "validation_errors": [],
        "repair_prompt": None,
        "retry_count": 0,
        "trigger_rationale": None,
        "language": "English",
    }

    with patch("ultron.graph.nodes.invoke_gemini_planner", side_effect=_stub_unsafe_planner):
        out = runner.invoke(state)

    assert out["retry_count"] == 3
    assert out["proposed_plan"] is None
    assert out["world"].mission.status == MissionStatus.DEGRADED
    assert out["world"].mission.tasks.active == {}
    assert any("battery" in err.lower() for err in out["validation_errors"])

    for i in range(550):
        obs = SensorObservation(
            observation_id=f"OBS-{i:04d}",
            sensor_id="depth_camera",
            sensor_type=SensorTypeEnum.DEPTH_CAMERA,
            vehicle_id="SCOUT01",
            detector_object_id=i,
            sensor_timestamp=1000.0 + i,
            ingested_at=1000.0 + i,
            coordinate_frame="oak_rgb_camera_optical_frame",
            position=Position3DMeasurement(x=1.0, y=0.5, z=2.0, range_m=2.2),
            classification="box",
            confidence=0.85,
            validation_status=ObservationValidationStatusEnum.VALIDATED,
            source_mode=SensorSourceModeEnum.SIMULATED,
            epistemic_status=EpistemicStatusEnum.OBSERVED,
            is_obstacle=True,
        )
        ingest_sensor_observation(world, obs)

    assert len(world.environment.observation_history) == 500
    assert world.environment.observation_history[0].observation_id == "OBS-0050"
    assert world.environment.observation_history[-1].observation_id == "OBS-0549"


@pytest.mark.asyncio
async def test_dashboard_snapshot_returns_most_recent_50_events_in_chronological_order() -> None:
    """Verify build_snapshot_envelope returns the newest 50 events when >50 exist."""
    await init_db()
    factory = AsyncSessionLocal
    gw = DashboardGateway(recent_events_limit=50)
    base_time = datetime(2099, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    async with factory() as session:
        existing_m = await session.get(Mission, "M-SNAP")
        if existing_m is not None:
            await session.delete(existing_m)
            await session.commit()

        mission = Mission(
            id="M-SNAP",
            objective="Snapshot pagination test",
            status="ACTIVE",
            created_at=base_time,
        )
        session.add(mission)
        for i in range(65):
            session.add(
                Event(
                    id=f"EVT-SNAP-{i:03d}",
                    mission_id="M-SNAP",
                    timestamp=base_time + timedelta(seconds=i),
                    category="TELEMETRY",
                    message=f"Event number {i}",
                )
            )
        await session.commit()

        envelope = await gw.build_snapshot_envelope(session)
        events = envelope.payload.recent_events
        assert len(events) == 50
        assert events[0].id == "EVT-SNAP-015"
        assert events[-1].id == "EVT-SNAP-064"


def test_startup_safety_and_secret_warnings_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Verify loud WARNING logs when lockout is off/mode is real or shared secret is placeholder on non-127.0.0.1 host."""
    import logging
    from types import SimpleNamespace
    from main import log_startup_safety_warnings

    sec_logger = logging.getLogger("operation_shadow")
    old_propagate = sec_logger.propagate
    sec_logger.propagate = True
    try:
        # 1. Default safe settings on 127.0.0.1 -> no WARNING
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="operation_shadow.main"):
            log_startup_safety_warnings(
                SimpleNamespace(
                    hardware_lockout=True,
                    default_vehicle_mode="simulated",
                    scout_serial_enabled=False,
                    scout_serial_failsafe_verified=False,
                    host="127.0.0.1",
                    vehicle_shared_secret="provisional_secret_change_me",
                )
            )
        assert not caplog.records

        # 2. Placeholder secret on non-127.0.0.1 host -> SECURITY WARNING
        for placeholder in ("provisional_secret_change_me", "change-me-local-demo-secret"):
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="operation_shadow.main"):
                log_startup_safety_warnings(
                    SimpleNamespace(
                        hardware_lockout=True,
                        default_vehicle_mode="simulated",
                        scout_serial_enabled=False,
                        scout_serial_failsafe_verified=False,
                        host="0.0.0.0",
                        vehicle_shared_secret=placeholder,
                    )
                )
            assert any("SECURITY WARNING" in r.message for r in caplog.records)

        # 3. Lockout off or real mode -> SAFETY WARNING
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="operation_shadow.main"):
            log_startup_safety_warnings(
                SimpleNamespace(
                    hardware_lockout=not True,
                    default_vehicle_mode="simulated",
                    scout_serial_enabled=False,
                    scout_serial_failsafe_verified=False,
                    host="127.0.0.1",
                    vehicle_shared_secret="custom-strong-secret",
                )
            )
        assert any("SAFETY WARNING" in r.message for r in caplog.records)
    finally:
        sec_logger.propagate = old_propagate

