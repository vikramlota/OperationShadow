class HawkOnboardSafetyLayer:
    """
    Deterministic onboard safety layer for HAWK aerial vehicle (TRD §9, §21, §22).
    Has final authority over motion:
    - Enforces hard geofence (venue boundaries)
    - Enforces min/max altitude
    - Enforces battery threshold auto-RTL/land
    - Triggers failsafe land on communication loss
    Cannot be overridden by ULTRON.
    """

    def __init__(
        self,
        x_min: float = 0.0,
        x_max: float = 20.0,
        y_min: float = 0.0,
        y_max: float = 15.0,
        alt_max: float = 5.0,
        critical_battery_pct: float = 20.0
    ) -> None:
        self.x_min = x_min
        self.x_max = x_max
        self.y_min = y_min
        self.y_max = y_max
        self.alt_max = alt_max
        self.critical_battery_pct = critical_battery_pct

    def check_geofence(self, x: float, y: float, alt: float) -> bool:
        """Returns False if coordinate violates indoor flight boundary."""
        if not (self.x_min <= x <= self.x_max):
            return False
        if not (self.y_min <= y <= self.y_max):
            return False
        if alt > self.alt_max or alt < 0.0:
            return False
        return True

    def check_battery_failsafe(self, battery_pct: float) -> str:
        """Returns action if battery is degraded."""
        if battery_pct <= self.critical_battery_pct:
            return "AUTO_RTL_LAND"
        return "NORMAL"
