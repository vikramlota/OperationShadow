"""WebSocket gateway modules (§3, §5, §10)."""

from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway
from websocket.vehicle_gateway import VehicleGatewayFastPath, vehicle_gateway_fast_path

__all__ = [
    "DashboardGateway",
    "VehicleGatewayFastPath",
    "dashboard_gateway",
    "vehicle_gateway_fast_path",
]
