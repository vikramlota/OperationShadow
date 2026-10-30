from typing import Tuple, Optional
from ultron.schemas.world import WorldModel


def check_replanning_triggers(world: WorldModel) -> Tuple[bool, Optional[str]]:
    """
    Evaluates the World Model to determine if a dynamic cross-vehicle replan is required.
    Returns (should_replan, trigger_rationale).
    """
    # 1. Unhandled Hazard Reported Overlapping Active Area
    unhandled_hazards = [h for h in world.environment.hazards if not getattr(h, "handled", False)]
    if unhandled_hazards:
        latest_hazard = unhandled_hazards[-1]
        return True, (
            f"Hazard '{latest_hazard.type}' reported by {latest_hazard.source_vehicle} "
            f"at ({latest_hazard.location.x}, {latest_hazard.location.y}) "
            f"(confidence {latest_hazard.confidence:.2f}). Rerouting active routes."
        )

    # 2. Target Discovered
    for obj in world.environment.objects:
        if not getattr(obj, "handled", False) and (obj.label == world.mission.objective or obj.label == "red_package"):
            return True, (
                f"Target '{obj.label}' located by {obj.source_vehicle} "
                f"at ({obj.location.x}, {obj.location.y}). Initiating mission completion sequence."
            )

    # 3. Path Blocked
    if world.environment.known_obstacles:
        latest_obstacle = world.environment.known_obstacles[-1]
        return True, f"SCOUT01 reported path blocked at ({latest_obstacle.x}, {latest_obstacle.y}). Rerouting required."

    # 4. Vehicle Degradation or Failure
    for vid, state in world.vehicles.items():
        if state.battery < 20.0:
            return True, f"Critical battery ({state.battery:.1f}%) reported by {vid}. Issuing safe return command."
        if state.connection == "OFFLINE":
            return True, f"Telemetry connection lost for {vid}. Reassigning critical tasks to remaining active vehicles."

    return False, None
