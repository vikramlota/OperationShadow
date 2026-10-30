from ultron.schemas.commands import Position2D, Position3D, ScoutAction, HawkAction
from ultron.schemas.mission import VehicleID

GEOFENCE_LIMITS = {
    "x_min": 0.0,
    "x_max": 20.0,
    "y_min": 0.0,
    "y_max": 15.0,
    "alt_max": 5.0,
}

SCOUT_ALLOWED_ACTIONS = {
    ScoutAction.EXPLORE.value,
    ScoutAction.NAVIGATE.value,
    ScoutAction.SCAN.value,
    ScoutAction.INSPECT.value,
    ScoutAction.FOLLOW.value,
    ScoutAction.RETURN_HOME.value,
    ScoutAction.PICK.value,
    ScoutAction.DROP.value,
    ScoutAction.STOP.value,
    ScoutAction.REPORT.value,
    "FIND_TARGET",
    "HOLD_POSITION",
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
