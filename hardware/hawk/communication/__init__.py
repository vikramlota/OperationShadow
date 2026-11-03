"""HAWK01 companion link WebSocket telemetry and command client (simulated / roadmap stub, not integrated; TRD v2.0 §9, §19)."""

from __future__ import annotations


class HawkTelemetryClient:
    """Simulated / roadmap companion link client stub for HAWK01 position, altitude, and battery."""

    def __init__(self, vehicle_id: str = "HAWK01", camera_port: int = 8082) -> None:
        self.vehicle_id = vehicle_id
        self.camera_port = camera_port
