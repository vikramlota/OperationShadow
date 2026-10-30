from typing import Tuple, List
from ultron.schemas.mission import MissionPlan, VehicleID
from ultron.schemas.world import WorldModel
from ultron.safety.constraints import SCOUT_ALLOWED_ACTIONS


class SafetyViolation(Exception):
    """Raised when a proposed plan violates deterministic constraints."""
    pass


def validate_plan(plan: MissionPlan, world: WorldModel) -> Tuple[bool, List[str]]:
    """
    Validates a proposed MissionPlan against physical constraints, known world state,
    and evidence-reference integrity.
    Returns (is_valid, list_of_errors).
    """
    errors: List[str] = []

    # Extract vocabulary from mission objective to ground references
    obj_words = set(world.mission.objective.lower().replace(",", " ").replace(".", " ").split())
    standard_areas = {"sector_a", "sector_b", "zone_1", "zone_2", "corridor", "building", "arena"}
    known_areas = set(a.lower() for a in world.environment.explored_areas).union(standard_areas).union(obj_words)

    standard_targets = {"red_package", "target", "hazards", "hazard", "package", "thermal_anomaly", "obstacle"}
    tracked_labels = {t.label.lower() for t in world.environment.tracked_objects.values()}
    known_targets = (
        set(obj.label.lower() for obj in world.environment.objects)
        .union(tracked_labels)
        .union(standard_targets)
        .union(obj_words)
    )

    valid_evidence_ids = {
        o.observation_id for o in world.environment.observation_history
    }.union(set(world.environment.tracked_objects.keys()))

    # Verify any plan-level evidence references exist in the World Model
    for eid in getattr(plan, "evidence_ids", []):
        if eid not in valid_evidence_ids:
            errors.append(
                f"Hallucination: Evidence ID '{eid}' does not exist in the World Model."
            )

    seen_task_types = set()

    for task in plan.tasks:
        # 1. Contradictory Task Check: No vehicle can execute identical concurrent actions
        task_signature = (task.vehicle, task.type.upper())
        if task_signature in seen_task_types:
            errors.append(
                f"Contradiction: {task.vehicle} is assigned duplicate concurrent task '{task.type}'."
            )
        seen_task_types.add(task_signature)

        # 2. Vehicle/Action Compatibility Check (Single-vehicle SCOUT01 deployment)
        if task.vehicle != VehicleID.SCOUT and str(task.vehicle) != "SCOUT01":
            errors.append(
                f"Vehicle Compatibility: Vehicle '{task.vehicle}' is not present. "
                "Only SCOUT01 is active in this deployment."
            )
        elif task.type.upper() not in SCOUT_ALLOWED_ACTIONS:
            errors.append(
                f"Action Compatibility: Task type '{task.type}' is not a permitted action for SCOUT01."
            )

        # 3. Reference Grounding Check
        if task.area:
            task_area_clean = task.area.lower().strip()
            if task_area_clean not in known_areas and not any(w in task_area_clean for w in known_areas):
                errors.append(f"Hallucination: Area '{task.area}' does not exist in the World Model.")

        if task.target:
            task_target_clean = task.target.lower().strip()
            if task_target_clean not in known_targets and not any(w in task_target_clean for w in known_targets):
                errors.append(f"Hallucination: Target '{task.target}' has not been observed.")

        # 4. Task Evidence Reference Check
        for teid in getattr(task, "evidence_ids", []):
            if teid not in valid_evidence_ids:
                errors.append(
                    f"Hallucination: Task '{task.id}' references nonexistent evidence ID '{teid}'."
                )

    is_valid = len(errors) == 0
    return is_valid, errors
