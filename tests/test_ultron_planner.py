from ultron.planner.cache import get_fallback_plan
from ultron.schemas.mission import VehicleID


def test_fallback_plan_generation():
    plan = get_fallback_plan("M-TEST-01")
    assert plan.mission_id == "M-TEST-01"
    assert len(plan.tasks) == 3
    assert plan.tasks[0].vehicle == VehicleID.SCOUT
    assert plan.tasks[0].type == "NAVIGATE"
    assert plan.tasks[1].vehicle == VehicleID.SCOUT
    assert plan.tasks[1].type == "EXPLORE"
    assert plan.tasks[2].vehicle == VehicleID.SCOUT
    assert plan.tasks[2].type == "INSPECT"
    assert "USED CACHED PLAN" in plan.rationale
