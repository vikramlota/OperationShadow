"""Unit tests for Workstream C — Standing Orders (`tests/test_ultron_standing_orders.py`)."""

from __future__ import annotations

import time
import pytest

from ultron.events.schemas import (
    EventPriorityEnum,
    EventSourceEnum,
    EventTypeEnum,
    UltronEvent,
)
from ultron.events.standing_orders import (
    StandingOrderConfigError,
    StandingOrderRegistry,
)
from ultron.planner.grounding import validate_plan_evidence
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
    WorldModel,
)


def _make_active_world(
    mission_id: str = "M-SO-01",
    objective: str = "Inspect sector_A and zone_2 for anomalies.",
    battery: float = 85.0,
) -> WorldModel:
    return WorldModel(
        mission=MissionState(
            id=mission_id,
            objective=objective,
            status=MissionStatus.IN_PROGRESS,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                position=Position2D(x=1.5, y=1.5),
                battery=battery,
                connection=ConnectionStatus.ONLINE,
                status="ACTIVE",
                pose_valid=True,
            )
        },
    )


def _seed_observation_and_object(
    world: WorldModel,
    *,
    obs_id: str = "OBS-CAM-001",
    obj_id: str = "OBJ-001",
    label: str = "chair",
    epistemic_status: EpistemicStatusEnum = EpistemicStatusEnum.OBSERVED,
    is_obstacle: bool = True,
    is_hazard: bool = False,
    range_m: float = 1.2,
    now: float = 1700000100.0,
) -> None:
    obs = SensorObservation(
        observation_id=obs_id,
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        sensor_timestamp=now - 0.1,
        ingested_at=now,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(x=0.5, y=0.0, z=range_m, range_m=range_m),
        classification=label,
        confidence=0.92,
        validation_status=ObservationValidationStatusEnum.VALIDATED,
        source_mode=SensorSourceModeEnum.SIMULATED,
        is_obstacle=is_obstacle,
        is_hazard=is_hazard,
    )
    world.environment.observation_history.append(obs)
    world.environment.tracked_objects[obj_id] = TrackedObject(
        object_id=obj_id,
        label=label,
        position=Position3DMeasurement(x=0.5, y=0.0, z=range_m, range_m=range_m),
        coordinate_frame="oak_rgb_camera_optical_frame",
        classification_confidence=0.92,
        first_observed_timestamp=now - 0.1,
        last_observed_timestamp=now - 0.1,
        last_ingested_at=now,
        last_known_source="depth_camera",
        vehicle_id="SCOUT01",
        source_mode=SensorSourceModeEnum.SIMULATED,
        contributing_observation_ids=[obs_id],
        contributing_sensor_types=["depth_camera"],
        tracking_status=TrackingStatusEnum.CONFLICTED
        if epistemic_status == EpistemicStatusEnum.CONFLICTED
        else TrackingStatusEnum.ACTIVE,
        epistemic_status=epistemic_status,
        is_obstacle=is_obstacle,
        is_hazard=is_hazard,
        conflict_details="Camera vs LiDAR 1.4m discrepancy"
        if epistemic_status == EpistemicStatusEnum.CONFLICTED
        else None,
    )


def test_each_standing_order_selects_correct_template_and_passes_validator() -> None:
    """Verify Critical Battery, Sensor Conflict, Blocked Route, and Hazard Hold standing orders."""
    registry = StandingOrderRegistry.from_json_file()
    now = 1700000100.0

    # A. Critical Battery (SO-01-CRITICAL-BATTERY)
    world_bat = _make_active_world("M-BAT", "Patrol north corridor", battery=14.5)
    ev_bat = UltronEvent(
        event_type=EventTypeEnum.VEHICLE_LOW_BATTERY,
        source=EventSourceEnum.TELEMETRY,
        mission_id="M-BAT",
        vehicle_id="SCOUT01",
        source_timestamp=now - 0.1,
        received_at=now,
        evidence_payload={"battery": 14.5},
        summary="Critical battery 14.5% on SCOUT01",
    )
    res_bat = registry.evaluate_and_execute(ev_bat, world_bat)
    assert res_bat.matched is True
    assert res_bat.executed is True
    assert res_bat.standing_order_id == "SO-01-CRITICAL-BATTERY"
    assert res_bat.plan is not None
    assert "USED CACHED PLAN" in res_bat.plan.rationale
    assert [t.type for t in res_bat.plan.tasks] == ["STOP", "REPORT"]
    ok_bat, errs_bat = validate_plan(res_bat.plan, world_bat)
    assert ok_bat is True, errs_bat

    # B. Sensor Conflict (SO-02-SENSOR-CONFLICT)
    world_conf = _make_active_world("M-CONF", "Inspect loading bay")
    _seed_observation_and_object(
        world_conf,
        obs_id="OBS-CONF-1",
        obj_id="OBJ-CONF-1",
        label="box",
        epistemic_status=EpistemicStatusEnum.CONFLICTED,
        now=now,
    )
    ev_conf = UltronEvent(
        event_type=EventTypeEnum.SENSOR_CONFLICT,
        source=EventSourceEnum.CAMERA,
        mission_id="M-CONF",
        vehicle_id="SCOUT01",
        source_timestamp=now - 0.1,
        received_at=now,
        observation_ids=["OBS-CONF-1"],
        tracked_object_ids=["OBJ-CONF-1"],
        epistemic_status=EpistemicStatusEnum.CONFLICTED,
        summary="Sensor conflict on OBJ-CONF-1",
    )
    res_conf = registry.evaluate_and_execute(ev_conf, world_conf)
    assert res_conf.executed is True
    assert res_conf.standing_order_id == "SO-02-SENSOR-CONFLICT"
    assert res_conf.plan is not None
    assert [t.type for t in res_conf.plan.tasks] == ["STOP", "SCAN", "REPORT"]
    assert "OBS-CONF-1" in res_conf.plan.evidence_ids
    ok_conf, errs_conf = validate_plan(res_conf.plan, world_conf)
    assert ok_conf is True, errs_conf

    # C. Blocked Route (SO-03-BLOCKED-ROUTE)
    world_obs = _make_active_world("M-OBS", "Traverse main aisle")
    _seed_observation_and_object(
        world_obs,
        obs_id="OBS-CHAIR-1",
        obj_id="OBJ-CHAIR-1",
        label="chair",
        epistemic_status=EpistemicStatusEnum.OBSERVED,
        is_obstacle=True,
        range_m=1.1,
        now=now,
    )
    ev_obs = UltronEvent(
        event_type=EventTypeEnum.OBSTACLE_DETECTED,
        source=EventSourceEnum.CAMERA,
        mission_id="M-OBS",
        vehicle_id="SCOUT01",
        source_timestamp=now - 0.1,
        received_at=now,
        observation_ids=["OBS-CHAIR-1"],
        tracked_object_ids=["OBJ-CHAIR-1"],
        evidence_payload={"label": "chair", "range_m": 1.1, "blocks_route": True},
        summary="Obstacle chair blocking route at 1.1m",
    )
    res_obs = registry.evaluate_and_execute(ev_obs, world_obs)
    assert res_obs.executed is True
    assert res_obs.standing_order_id == "SO-03-BLOCKED-ROUTE"
    assert res_obs.plan is not None
    assert [t.type for t in res_obs.plan.tasks] == ["STOP", "SCAN", "REPORT"]
    assert "OBS-CHAIR-1" in res_obs.plan.evidence_ids
    ok_obs, errs_obs = validate_plan(res_obs.plan, world_obs)
    assert ok_obs is True, errs_obs

    # D. Hazard Hold (SO-04-HAZARD-HOLD)
    world_haz = _make_active_world("M-HAZ", "Check storage room")
    _seed_observation_and_object(
        world_haz,
        obs_id="OBS-FIRE-1",
        obj_id="OBJ-FIRE-1",
        label="fire",
        is_obstacle=False,
        is_hazard=True,
        now=now,
    )
    world_haz.environment.hazards.append(
        HazardEntity(
            id="HAZ-01",
            location=Position2D(x=1.0, y=1.0),
            coordinate_frame="oak_rgb_camera_optical_frame",
            confidence=0.91,
            source_vehicle=VehicleID.SCOUT,
            type="fire",
            observation_id="OBS-FIRE-1",
            timestamp=now - 0.1,
        )
    )
    ev_haz = UltronEvent(
        event_type=EventTypeEnum.HAZARD_DETECTED,
        source=EventSourceEnum.CAMERA,
        mission_id="M-HAZ",
        vehicle_id="SCOUT01",
        source_timestamp=now - 0.1,
        received_at=now,
        confidence=0.91,
        observation_ids=["OBS-FIRE-1"],
        tracked_object_ids=["OBJ-FIRE-1"],
        evidence_payload={"hazard_id": "HAZ-01", "hazard_type": "fire"},
        summary="Fire hazard detected",
    )
    res_haz = registry.evaluate_and_execute(ev_haz, world_haz)
    assert res_haz.executed is True
    assert res_haz.standing_order_id == "SO-04-HAZARD-HOLD"
    assert res_haz.operator_intervention_required is True
    assert res_haz.plan is not None
    ok_haz, errs_haz = validate_plan(res_haz.plan, world_haz)
    assert ok_haz is True, errs_haz


def test_standing_orders_retain_three_arbitrary_objectives_and_pass_validation() -> None:
    """Verify 3 different arbitrary objectives are preserved verbatim and pass safety + evidence validation."""
    registry = StandingOrderRegistry.from_json_file()
    now = 1700000200.0
    objectives = [
        "Objective Alpha: Sweep east wing for unauthorized packages and report.",
        "Objective Bravo: Monitor server room corridor for thermal or obstacle anomalies.",
        "Objective Charlie: Verify perimeter door clearance before night lockdown.",
    ]

    for idx, custom_obj in enumerate(objectives, start=1):
        mid = f"M-OBJ-{idx:02d}"
        world = _make_active_world(mid, custom_obj)
        _seed_observation_and_object(
            world,
            obs_id=f"OBS-{idx}",
            obj_id=f"OBJ-{idx}",
            label="box",
            is_obstacle=True,
            range_m=1.0,
            now=now + idx,
        )
        ev = UltronEvent(
            event_type=EventTypeEnum.OBSTACLE_DETECTED,
            source=EventSourceEnum.CAMERA,
            mission_id=mid,
            vehicle_id="SCOUT01",
            source_timestamp=now + idx - 0.1,
            received_at=now + idx,
            observation_ids=[f"OBS-{idx}"],
            tracked_object_ids=[f"OBJ-{idx}"],
            evidence_payload={"label": "box", "range_m": 1.0, "blocks_route": True},
            summary=f"Box blocking route on mission {mid}",
        )
        res = registry.evaluate_and_execute(ev, world)
        assert res.executed is True
        assert res.plan is not None
        assert res.plan.objective == custom_obj
        assert f"OBS-{idx}" in res.plan.evidence_ids
        ok, errs = validate_plan(res.plan, world)
        assert ok is True, errs
        _, ev_warns = validate_plan_evidence(world, res.plan)
        assert ev_warns == []


def test_missing_evidence_prevents_action_and_duplicates_suppressed_and_unknown_events_ignored() -> None:
    """Verify missing World Model evidence blocks execution, duplicate events do not re-execute, and invalid configs fail."""
    registry = StandingOrderRegistry.from_json_file()
    now = 1700000300.0

    # 1. Missing evidence in World Model prevents Standing Order execution
    empty_world = _make_active_world("M-EMPTY", "Sweep corridor")
    ev_unverified_conflict = UltronEvent(
        event_type=EventTypeEnum.SENSOR_CONFLICT,
        source=EventSourceEnum.CAMERA,
        mission_id="M-EMPTY",
        vehicle_id="SCOUT01",
        source_timestamp=now - 0.1,
        received_at=now,
        observation_ids=["OBS-NONEXISTENT"],
        tracked_object_ids=["OBJ-NONEXISTENT"],
        summary="Unverified conflict with no World Model object",
    )
    res_missing = registry.evaluate_and_execute(ev_unverified_conflict, empty_world)
    assert res_missing.executed is False
    assert res_missing.status == "missing_required_evidence"
    assert res_missing.plan is None

    # 2. Duplicate event within cooldown does not re-execute the same Standing Order
    world_dup = _make_active_world("M-DUP", "Sweep corridor")
    _seed_observation_and_object(
        world_dup,
        obs_id="OBS-DUP-1",
        obj_id="OBJ-DUP-1",
        label="chair",
        is_obstacle=True,
        range_m=1.0,
        now=now,
    )
    ev_first = UltronEvent(
        event_type=EventTypeEnum.OBSTACLE_DETECTED,
        source=EventSourceEnum.CAMERA,
        mission_id="M-DUP",
        vehicle_id="SCOUT01",
        source_timestamp=now - 0.1,
        received_at=now,
        observation_ids=["OBS-DUP-1"],
        tracked_object_ids=["OBJ-DUP-1"],
        evidence_payload={"label": "chair", "range_m": 1.0, "blocks_route": True},
        summary="Chair blocking route",
    )
    res_first = registry.evaluate_and_execute(ev_first, world_dup)
    assert res_first.executed is True

    ev_second = ev_first.model_copy(update={"event_id": "UEVT-DUP-2", "received_at": now + 1.0})
    res_second = registry.evaluate_and_execute(ev_second, world_dup)
    assert res_second.matched is True
    assert res_second.executed is False
    assert res_second.status == "suppressed_already_handled"

    # 3. Event type without a Standing Order (e.g., TARGET_DETECTED) does not select an arbitrary Standing Order
    ev_target = UltronEvent(
        event_type=EventTypeEnum.TARGET_DETECTED,
        source=EventSourceEnum.CAMERA,
        mission_id="M-DUP",
        vehicle_id="SCOUT01",
        source_timestamp=now,
        received_at=now + 2.0,
        summary="Target detected",
    )
    res_target = registry.evaluate_and_execute(ev_target, world_dup)
    assert res_target.matched is False
    assert res_target.executed is False
    assert res_target.status == "no_matching_order"

    # 4. Invalid Standing Order configuration fails safely at load time
    with pytest.raises(StandingOrderConfigError):
        StandingOrderRegistry.from_dict(
            {
                "version": "1.0",
                "standing_orders": [
                    {
                        "id": "SO-BAD-01",
                        "name": "Invalid Action Order",
                        "description": "Contains unsupported task type FLY_DRONE",
                        "supported_event_types": ["OBSTACLE_DETECTED"],
                        "priority": 50,
                        "preconditions": {},
                        "required_evidence": ["obs"],
                        "response_template": {
                            "branch": "blocked_route",
                            "rationale_prefix": "USED CACHED PLAN — Bad order:",
                            "tasks": [
                                {
                                    "id": "T-BAD-1",
                                    "vehicle": "SCOUT01",
                                    "type": "FLY_DRONE",
                                    "priority": 1,
                                }
                            ],
                        },
                    }
                ],
            }
        )
