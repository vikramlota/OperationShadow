import asyncio

import pytest
from pydantic import ValidationError

from api.scout_control import ControlFrame
from services.scout_control_simulator import ScoutControlSimulator


@pytest.mark.asyncio
async def test_control_has_single_movement_owner_and_x_has_priority() -> None:
    simulator = ScoutControlSimulator()
    await simulator.start()
    try:
        first_client = await simulator.connect_client()
        second_client = await simulator.connect_client()
        assert first_client != second_client

        assert await simulator.update_command(first_client, "W")
        assert not await simulator.update_command(second_client, "A")
        assert await simulator.update_command(second_client, "X")
        assert (await simulator.status())["current_command"] == "X"
        assert await simulator.update_command(second_client, "D")
        assert (await simulator.status())["current_command"] == "D"

        assert await simulator.update_command(first_client, "X")
        assert await simulator.update_command(first_client, "W")
        assert (await simulator.status())["current_command"] == "W"

        assert await simulator.update_command(first_client, "X")
        status = await simulator.status()
        assert status["current_command"] == "X"
        assert status["transmitter_connected"] is False
        assert status["physical_control_available"] is False

        with pytest.raises(ValidationError):
            ControlFrame.model_validate({"type": "command", "command": "STOP"})
    finally:
        await simulator.shutdown()


@pytest.mark.asyncio
async def test_control_lease_timeout_and_disconnect_force_x_without_resuming() -> None:
    simulator = ScoutControlSimulator()
    await simulator.start()
    try:
        client_id = await simulator.connect_client()
        await simulator.update_command(client_id, "D")
        await asyncio.sleep(0.9)
        assert (await simulator.status())["current_command"] == "X"

        await simulator.update_command(client_id, "A")
        await simulator.disconnect_client(client_id)
        status = await simulator.status()
        assert status["current_command"] == "X"
        assert status["control_client_connected"] is False

        new_client = await simulator.connect_client()
        assert (await simulator.status())["current_command"] == "X"
        assert await simulator.update_command(new_client, "S")
    finally:
        await simulator.shutdown()
