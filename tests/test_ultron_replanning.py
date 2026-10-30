from ultron.schemas.world import WorldModel, MissionState, HazardEntity, ScoutState, HawkState
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import VehicleID
from ultron.evaluator.triggers import check_replanning_triggers


def test_hazard_trigger_activates_replan():
    world = WorldModel(mission=MissionState(id="M001", objective="test"))
    should_replan, _ = check_replanning_triggers(world)
    assert should_replan is False

    # Inject hazard reported by HAWK
    world.environment.hazards.append(
        HazardEntity(
            id="H01",
            location=Position2D(x=5.1, y=3.0),
            confidence=0.89,
            source_vehicle=VehicleID.HAWK,
            type="possible_hazard"
        )
    )

    should_replan, rationale = check_replanning_triggers(world)
    assert should_replan is True
    assert rationale is not None
    assert "HAWK01" in rationale
    assert "possible_hazard" in rationale


def test_battery_critical_trigger():
    world = WorldModel(mission=MissionState(id="M001", objective="test"))
    world.vehicles["SCOUT01"].battery = 15.0  # critical threshold is < 20%

    should_replan, rationale = check_replanning_triggers(world)
    assert should_replan is True
    assert rationale is not None
    assert "Critical battery" in rationale
