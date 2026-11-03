"""HAWK01 companion runtime entry point (simulated / roadmap stub, not integrated; TRD v2.0 §9, §26)."""

from __future__ import annotations

from hardware.hawk.communication import HawkTelemetryClient
from hardware.hawk.flight_interface import FlightControllerBridge
from hardware.hawk.sensors import HawkSensorMonitor


def main() -> None:
    """Initialize HAWK01 companion link stub components under hardware lockout defaults."""
    _bridge = FlightControllerBridge()
    _sensors = HawkSensorMonitor()
    _client = HawkTelemetryClient()


if __name__ == "__main__":
    main()
