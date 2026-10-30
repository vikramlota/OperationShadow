from ultron.schemas.mission import MissionPlan, Task, VehicleID, TaskPriority


def get_fallback_plan(mission_id: str) -> MissionPlan:
    """
    Returns a hardcoded, perfectly validated MissionPlan for the rehearsed demo.
    Used strictly if the initial LLM call times out on demo day.
    """
    return MissionPlan(
        mission_id=mission_id,
        objective="Inspect the building, find the target, and report hazards.",
        rationale="USED CACHED PLAN — Live planning call timed out. Executing rehearsed demo initialization.",
        tasks=[
            Task(
                id="T-CACHE-01",
                vehicle=VehicleID.HAWK,
                type="HOVER_SCAN",
                area="zone_2",
                priority=TaskPriority.HIGH
            ),
            Task(
                id="T-CACHE-02",
                vehicle=VehicleID.SCOUT,
                type="EXPLORE",
                area="sector_A",
                priority=TaskPriority.MEDIUM
            ),
            Task(
                id="T-CACHE-03",
                vehicle=VehicleID.SCOUT,
                type="FIND_TARGET",
                target="red_package",
                priority=TaskPriority.LOW
            )
        ]
    )
