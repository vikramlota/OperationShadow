"""Gated USB serial output for the SCOUT01 ESP-NOW transmitter."""

from __future__ import annotations

import asyncio

import serial

from core.logging import get_logger

logger = get_logger("scout_serial_writer")


class ScoutSerialWriter:
    """Write single-byte commands only when the hardware safety gates are cleared."""

    def __init__(
        self,
        *,
        port: str,
        baudrate: int,
        enabled: bool,
        hardware_lockout: bool,
        real_mode: bool,
        failsafe_verified: bool,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.armed = (
            enabled
            and not hardware_lockout
            and real_mode
            and failsafe_verified
        )
        self._connection: serial.Serial | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._connection.is_open

    async def start(self) -> None:
        if not self.armed:
            logger.info(
                "SCOUT01 serial writes remain disabled by configuration or safety gates."
            )
            return
        try:
            await asyncio.to_thread(self._open)
            await self.send("X")
        except serial.SerialException:
            logger.exception("Could not open or initialize SCOUT01 serial port %s.", self.port)
            await self.close()

    async def send(self, command: str) -> None:
        if command not in {"W", "A", "S", "D", "X"}:
            raise ValueError("SCOUT01 serial command must be W, A, S, D, or X.")
        if not self.armed:
            return
        async with self._lock:
            connection = self._connection
            if connection is None or not connection.is_open:
                raise serial.SerialException(
                    f"SCOUT01 serial transmitter is offline on {self.port}."
                )
            try:
                written = await asyncio.to_thread(
                    connection.write, command.encode("ascii")
                )
                if written != 1:
                    raise serial.SerialException(
                        f"Short write to SCOUT01 serial port {self.port}: {written} byte(s)."
                    )
                await asyncio.to_thread(connection.flush)
            except serial.SerialException:
                await self._close_locked()
                raise

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    def _open(self) -> None:
        self._connection = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=0.1,
            write_timeout=0.5,
        )

    async def _close_locked(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None and connection.is_open:
            try:
                await asyncio.to_thread(connection.close)
            except serial.SerialException:
                logger.exception("Error closing SCOUT01 serial port %s.", self.port)
