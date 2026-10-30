from typing import Dict, Any, Optional
from hawk.flight_interface.safety_layer import HawkOnboardSafetyLayer


class FlightControllerInterface:
    """
    Wraps existing Flight Controller firmware / telemetry bridge (TRD §9, §16).
    Executes pre-programmed waypoints, hover points, takeoff, RTL, and land.
    Does NOT perform free autonomous path-planning.
    """

    ALLOWED_ACTIONS = {"TAKEOFF", "PATROL", "HOVER_SCAN", "RETURN_HOME", "LAND", "STOP", "REPORT"}

    def __init__(self) -> None:
        self.safety = HawkOnboardSafetyLayer()
        self.current_state = "IDLE"

    def dispatch_action(self, action: str, target: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Dispatches validated flight action or rejects if geofence is violated."""
        if action not in self.ALLOWED_ACTIONS:
            return {"status": "REJECTED", "reason": f"Action {action} forbidden for HAWK"}

        if target:
            x = target.get("x", 0.0)
            y = target.get("y", 0.0)
            alt = target.get("alt", 0.0)
            if not self.safety.check_geofence(x, y, alt):
                return {"status": "REJECTED", "reason": "Target violates geofence boundaries"}

        self.current_state = action
        return {"status": "ACCEPTED", "action": action, "target": target}
