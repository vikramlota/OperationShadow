from typing import Tuple, List
from ultron.schemas.mission import MissionPlan, VehicleID
from ultron.schemas.world import WorldModel
from ultron.safety.constraints import HAWK_ALLOWED_ACTIONS


class SafetyViolation(Exception):
    """Raised when a proposed plan violates deterministic constraints."""
    pass


def validate_plan(plan: MissionPlan, world: WorldModel) -> Tuple[bool, List[str]]:
    """
    Validates a proposed MissionPlan against physical constraints and known world state.
    Returns (is_valid, list_of_errors).
    """
    errors: List[str] = []
    active_priorities = set()

    for task in plan.tasks:
        # 1. Contradictory Task Check: No vehicle can execute multiple concurrent tasks at the same priority
        vehicle_priority_key = (task.vehicle, task.priority)
        if vehicle_priority_key in active_priorities:
            errors.append(
                f"Contradiction: {task.vehicle} is assigned multiple conflicting tasks at priority {task.priority}."
            )
        active_priorities.add(vehicle_priority_key)

        # 2. Vehicle/Action Compatibility Check
        if task.vehicle == VehicleID.HAWK:
            allowed_hawk_values = [action.value for action in HAWK_ALLOWED_ACTIONS]
            if task.type.upper() not in allowed_hawk_values:
                errors.append(
                    f"Action Compatibility: {task.type} is strictly forbidden for HAWK01. "
                    f"Must be one of: {allowed_hawk_values}"
                )

        # 3. Reference Grounding Check
        if task.area:
            known_areas = set(world.environment.explored_areas + ["sector_A", "zone_2"])
            if task.area not in known_areas:
                errors.append(f"Hallucination: Area '{task.area}' does not exist in the World Model.")

        if task.target:
            known_targets = set([obj.label for obj in world.environment.objects] + ["red_package"])
            if task.target not in known_targets:
                errors.append(f"Hallucination: Target '{task.target}' has not been observed.")

    is_valid = len(errors) == 0
    return is_valid, errors
