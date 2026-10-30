import json
from typing import Callable, Optional, Dict, Any


class ScoutWebSocketClient:
    """
    Persistent authenticated WebSocket connection from SCOUT (Pi) to ULTRON laptop backend (TRD §19).
    Carries inbound structured commands and outbound heartbeat telemetry.
    """

    def __init__(self, server_url: str, auth_token: str) -> None:
        self.server_url = server_url
        self.auth_token = auth_token
        self.connected = False
        self.command_callback: Optional[Callable[[Dict[str, Any]], None]] = None

    def set_command_handler(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self.command_callback = callback

    def format_telemetry_payload(self, telemetry: Dict[str, Any]) -> str:
        """Packages telemetry with auth headers."""
        return json.dumps({
            "token": self.auth_token,
            "vehicle_id": "SCOUT01",
            "telemetry": telemetry
        })
