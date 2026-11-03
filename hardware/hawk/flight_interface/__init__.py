"""Flight controller wrapper for HAWK01 fixed hover-point and waypoint execution (simulated / roadmap stub, not integrated; TRD v2.0 §9, §16)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HoverWaypoint:
    """Pre-configured indoor hover coordinate within the venue geofence (simulated / roadmap)."""

    name: str
    x: float
    y: float
    alt: float


class FlightControllerBridge:
    """Simulated / roadmap bridge stub to onboard flight controller firmware (not integrated in active runtime)."""

    def __init__(self, max_altitude_m: float = 5.0, min_battery_pct: float = 20.0) -> None:
        self.max_altitude_m = max_altitude_m
        self.min_battery_pct = min_battery_pct
        self.armed = False

    def validate_hover_target(self, waypoint: HoverWaypoint) -> bool:
        """Enforce onboard altitude ceiling prior to command dispatch."""
        return 0.0 <= waypoint.alt <= self.max_altitude_m
