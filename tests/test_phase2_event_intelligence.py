"""Comprehensive Phase 2 Event-Driven Intelligence Regression Suite (`tests/test_phase2_event_intelligence.py`).

Covers all 16 required Phase 2 verification scenarios:
1. Malformed event rejection (missing fields, invalid vehicle IDs, non-finite timestamps, E-Stop exclusion)
2. Event priority ordering (CRITICAL > HIGH > MEDIUM > LOW, FIFO tie-breaking)
3. Event deduplication and cooldown (duplicate suppression + material change pass-through + bounded memory)
4. Out-of-order and stale event rejection
5. Unknown or unsupported event type handling
6. Standing Order selection, precedence, and 3 arbitrary non-hardcoded mission objectives
7. Standing Order validation failure & missing evidence handling
8. LLM triage structured output validation & forbidden directive guard
9. LLM triage with invalid or stale evidence IDs
10. Gemini timeout & shared circuit breaker during triage
11. Deterministic fallback when Gemini is unavailable
12. Simultaneous events & bounded triage concurrency
13. Mission cancellation during triage
14. Stale epoch triage discard
15. Dashboard event and reasoning-trace chronological consistency (EventService + DashboardGateway + Replay)
16. Emergency Stop arriving while events are queued or triage is running
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Ensure root and backend paths are importable
_ROOT = Path(__file__).resolve().parent.parent
for _p in [str(_ROOT), str(_ROOT / "backend"), str(_ROOT / "backend" / "fastapi")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db.models import Base, Mission
from services.event_service import EventService
from services.mission_service import MissionService
from services.ultron_agent_adapter import UltronLangGraphAgent
from ultron.events.router import EventRouter, compute_event_signature
from ultron.events.schemas import (
    EventPriorityEnum,
    EventRouteTargetEnum,
    EventSourceEnum,
    EventTypeEnum,
    UltronEvent,
)
from ultron.events.standing_orders import (
    StandingOrderConfigError,
    StandingOrderRegistry,
)
from ultron.events.triage import (
    EventTriageResult,
    TriageConfidenceEnum,
    TriageDispositionEnum,
    TriageSeverityAssessmentEnum,
    build_deterministic_triage_fallback,
    invoke_event_triage,
    validate_triage_result,
)
from ultron.planner.engine import is_breaker_open, reset_breaker
from ultron.safety.validator import validate_plan
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import MissionStatus, VehicleID
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
    HazardEntity,
    MissionState,
    ScoutState,
    TrackedObject,
    TrackingStatusEnum,
    VehicleMode,
    WorldModel,
)
from websocket.dashboard_gateway import DashboardGateway


def _make_active_world(
    mission_id: str = "M-PH2",
    objective: str = "Inspect northern utility corridor for thermal anomalies",
    battery: float = 88.0,
) -> WorldModel:
    world = WorldModel(
        mission=MissionState(
            id=mission_id,
            objective=objective,
            status=MissionStatus.IN_PROGRESS,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                mode=VehicleMode.SIMULATED,
                position=Position2D(x=1.5, y=2.0),
                battery=battery,
                status="ACTIVE",
                connection=ConnectionStatus.ONLINE,
                last_heartbeat_timestamp=1700000100.0,
            )
        },
    )
    obs = SensorObservation(
        observation_id="OBS-CAM-VALID01",
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        detector_object_id=1,
        sensor_timestamp=1700000100.0,
        ingested_at=1700000100.1,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(
            x=1.0, y=0.0, z=1.2, range_m=1.2, uncertainty_m=0.1
        ),
        classification="chair",
        confidence=0.92,
        validation_status=ObservationValidationStatusEnum.VALIDATED,
        source_mode=SensorSourceModeEnum.SIMULATED,
        is_obstacle=True,
        is_hazard=False,
    )
    world.environment.observation_history.append(obs)
    world.environment.tracked_objects["OBJ-001"] = TrackedObject(
        object_id="OBJ-001",
        label="chair",
        position=obs.position,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position_uncertainty_m=0.1,
        classification_confidence=0.92,
        first_observed_timestamp=1700000100.0,
        last_observed_timestamp=1700000100.0,
        last_ingested_at=1700000100.1,
        last_known_source="depth_camera",
        vehicle_id="SCOUT01",
        source_mode=SensorSourceModeEnum.SIMULATED,
        contributing_observation_ids=["OBS-CAM-VALID01"],
        contributing_sensor_types=["depth_camera"],
        tracking_status=TrackingStatusEnum.ACTIVE,
        epistemic_status=EpistemicStatusEnum.OBSERVED,
        currently_visible=True,
        is_obstacle=True,
        is_hazard=False,
    )
    return world


# ---------------------------------------------------------------------------
# 1. Malformed event rejection & 5. Unknown/unsupported event types
# ---------------------------------------------------------------------------


def test_malformed_and_unsupported_events_rejected_explicitly() -> None:
    """Verify malformed payloads, non-SCOUT01 vehicles, non-finite timestamps, unknown types, and E-Stop are rejected."""
    router = EventRouter()

    # Missing required fields
    res_missing = router.route_event({"event_type": "HAZARD_DETECTED"})
    assert not res_missing.accepted
    assert res_missing.status == "rejected_malformed"

    # Unsupported vehicle ID (HAWK01)
    res_hawk = router.route_event(
        {
            "event_type": "HAZARD_DETECTED",
            "source": "camera",
            "vehicle_id": "HAWK01",
            "received_at": 1700000100.0,
            "summary": "Simulated aerial hazard",
        }
    )
    assert not res_hawk.accepted
    assert res_hawk.status == "rejected_malformed"
    assert "SCOUT01" in res_hawk.reason

    # Non-finite or non-positive timestamps
    for bad_ts in (float("nan"), float("inf"), -10.0, 0.0):
        res_ts = router.route_event(
            {
                "event_type": "OBSTACLE_DETECTED",
                "source": "camera",
                "received_at": bad_ts,
                "summary": "Obstacle with invalid timestamp",
            }
        )
        assert not res_ts.accepted
        assert res_ts.status == "rejected_malformed"

    # Unknown / unsupported event_type
    res_unknown = router.route_event(
        {
            "event_type": "ALIEN_SPACESHIP_LANDED",
            "source": "camera",
            "received_at": 1700000100.0,
            "summary": "Unsupported event type",
        }
    )
    assert not res_unknown.accepted
    assert res_unknown.status == "rejected_malformed"

    # Emergency Stop is explicitly excluded from EventRouter
    res_estop = router.route_event(
        {
            "event_type": "VEHICLE_FAULT",
            "source": "vehicle_state",
            "vehicle_id": "SCOUT01",
            "received_at": 1700000100.0,
            "evidence_payload": {"vehicle_status": "EMERGENCY_STOPPED"},
            "summary": "E-Stop active",
        }
    )
    assert not res_estop.accepted
    assert res_estop.status == "excluded_emergency_stop"
    assert router.rejected_malformed_count == 7


# ---------------------------------------------------------------------------
# 2. Event priority ordering & 3. Deduplication, cooldown, and bounded memory
# ---------------------------------------------------------------------------


def test_event_router_priority_ordering_dedup_material_change_and_bounds() -> None:
    """Verify CRITICAL > HIGH > MEDIUM > LOW ordering, FIFO tie-breaking, content dedup, material change pass-through, and bounded queues."""
    router = EventRouter(
        dedup_window_sec=2.0,
        stale_threshold_sec=5.0,
        max_queue_size=4,
        max_history_size=5,
        max_dedup_entries=5,
    )
    t0 = 1700000100.0

    ev_low = UltronEvent(
        event_id="EV-LOW",
        event_type=EventTypeEnum.MISSION_STATE_CHANGED,
        source=EventSourceEnum.MISSION_LIFECYCLE,
        priority=EventPriorityEnum.LOW,
        received_at=t0 + 0.01,
        summary="Low priority lifecycle event",
    )
    ev_med = UltronEvent(
        event_id="EV-MED",
        event_type=EventTypeEnum.TARGET_DETECTED,
        source=EventSourceEnum.CAMERA,
        priority=EventPriorityEnum.MEDIUM,
        tracked_object_ids=["OBJ-010"],
        evidence_payload={"position": {"x": 0.0, "y": 0.0, "z": 1.0}},
        received_at=t0 + 0.02,
        summary="Medium priority target",
    )
    ev_high_1 = UltronEvent(
        event_id="EV-HIGH-1",
        event_type=EventTypeEnum.HAZARD_DETECTED,
        source=EventSourceEnum.CAMERA,
        priority=EventPriorityEnum.HIGH,
        evidence_payload={"hazard_type": "fire", "location_x": 1.0, "location_y": 1.0},
        received_at=t0 + 0.03,
        summary="High priority fire 1",
    )
    ev_high_2 = UltronEvent(
        event_id="EV-HIGH-2",
        event_type=EventTypeEnum.SENSOR_CONFLICT,
        source=EventSourceEnum.CAMERA,
        priority=EventPriorityEnum.HIGH,
        tracked_object_ids=["OBJ-011"],
        received_at=t0 + 0.04,
        summary="High priority conflict 2",
    )
    ev_crit = UltronEvent(
        event_id="EV-CRIT",
        event_type=EventTypeEnum.VEHICLE_FAULT,
        source=EventSourceEnum.VEHICLE_STATE,
        vehicle_id="SCOUT01",
        priority=EventPriorityEnum.CRITICAL,
        received_at=t0 + 0.05,
        summary="Critical vehicle fault",
    )

    for ev in (ev_low, ev_med, ev_high_1, ev_high_2, ev_crit):
        out = router.route_event(ev)
        assert out.accepted

    # Queue max_queue_size=4 evicted EV-LOW; remaining 4 drain in CRITICAL -> HIGH (FIFO) -> MEDIUM order
    assert router.dropped_queue_overflow_count == 1
    drained = router.drain_events()
    assert [e.event_id for e in drained] == [
        "EV-CRIT",
        "EV-HIGH-1",
        "EV-HIGH-2",
        "EV-MED",
    ]

    # Duplicate within 2.0s window with different random event_id is suppressed
    ev_dup = UltronEvent(
        event_id="EV-HIGH-1-DUP",
        event_type=EventTypeEnum.HAZARD_DETECTED,
        source=EventSourceEnum.CAMERA,
        priority=EventPriorityEnum.HIGH,
        evidence_payload={"hazard_type": "fire", "location_x": 1.05, "location_y": 1.02},
        received_at=t0 + 0.50,
        summary="Duplicate fire observation within 2s",
    )
    out_dup = router.route_event(ev_dup)
    assert not out_dup.accepted
    assert out_dup.status == "suppressed_duplicate"
    assert out_dup.coalesced_count == 2

    # Material spatial change (>= 0.50m displacement on same tracked object) is NOT suppressed
    ev_moved = UltronEvent(
        event_id="EV-MED-MOVED",
        event_type=EventTypeEnum.TARGET_DETECTED,
        source=EventSourceEnum.CAMERA,
        priority=EventPriorityEnum.MEDIUM,
        tracked_object_ids=["OBJ-010"],
        evidence_payload={"position": {"x": 0.0, "y": 0.0, "z": 1.0}},
        received_at=t0 + 0.60,
        summary="Target at initial position",
    )
    router.route_event(ev_moved)
    ev_moved_far = UltronEvent(
        event_id="EV-MED-MOVED-2",
        event_type=EventTypeEnum.TARGET_DETECTED,
        source=EventSourceEnum.CAMERA,
        priority=EventPriorityEnum.MEDIUM,
        tracked_object_ids=["OBJ-010"],
        evidence_payload={"position": {"x": 0.85, "y": 0.0, "z": 1.0}},
        received_at=t0 + 0.80,
        summary="Target moved 0.85m",
    )
    out_moved_far = router.route_event(ev_moved_far)
    assert out_moved_far.accepted
    assert len(router.history) <= 5


# ---------------------------------------------------------------------------
# 4. Out-of-order and stale event rejection
# ---------------------------------------------------------------------------


def test_out_of_order_and_stale_events_rejected() -> None:
    """Verify older source_timestamp is rejected as out-of-order and expired source_timestamp is rejected as stale."""
    router = EventRouter(dedup_window_sec=1.0, stale_threshold_sec=3.0)
    t_base = 1700000200.0

    ev_fresh = UltronEvent(
        event_type=EventTypeEnum.OBSTACLE_DETECTED,
        source=EventSourceEnum.ROSBRIDGE,
        vehicle_id="SCOUT01",
        tracked_object_ids=["OBJ-001"],
        source_timestamp=t_base,
        received_at=t_base + 0.1,
        summary="Fresh obstacle frame",
    )
    assert router.route_event(ev_fresh).accepted

    # Older source_timestamp on the same stream -> rejected_out_of_order
    ev_older = UltronEvent(
        event_type=EventTypeEnum.OBSTACLE_DETECTED,
        source=EventSourceEnum.ROSBRIDGE,
        vehicle_id="SCOUT01",
        tracked_object_ids=["OBJ-001"],
        source_timestamp=t_base - 0.5,
        received_at=t_base + 0.2,
        summary="Out-of-order obstacle frame",
    )
    out_older = router.route_event(ev_older)
    assert not out_older.accepted
    assert out_older.status == "rejected_out_of_order"
    assert router.rejected_out_of_order_count == 1

    # Stale source_timestamp (age 6.0s > 3.0s threshold) -> rejected_stale
    ev_stale = UltronEvent(
        event_type=EventTypeEnum.TARGET_DETECTED,
        source=EventSourceEnum.CAMERA,
        vehicle_id="SCOUT01",
        tracked_object_ids=["OBJ-099"],
        source_timestamp=t_base + 1.0,
        received_at=t_base + 7.0,
        summary="Stale target detection",
    )
    out_stale = router.route_event(ev_stale)
    assert not out_stale.accepted
    assert out_stale.status == "rejected_stale"
    assert router.rejected_stale_count == 1


# ---------------------------------------------------------------------------
# 6 & 7. Standing Order selection, precedence, 3 objectives, and validation failures
# ---------------------------------------------------------------------------


def test_standing_order_precedence_objectives_and_validation_failures() -> None:
    """Verify Standing Order selection, 3 arbitrary objectives, missing evidence rejection, and invalid config failure."""
    registry = StandingOrderRegistry.from_json_file()
    objectives = [
        "Survey subterranean cooling pipes in West Wing",
        "Locate lost medical supply crate near Loading Dock 4",
        "Map radiation contours in Decommissioned Reactor Hall",
    ]

    for idx, obj_text in enumerate(objectives):
        world = _make_active_world(mission_id=f"M-OBJ-{idx}", objective=obj_text)
        ev = UltronEvent(
            event_type=EventTypeEnum.OBSTACLE_DETECTED,
            source=EventSourceEnum.CAMERA,
            mission_id=world.mission.id,
            vehicle_id="SCOUT01",
            observation_ids=["OBS-CAM-VALID01"],
            tracked_object_ids=["OBJ-001"],
            coordinate_frame="oak_rgb_camera_optical_frame",
            confidence=0.92,
            received_at=1700000101.0 + idx * 20.0,
            evidence_payload={"label": "chair", "range_m": 1.2, "blocks_route": True},
            summary="Chair blocking corridor at 1.2m",
        )
        res = registry.evaluate_and_execute(ev, world)
        assert res.executed is True
        assert res.standing_order_id == "SO-03-BLOCKED-ROUTE"
        assert res.plan is not None
        assert res.plan.objective == obj_text
        assert "USED CACHED PLAN" in res.plan.rationale
        is_valid, errors = validate_plan(res.plan, world)
        assert is_valid, f"Standing Order plan failed safety validator: {errors}"

    # Missing required evidence in WorldModel must NOT execute Standing Order
    empty_world = WorldModel(
        mission=MissionState(
            id="M-EMPTY", objective="Test missing evidence", status=MissionStatus.IN_PROGRESS
        )
    )
    ev_no_ev = UltronEvent(
        event_type=EventTypeEnum.SENSOR_CONFLICT,
        source=EventSourceEnum.CAMERA,
        mission_id="M-EMPTY",
        vehicle_id="SCOUT01",
        observation_ids=["OBS-DOES-NOT-EXIST"],
        received_at=1700000500.0,
        summary="Conflict without WorldModel backing",
    )
    res_no_ev = registry.evaluate_and_execute(ev_no_ev, empty_world)
    assert res_no_ev.executed is False
    assert res_no_ev.status == "missing_required_evidence"

    # Invalid Standing Order config (e.g. HAWK vehicle or missing USED CACHED PLAN) fails fast
    with pytest.raises(StandingOrderConfigError):
        StandingOrderRegistry.from_dict(
            {
                "version": "1.0",
                "standing_orders": [
                    {
                        "id": "SO-BAD-01",
                        "name": "Bad Order",
                        "description": "Invalid vehicle",
                        "supported_event_types": ["HAZARD_DETECTED"],
                        "priority": 50,
                        "preconditions": {},
                        "required_evidence": ["hazard"],
                        "response_template": {
                            "branch": "hazard_hold",
                            "rationale_prefix": "Missing cached plan marker",
                            "tasks": [
                                {
                                    "id": "T1",
                                    "vehicle": "SCOUT01",
                                    "type": "STOP",
                                    "priority": 1,
                                }
                            ],
                        },
                    }
                ],
            }
        )


# ---------------------------------------------------------------------------
# 8, 9, 10, 11. LLM Triage validation, invalid evidence, timeout/breaker, fallback
# ---------------------------------------------------------------------------


def test_llm_triage_validation_invalid_evidence_timeout_breaker_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify EventTriageResult validation, fabricated/stale evidence stripping, timeout + circuit breaker, and deterministic fallback."""
    reset_breaker()
    world = _make_active_world()

    # Add a stale observation to test stale evidence demotion
    stale_obs = SensorObservation(
        observation_id="OBS-CAM-STALE99",
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        sensor_timestamp=1700000050.0,  # 50s older than current time
        ingested_at=1700000050.1,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(x=2.0, y=0.5, z=1.5, range_m=2.0, uncertainty_m=0.15),
        classification="box",
        confidence=0.80,
        validation_status=ObservationValidationStatusEnum.VALIDATED,
        source_mode=SensorSourceModeEnum.SIMULATED,
    )
    world.environment.observation_history.append(stale_obs)

    ev = UltronEvent(
        event_id="UEVT-TRIAGE01",
        event_type=EventTypeEnum.TARGET_DETECTED,
        source=EventSourceEnum.CAMERA,
        mission_id=world.mission.id,
        vehicle_id="SCOUT01",
        observation_ids=["OBS-CAM-VALID01"],
        tracked_object_ids=["OBJ-001"],
        coordinate_frame="oak_rgb_camera_optical_frame",
        confidence=0.89,
        received_at=1700000100.5,
        evidence_payload={"label": "red_package", "matches_mission_objective": True},
        summary="Potential mission target detected in camera frame",
    )

    # 8 & 9: Validate triage output with fabricated IDs, stale IDs, and unverified Standing Order
    raw_triage = EventTriageResult(
        event_id="WRONG-ID",
        disposition=TriageDispositionEnum.REQUEST_REPLAN,
        severity_assessment=TriageSeverityAssessmentEnum.MEDIUM,
        confidence=TriageConfidenceEnum.HIGH,
        rationale="Target detected in camera frame; replanning inspection.",
        facts_used=["Observed red_package in oak_rgb_camera_optical_frame."],
        inferences=["Target requires closer inspection."],
        unknowns=[],
        evidence_ids=["OBS-CAM-VALID01", "OBS-FABRICATED-999", "OBS-CAM-STALE99"],
        requires_replan=False,
    )
    cleaned, repairs = validate_triage_result(raw_triage, ev, world)
    assert cleaned.event_id == "UEVT-TRIAGE01"
    assert cleaned.evidence_ids == ["OBS-CAM-VALID01"]
    assert cleaned.confidence == TriageConfidenceEnum.LOW  # downgraded due to invalid/stale IDs
    assert cleaned.requires_replan is True
    assert any("STALE" in u for u in cleaned.unknowns)
    assert any("camera-relative" in u or "transform" in u.lower() for u in cleaned.unknowns)
    assert len(repairs) >= 3

    # Forbidden directive (PWM / motor_cmd / HAWK) in LLM output is rejected -> deterministic fallback
    forbidden_triage = EventTriageResult(
        event_id=ev.event_id,
        disposition=TriageDispositionEnum.REQUEST_REPLAN,
        rationale="Send motor_cmd PWM=255 and dispatch HAWK immediately.",
    )
    safe_fallback, fb_repairs = validate_triage_result(forbidden_triage, ev, world)
    assert safe_fallback.rationale.startswith("DETERMINISTIC TRIAGE FALLBACK")
    assert any("forbidden" in r.lower() for r in fb_repairs)

    # 11: Deterministic fallback when GEMINI_API_KEY is unset
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    fb_res, fb_outcome = invoke_event_triage(ev, world)
    assert fb_outcome.source == "fallback"
    assert fb_outcome.reason == "api_key_unset"
    assert fb_res.disposition == TriageDispositionEnum.REQUEST_REPLAN
    assert fb_res.evidence_ids == ["OBS-CAM-VALID01", "OBJ-001"]

    # 10: Gemini timeout during triage enforces SHADOW_LLM_TRIAGE_TIMEOUT_SEC and trips shared circuit breaker
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key-do-not-use")
    monkeypatch.setenv("SHADOW_LLM_TRIAGE_TIMEOUT_SEC", "0.15")
    monkeypatch.setenv("SHADOW_LLM_BREAKER_COOLDOWN_SEC", "10.0")

    def _hung_triage_sdk(**kwargs: Any) -> Any:
        time.sleep(1.0)
        return MagicMock(parsed=None)

    with patch("ultron.events.triage._call_gemini_triage_sdk", side_effect=_hung_triage_sdk) as mock_sdk:
        t0 = time.perf_counter()
        to_res, to_outcome = invoke_event_triage(ev, world)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.50
        assert to_outcome.source == "fallback"
        assert to_outcome.reason == "read_timeout"
        assert is_breaker_open() is True
        assert mock_sdk.call_count == 1

        # Second call while circuit breaker is open returns immediately without calling SDK
        t1 = time.perf_counter()
        cb_res, cb_outcome = invoke_event_triage(ev, world)
        cb_elapsed = time.perf_counter() - t1
        assert cb_elapsed < 0.05
        assert cb_outcome.source == "fallback"
        assert cb_outcome.reason == "circuit_breaker_open"
        assert mock_sdk.call_count == 1

    reset_breaker()


# ---------------------------------------------------------------------------
# 12, 13, 14, 16. Simultaneous events, bounded concurrency, cancellation,
#                 stale epoch discard, and E-Stop independence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_triage_bounded_concurrency_epoch_fencing_cancellation_and_estop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify bounded triage concurrency (=1), stale epoch discard, mission cancellation, and E-Stop preemption."""
    reset_breaker()
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    agent = UltronLangGraphAgent(replan_cooldown_sec=0.01, replan_debounce_sec=0.0)
    await agent.create_simulated_mission("M-CONC-01", "Inspect Sector Alpha for targets")

    active_triage_calls = 0
    max_concurrent_triage = 0
    triaged_event_ids: list[str] = []

    def _tracked_triage(
        event: UltronEvent, world: WorldModel, *, registry: Any = None
    ) -> Any:
        nonlocal active_triage_calls, max_concurrent_triage
        active_triage_calls += 1
        max_concurrent_triage = max(max_concurrent_triage, active_triage_calls)
        triaged_event_ids.append(event.event_id)
        time.sleep(0.06)
        active_triage_calls -= 1
        res = build_deterministic_triage_fallback(event, world, fallback_reason="test")
        from ultron.events.triage import TriageOutcome

        return res, TriageOutcome(source="fallback", reason="test", elapsed_ms=60.0)

    t_now = time.time()
    with patch("services.ultron_agent_adapter.invoke_event_triage", side_effect=_tracked_triage):
        # Submit 3 simultaneous ambiguous events with different priorities (LOW, MEDIUM, HIGH)
        agent.submit_event(
            UltronEvent(
                event_id="UEVT-SIM-LOW",
                event_type=EventTypeEnum.OBSERVATION_STALE,
                source=EventSourceEnum.CAMERA,
                priority=EventPriorityEnum.LOW,
                mission_id="M-CONC-01",
                vehicle_id="SCOUT01",
                received_at=t_now + 0.01,
                summary="Stale peripheral observation",
            )
        )
        agent.submit_event(
            UltronEvent(
                event_id="UEVT-SIM-MED",
                event_type=EventTypeEnum.TARGET_DETECTED,
                source=EventSourceEnum.CAMERA,
                priority=EventPriorityEnum.MEDIUM,
                mission_id="M-CONC-01",
                vehicle_id="SCOUT01",
                received_at=t_now + 0.02,
                evidence_payload={"label": "crate", "matches_mission_objective": False},
                summary="Ambiguous crate detected",
            )
        )
        agent.submit_event(
            UltronEvent(
                event_id="UEVT-SIM-HIGH",
                event_type=EventTypeEnum.SENSOR_DEGRADED,
                source=EventSourceEnum.SENSOR_HEALTH,
                priority=EventPriorityEnum.HIGH,
                mission_id="M-CONC-01",
                vehicle_id="SCOUT01",
                received_at=t_now + 0.03,
                 evidence_payload={"sensor_id": "depth_camera"},
                summary="Depth camera frame rate degraded",
            )
        )

        await agent.wait_for_replanning(timeout=3.0)

    assert max_concurrent_triage == 1
    assert set(triaged_event_ids) == {"UEVT-SIM-LOW", "UEVT-SIM-MED", "UEVT-SIM-HIGH"}
    assert agent.triage_completed_count == 3

    # 13 & 14: Mission cancellation / epoch change while triage is in flight discards stale triage output
    def _slow_triage_for_cancel(
        event: UltronEvent, world: WorldModel, *, registry: Any = None
    ) -> Any:
        time.sleep(0.15)
        res = build_deterministic_triage_fallback(event, world, fallback_reason="slow")
        from ultron.events.triage import TriageOutcome

        return res, TriageOutcome(source="fallback", reason="slow", elapsed_ms=150.0)

    with patch("services.ultron_agent_adapter.invoke_event_triage", side_effect=_slow_triage_for_cancel):
        agent.submit_event(
            UltronEvent(
                event_id="UEVT-CANCEL-01",
                event_type=EventTypeEnum.TARGET_DETECTED,
                source=EventSourceEnum.CAMERA,
                priority=EventPriorityEnum.MEDIUM,
                mission_id="M-CONC-01",
                vehicle_id="SCOUT01",
                received_at=t_now + 5.0,
                summary="Target during mission cancellation",
            )
        )
        await asyncio.sleep(0.02)
        agent.cancel_pending_replanning("mission_cancelled")
        await agent.wait_for_replanning(timeout=1.0)

    assert agent.triage_discarded_stale_count >= 1
    assert not any(ev.event_id == "UEVT-CANCEL-01" for ev, _, _ in agent.triage_outcomes)

    # 16: Emergency Stop arriving while events are queued or triage is running
    await agent.create_simulated_mission("M-ESTOP-02", "Sweep corridor before E-Stop")
    with patch("services.ultron_agent_adapter.invoke_event_triage", side_effect=_slow_triage_for_cancel):
        agent.submit_event(
            UltronEvent(
                event_id="UEVT-ESTOP-INFLIGHT",
                event_type=EventTypeEnum.SENSOR_DEGRADED,
                source=EventSourceEnum.SENSOR_HEALTH,
                priority=EventPriorityEnum.HIGH,
                mission_id="M-ESTOP-02",
                vehicle_id="SCOUT01",
                received_at=t_now + 10.0,
                evidence_payload={"sensor_id": "depth_camera"},
                summary="Sensor degraded right before E-Stop",
            )
        )
        await asyncio.sleep(0.02)
        await agent.update_vehicle_state("SCOUT01", status="EMERGENCY_STOPPED")
        await agent.wait_for_replanning(timeout=1.0)

    assert agent.world.vehicles["SCOUT01"].status == "EMERGENCY_STOPPED"
    assert not any(ev.event_id == "UEVT-ESTOP-INFLIGHT" for ev, _, _ in agent.triage_outcomes)

    # Any event submitted while E-Stop is active is immediately excluded
    post_estop_out = agent.submit_event(
        UltronEvent(
            event_id="UEVT-POST-ESTOP",
            event_type=EventTypeEnum.OBSTACLE_DETECTED,
            source=EventSourceEnum.CAMERA,
            mission_id="M-ESTOP-02",
            vehicle_id="SCOUT01",
            received_at=t_now + 12.0,
            summary="Obstacle after E-Stop",
        )
    )
    assert not post_estop_out.accepted
    assert post_estop_out.status == "excluded_emergency_stop"


# ---------------------------------------------------------------------------
# 15. Dashboard event and reasoning-trace chronological consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_event_and_reasoning_trace_chronological_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify routed events, Standing Orders, and triage traces sync to EventService/DashboardGateway and replay chronologically."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gateway = DashboardGateway()
    event_svc = EventService(gateway=gateway)
    agent = UltronLangGraphAgent(replan_cooldown_sec=0.01, replan_debounce_sec=0.0)
    mission_svc = MissionService(agent=agent, gateway=gateway, events=event_svc)

    from schemas.entities import MissionCreateRequest

    async with session_factory() as session:
        create_res = await mission_svc.create_mission(
            session,
            MissionCreateRequest(
                objective="Inspect Server Room B and report obstacles",
                language="English",
            ),
        )
        mission_id = create_res.mission_id

    # Ingest a blocking obstacle frame (triggers Standing Order SO-03-BLOCKED-ROUTE)
    now_dt = datetime.now(timezone.utc)
    agent.ingest_detection_frame_into_world_model(
        stamp=now_dt.timestamp(),
        received_at=now_dt,
        frame="oak_rgb_camera_optical_frame",
        objects=[
            {
                "id": 1,
                "class": "chair",
                "score": 0.94,
                "position": {"x": 0.9, "y": 0.0, "z": 1.1},
                "range_m": 1.1,
            }
        ],
        vehicle_id="SCOUT01",
        telemetry_source="rosbridge",
        map_frame_confirmed=False,
    )
    await agent.wait_for_replanning(timeout=3.0)
    assert agent.standing_orders_applied_count >= 1

    # Sync routed events & decision traces to EventService and verify replay timeline
    async with session_factory() as session:
        persisted_events, persisted_traces = await agent.sync_event_intelligence_to_event_service(
            session,
            event_svc,
            mission_id=mission_id,
            broadcast=True,
        )
        assert len(persisted_events) >= 1
        assert len(persisted_traces) >= 1

        replay = await mission_svc.get_mission_replay(session, mission_id)
        assert replay.mission_id == mission_id
        assert len(replay.entries) >= 4
        timestamps = [entry.timestamp for entry in replay.entries]
        assert timestamps == sorted(timestamps)

    await engine.dispose()
