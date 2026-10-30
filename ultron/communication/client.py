import json
from typing import Any, Dict, Optional
from ultron.schemas.commands import VehicleCommand
from ultron.communication.channels import CommunicationChannels


class VehicleCommunicationClient:
    """
    Handles structured message formatting and dispatch across vehicle WebSocket links
    per TRD v2.0 §19 & §20.
    """

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token

    def format_command_payload(self, command: VehicleCommand) -> str:
        """Serializes a validated vehicle command to JSON wire frame."""
        return command.model_dump_json()

    def parse_inbound_message(self, message: str) -> Dict[str, Any]:
        """Parses an inbound telemetry or observation JSON envelope."""
        return json.loads(message)
