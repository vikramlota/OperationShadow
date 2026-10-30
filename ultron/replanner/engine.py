from typing import Optional
from ultron.schemas.mission import MissionPlan
from ultron.schemas.world import WorldModel
from ultron.evaluator.triggers import check_replanning_triggers
from ultron.planner.engine import invoke_gemini_planner


def evaluate_and_replan(
    world: WorldModel,
    repair_prompt: Optional[str] = None
) -> Optional[MissionPlan]:
    """
    Evaluates trigger conditions on the World Model.
    If a trigger condition is met, invokes the replanner with the trigger rationale.
    """
    should_replan, rationale = check_replanning_triggers(world)
    if not should_replan and not repair_prompt:
        return None

    return invoke_gemini_planner(
        world=world,
        repair_prompt=repair_prompt,
        trigger_rationale=rationale
    )
