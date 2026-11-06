from typing import Dict, Any, Optional
from scout.navigation.avoidance import ReactiveObstacleAvoidance


class GroundNavigationController:
    """
    Deterministic ground navigation: waypoint following + reactive reverse & avoidance (TRD §15).
    Translates high-level actions (EXPLORE, NAVIGATE) into motor speed and steering vectors,
    reversing ('S') and rerouting when an obstacle blocks the forward path.
    """

    def __init__(self, safety_margin_cm: float = 30.0) -> None:
        self.avoidance = ReactiveObstacleAvoidance(min_distance_cm=safety_margin_cm)
        self.current_waypoint: Optional[Dict[str, float]] = None

    def plan_motion(
        self,
        target_x: float,
        target_y: float,
        current_x: float,
        current_y: float,
        front_clearance_cm: float,
        left_clearance_cm: float = 100.0,
        right_clearance_cm: float = 100.0,
    ) -> Dict[str, Any]:
        """Calculates motion command while respecting obstacle clearance, auto-reversing, and rerouting."""
        if not self.avoidance.evaluate(front_clearance_cm):
            steer_deg = -45.0 if left_clearance_cm >= right_clearance_cm else 45.0
            turn_cmd = "A" if steer_deg < 0 else "D"
            return {
                "action": "REVERSE_AND_AVOID",
                "serial_sequence": ["X", "S", turn_cmd, "W"],
                "speed": -0.4,
                "steer": steer_deg,
                "replan_required": True,
                "reason": "OBSTACLE_AVOIDANCE_REVERSE_AND_REROUTE",
            }

        dx = target_x - current_x
        dy = target_y - current_y
        return {
            "action": "MOVE_FORWARD",
            "serial_sequence": ["W"],
            "speed": 0.5,
            "target": {"x": target_x, "y": target_y},
            "delta": {"dx": dx, "dy": dy},
        }
