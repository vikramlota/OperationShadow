"""HAWK01 onboard sensor and battery voltage monitoring (simulated / roadmap stub, not integrated; TRD v2.0 §11, §21)."""

from __future__ import annotations


class HawkSensorMonitor:
    """Simulated / roadmap monitor stub for IMU, barometer altitude hold, and critical battery RTL threshold."""

    def __init__(self, critical_battery_pct: float = 20.0) -> None:
        self.critical_battery_pct = critical_battery_pct

    def requires_emergency_rtl(self, battery_pct: float) -> bool:
        """Return True if onboard battery level requires immediate deterministic RTL/land."""
        return battery_pct < self.critical_battery_pct
