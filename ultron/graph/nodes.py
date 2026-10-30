from typing import Any, Dict
from ultron.state import UltronState
from ultron.planner.engine import invoke_gemini_planner
from ultron.safety.validator import validate_plan
from ultron.safety.repairs import build_repair_prompt
from ultron.evaluator.triggers import check_replanning_triggers


def node_plan(state: UltronState) -> Dict[str, Any]:
    """Generates a mission plan using the Gemini planner or cached fallback."""
    plan = invoke_gemini_planner(
        world=state["world"],
        repair_prompt=state.get("repair_prompt"),
        trigger_rationale=state.get("trigger_rationale")
    )
    return {"proposed_plan": plan}


def node_validate(state: UltronState) -> Dict[str, Any]:
    """Evaluates the proposed plan against physical constraints and known world state."""
    proposed_plan = state.get("proposed_plan")
    if not proposed_plan:
        return {
            "validation_errors": ["No proposed plan to validate."],
            "repair_prompt": "Please generate a valid mission plan.",
            "retry_count": state.get("retry_count", 0) + 1
        }

    is_valid, errors = validate_plan(proposed_plan, state["world"])

    if is_valid:
        return {
            "validation_errors": [],
            "repair_prompt": None,
            "retry_count": 0
        }

    repair = build_repair_prompt(proposed_plan.rationale, errors)
    current_retries = state.get("retry_count", 0)

    return {
        "validation_errors": errors,
        "repair_prompt": repair,
        "retry_count": current_retries + 1
    }


def node_assign(state: UltronState) -> Dict[str, Any]:
    """Dispatches the validated plan's tasks to SCOUT and HAWK and updates task tracking."""
    valid_plan = state.get("proposed_plan")
    world = state["world"]
    if valid_plan:
        world.mission.tasks.active.clear()
        for task in valid_plan.tasks:
            world.mission.tasks.active[task.vehicle] = task.id

    # Mark active hazards/objects as handled so they do not continuously retrigger replanning
    for h in world.environment.hazards:
        h.handled = True
    for o in world.environment.objects:
        o.handled = True

    return {"world": world}


def node_evaluate(state: UltronState) -> Dict[str, Any]:
    """Checks physical triggers in the environment to determine if replanning is needed."""
    should_replan, rationale = check_replanning_triggers(state["world"])
    return {"trigger_rationale": rationale if should_replan else None}


def node_degraded(state: UltronState) -> Dict[str, Any]:
    """Issues safe default commands (HOLD POSITION) if retries are exhausted."""
    return {"validation_errors": [], "repair_prompt": None}
