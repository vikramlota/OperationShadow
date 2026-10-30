from ultron.schemas.commands import Position2D, Position3D, HawkAction
from ultron.schemas.mission import VehicleID

GEOFENCE_LIMITS = {
    "x_min": 0.0,
    "x_max": 20.0,
    "y_min": 0.0,
    "y_max": 15.0,
    "alt_max": 5.0,
}

HAWK_ALLOWED_ACTIONS = {
    HawkAction.TAKEOFF,
    HawkAction.PATROL,
    HawkAction.HOVER_SCAN,
    HawkAction.RETURN_HOME,
    HawkAction.LAND,
    HawkAction.STOP,
    HawkAction.REPORT,
}


def is_within_geofence(position: Position3D) -> bool:
    """Checks if a proposed 3D coordinate falls within the venue geofence."""
    if not (GEOFENCE_LIMITS["x_min"] <= position.x <= GEOFENCE_LIMITS["x_max"]):
        return False
    if not (GEOFENCE_LIMITS["y_min"] <= position.y <= GEOFENCE_LIMITS["y_max"]):
        return False
    if position.alt > GEOFENCE_LIMITS["alt_max"]:
        return False
    return True
