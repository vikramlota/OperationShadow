import pytest

from services.scout_control_simulator import ScoutControlSimulator
from services.scout_serial_writer import ScoutSerialWriter


class FakeSerial:
    def __init__(self) -> None:
        self.is_open = True
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.is_open = False


@pytest.mark.asyncio
async def test_serial_writer_sends_single_byte_commands_without_newlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import serial

    fake_connection = FakeSerial()
    monkeypatch.setattr(serial, "Serial", lambda **_kwargs: fake_connection)
    writer = ScoutSerialWriter(
        port="/dev/fake-scout",
        baudrate=115200,
        enabled=True,
        hardware_lockout=False,
        real_mode=True,
        failsafe_verified=True,
    )

    await writer.start()
    await writer.send("W")
    await writer.send("X")
    assert fake_connection.writes == [b"X", b"W", b"X"]
    await writer.close()
    assert not writer.connected


@pytest.mark.asyncio
async def test_serial_writer_never_opens_when_any_hardware_gate_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import serial

    def unexpected_open(**_kwargs: object) -> FakeSerial:
        raise AssertionError("Serial port must not open while safety gates are closed.")

    monkeypatch.setattr(serial, "Serial", unexpected_open)
    writer = ScoutSerialWriter(
        port="/dev/fake-scout",
        baudrate=115200,
        enabled=True,
        hardware_lockout=True,
        real_mode=True,
        failsafe_verified=True,
    )

    await writer.start()
    await writer.send("W")
    assert not writer.connected
    assert not writer.armed


@pytest.mark.asyncio
async def test_control_lease_repeats_movement_and_sends_x_to_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import serial

    fake_connection = FakeSerial()
    monkeypatch.setattr(serial, "Serial", lambda **_kwargs: fake_connection)
    writer = ScoutSerialWriter(
        port="/dev/fake-scout",
        baudrate=115200,
        enabled=True,
        hardware_lockout=False,
        real_mode=True,
        failsafe_verified=True,
    )
    control = ScoutControlSimulator(writer)
    await control.start()
    try:
        client_id = await control.connect_client()
        assert await control.update_command(client_id, "W")
        assert await control.keepalive(client_id)
        assert await control.update_command(client_id, "X")
        assert fake_connection.writes == [b"X", b"W", b"W", b"X"]
    finally:
        await control.shutdown()
