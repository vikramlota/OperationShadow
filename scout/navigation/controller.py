from typing import Dict, Any, Optional
from scout.navigation.avoidance import ReactiveObstacleAvoidance


class GroundNavigationController:
    """
    Deterministic ground navigation: waypoint following + reactive avoidance (TRD §15).
    Translates high-level actions (EXPLORE, NAVIGATE) into motor speed and steering vectors.
    """

    def __init__(self, safety_margin_cm: float = 30.0) -> None:
        self.avoidance = ReactiveObstacleAvoidance(min_distance_cm=safety_margin_cm)
        self.current_waypoint: Optional[Dict[str, float]] = None

    def plan_motion(self, target_x: float, target_y: float, current_x: float, current_y: float, front_clearance_cm: float) -> Dict[str, Any]:
        """Calculates motion command while respecting obstacle clearance."""
        if not self.avoidance.evaluate(front_clearance_cm):
            return {"action": "STOP_AND_ROTATE", "speed": 0.0, "steer": 45.0, "reason": "OBSTACLE_AVOIDANCE"}

        dx = target_x - current_x
        dy = target_y - current_y
        return {"action": "MOVE_FORWARD", "speed": 0.5, "target": {"x": target_x, "y": target_y}}
