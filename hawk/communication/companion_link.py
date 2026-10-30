import json
from typing import Callable, Optional, Dict, Any


class HawkCompanionLink:
    """
    Wi-Fi telemetry WebSocket companion link to ULTRON laptop backend (TRD §9, §19).
    Receives waypoint/hover/land/RTL commands and pushes authoritative flight telemetry.
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
            "vehicle_id": "HAWK01",
            "telemetry": telemetry
        })
