from typing import Optional

from ultron.schemas.mission import MissionPlan, Task, TaskPriority, VehicleID

DEFAULT_FALLBACK_OBJECTIVE = "Inspect the building, find the target, and report hazards."


def get_fallback_plan(
    mission_id: str,
    objective: Optional[str] = None,
) -> MissionPlan:
    """
    Returns a hardcoded, perfectly validated MissionPlan for the rehearsed demo.
    When `objective` is provided and non-empty, uses that objective; otherwise
    falls back to `DEFAULT_FALLBACK_OBJECTIVE`.
    """
    resolved_objective = (
        objective.strip()
        if isinstance(objective, str) and objective.strip()
        else DEFAULT_FALLBACK_OBJECTIVE
    )
    return MissionPlan(
        mission_id=mission_id,
        objective=resolved_objective,
        rationale="USED CACHED PLAN — Live planning call timed out. Executing rehearsed demo initialization.",
        tasks=[
            Task(
                id="T-CACHE-01",
                vehicle=VehicleID.SCOUT,
                type="NAVIGATE",
                area="zone_2",
                priority=TaskPriority.HIGH,
            ),
            Task(
                id="T-CACHE-02",
                vehicle=VehicleID.SCOUT,
                type="EXPLORE",
                area="sector_A",
                priority=TaskPriority.MEDIUM,
            ),
            Task(
                id="T-CACHE-03",
                vehicle=VehicleID.SCOUT,
                type="INSPECT",
                target="red_package",
                priority=TaskPriority.LOW,
            ),
        ],
    )
