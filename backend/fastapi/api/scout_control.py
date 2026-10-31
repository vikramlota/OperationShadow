"""SCOUT01 keyboard control API with safety-gated serial dispatch."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, ValidationError
from serial import SerialException
from services.scout_control_simulator import ScoutControlSimulator

router = APIRouter(prefix="/scout/control", tags=["scout-control"])
websocket_router = APIRouter(tags=["scout-control"])


class ControlFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["command", "keepalive"]
    command: Literal["W", "A", "S", "D", "X"] | None = None


def get_simulator(request: Request) -> ScoutControlSimulator:
    simulator: ScoutControlSimulator | None = getattr(
        request.app.state, "scout_control_simulator", None
    )
    if simulator is None:
        raise RuntimeError("SCOUT01 control simulator is not initialized.")
    return simulator


@router.get("/status")
async def scout_control_status(
    simulator: Annotated[ScoutControlSimulator, Depends(get_simulator)],
) -> dict[str, str | bool | None]:
    """Return control mode, lease state, and optional serial transmitter status."""
    return await simulator.status()


@websocket_router.websocket("/ws/scout/control")
async def scout_control_websocket(websocket: WebSocket) -> None:
    """Accept browser control frames; dispatch serial only when every safety gate is clear."""
    simulator: ScoutControlSimulator = getattr(
        websocket.app.state, "scout_control_simulator"
    )
    await websocket.accept()
    client_id = await simulator.connect_client()

    try:
        await websocket.send_json({"type": "status", **(await simulator.status())})
        while True:
            raw_frame = await websocket.receive_text()
            try:
                frame = ControlFrame.model_validate_json(raw_frame)
            except ValidationError:
                await websocket.send_json(
                    {"type": "error", "message": "Invalid control frame."}
                )
                continue

            if frame.type == "keepalive":
                if frame.command is not None:
                    await websocket.send_json(
                        {"type": "error", "message": "Keepalive frames cannot include a command."}
                    )
                    continue
                if not await simulator.keepalive(client_id):
                    await websocket.send_json(
                        {"type": "error", "message": "This client does not own active movement; send X before requesting control."}
                    )
                continue

            if frame.command is None:
                await websocket.send_json(
                    {"type": "error", "message": "Command frames require W, A, S, D, or X."}
                )
                continue

            if not await simulator.update_command(client_id, frame.command):
                await websocket.send_json(
                    {"type": "error", "message": "Another client owns movement; send X before requesting control."}
                )
                continue
            await websocket.send_json(
                {"type": "status", **(await simulator.status())}
            )
    except SerialException as exc:
        await websocket.send_json(
            {"type": "error", "message": f"SCOUT01 command was not delivered: {exc}"}
        )
        await websocket.send_json(
            {"type": "status", **(await simulator.status())}
        )
    except WebSocketDisconnect:
        pass
    finally:
        await simulator.disconnect_client(client_id)
