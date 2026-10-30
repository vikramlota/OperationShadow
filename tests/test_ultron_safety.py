from ultron.schemas.mission import MissionPlan, Task, VehicleID, TaskPriority
from ultron.schemas.world import WorldModel, MissionState, ScoutState, HawkState
from ultron.safety.validator import validate_plan
from ultron.safety.constraints import is_within_geofence
from ultron.schemas.commands import Position3D


def test_validator_detects_forbidden_hawk_action():
    world = WorldModel(mission=MissionState(id="M001", objective="test"))
    bad_plan = MissionPlan(
        mission_id="M001",
        objective="test",
        rationale="Testing illegal HAWK free navigation",
        tasks=[
            Task(
                id="T001",
                vehicle=VehicleID.HAWK,
                type="FREE_NAVIGATE",  # strictly forbidden for HAWK
                priority=TaskPriority.HIGH
            )
        ]
    )
    is_valid, errors = validate_plan(bad_plan, world)
    assert not is_valid
    assert any("strictly forbidden for HAWK01" in err for err in errors)


def test_validator_accepts_valid_plan():
    world = WorldModel(mission=MissionState(id="M001", objective="test"))
    valid_plan = MissionPlan(
        mission_id="M001",
        objective="test",
        rationale="Valid task distribution",
        tasks=[
            Task(
                id="T001",
                vehicle=VehicleID.HAWK,
                type="HOVER_SCAN",
                area="zone_2",
                priority=TaskPriority.HIGH
            ),
            Task(
                id="T002",
                vehicle=VehicleID.SCOUT,
                type="EXPLORE",
                area="sector_A",
                priority=TaskPriority.HIGH
            )
        ]
    )
    is_valid, errors = validate_plan(valid_plan, world)
    assert is_valid
    assert len(errors) == 0


def test_geofence_enforcement():
    inside_coord = Position3D(x=10.0, y=8.0, alt=3.5)
    outside_coord = Position3D(x=25.0, y=8.0, alt=3.5)
    too_high_coord = Position3D(x=10.0, y=8.0, alt=6.0)

    assert is_within_geofence(inside_coord) is True
    assert is_within_geofence(outside_coord) is False
    assert is_within_geofence(too_high_coord) is False
