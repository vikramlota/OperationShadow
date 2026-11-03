from __future__ import annotations

import math
from typing import List, Tuple

from ultron.safety.constraints import SCOUT_ALLOWED_ACTIONS
from ultron.schemas.mission import MissionPlan, VehicleID
from ultron.schemas.world import WorldModel

_PASSIVE_HOLD_ACTIONS = {"STOP", "REPORT"}


class SafetyViolation(Exception):
    """Raised when a proposed plan violates deterministic constraints."""

    pass


def validate_plan(plan: MissionPlan, world: WorldModel) -> Tuple[bool, List[str]]:
    """Validate a proposed MissionPlan against single-vehicle SCOUT01 constraints,
    World Model state, battery/connectivity health, Emergency Stop override, and evidence grounding.

    Returns (is_valid, list_of_errors).
    """
    errors: List[str] = []

    # 0. Verify SCOUT01 exists in the World Model and inspect its safety/health state
    scout = world.vehicles.get("SCOUT01")
    if scout is None:
        errors.append(
            "Vehicle Missing: SCOUT01 does not exist in the World Model; mission cannot be approved."
        )
        return False, errors

    conn_raw = getattr(scout, "connection", None)
    conn_str = (
        getattr(conn_raw, "value", str(conn_raw)).strip().upper()
        if conn_raw is not None
        else ""
    )
    status_raw = getattr(scout, "status", None)
    status_str = str(status_raw).strip().upper() if status_raw is not None else ""

    # Emergency Stop is an overriding condition blocking mission approval/execution
    if status_str == "EMERGENCY_STOPPED":
        errors.append(
            "Emergency Stop Active: SCOUT01 is EMERGENCY_STOPPED; mission tasks cannot be approved until an explicit reset."
        )

    if not conn_str:
        errors.append(
            "Vehicle Connection Unavailable: SCOUT01 connection status is missing."
        )

    bat_raw: object = getattr(scout, "battery", None)
    if (
        bat_raw is not None
        and not isinstance(bat_raw, bool)
        and isinstance(bat_raw, (int, float))
        and math.isfinite(float(bat_raw))
        and 0.0 <= float(bat_raw) <= 100.0
    ):
        battery_valid = True
        battery_val = float(bat_raw)
    else:
        battery_valid = False
        battery_val = -1.0
        errors.append(
            f"Battery Telemetry Unavailable/Invalid: SCOUT01 battery value ({bat_raw!r}) is missing or non-finite."
        )

    # Extract vocabulary from mission objective to ground references
    obj_words = set(
        world.mission.objective.lower().replace(",", " ").replace(".", " ").split()
    )
    standard_areas = {
        "sector_a",
        "sector_b",
        "zone_1",
        "zone_2",
        "corridor",
        "building",
        "arena",
    }
    known_areas = (
        set(a.lower() for a in world.environment.explored_areas)
        .union(standard_areas)
        .union(obj_words)
    )

    standard_targets = {
        "red_package",
        "target",
        "hazards",
        "hazard",
        "package",
        "thermal_anomaly",
        "obstacle",
    }
    tracked_labels = {
        t.label.lower() for t in world.environment.tracked_objects.values()
    }
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
        action_upper = (task.type or "").strip().upper()
        is_movement_or_active = action_upper not in _PASSIVE_HOLD_ACTIONS

        # 1. Contradictory Task Check: No vehicle can execute identical concurrent actions
        task_signature = (task.vehicle, action_upper)
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
        elif action_upper not in SCOUT_ALLOWED_ACTIONS:
            errors.append(
                f"Action Compatibility: Task type '{task.type}' is not a permitted action for SCOUT01."
            )

        # 3. Reference Grounding Check
        if task.area:
            task_area_clean = task.area.lower().strip()
            if task_area_clean not in known_areas and not any(
                w in task_area_clean for w in known_areas
            ):
                errors.append(
                    f"Hallucination: Area '{task.area}' does not exist in the World Model."
                )

        if task.target:
            task_target_clean = task.target.lower().strip()
            if task_target_clean not in known_targets and not any(
                w in task_target_clean for w in known_targets
            ):
                errors.append(
                    f"Hallucination: Target '{task.target}' has not been observed."
                )

        # 4. Task Evidence Reference Check
        for teid in getattr(task, "evidence_ids", []):
            if teid not in valid_evidence_ids:
                errors.append(
                    f"Hallucination: Task '{task.id}' references nonexistent evidence ID '{teid}'."
                )

        # 5. SCOUT01 Connection, Status & Battery Safety Checks
        if conn_str not in {"ONLINE", "CONNECTED"} and is_movement_or_active:
            errors.append(
                f"Vehicle Connection Unsafe: SCOUT01 connection is '{conn_str or 'MISSING'}' "
                f"(must be ONLINE/CONNECTED) to execute task '{task.id}' ({task.type})."
            )

        if status_str in {"OFFLINE", "UNKNOWN", "DISCONNECTED", "FAULT"} and is_movement_or_active:
            errors.append(
                f"Vehicle State Invalid: SCOUT01 status is '{status_str}'; "
                f"cannot execute movement task '{task.id}' ({task.type})."
            )

        if not getattr(scout, "pose_valid", True) and is_movement_or_active:
            errors.append(
                f"Vehicle Pose Invalid: SCOUT01 pose_valid is False; "
                f"cannot execute movement task '{task.id}' ({task.type})."
            )

        if battery_valid and battery_val <= 20.0 and is_movement_or_active:
            errors.append(
                f"Low Battery Safety Violation: SCOUT01 battery is {battery_val:.1f}% (<= 20.0%); "
                f"cannot execute '{task.type}'."
            )

    is_valid = len(errors) == 0
    return is_valid, errors
