"""Gated USB serial output for the SCOUT01 ESP-NOW transmitter."""

from __future__ import annotations

import asyncio
import glob

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

    async def ensure_connected(self) -> bool:
        """Attempt to open the serial port (or auto-detect fallback USB serial ports) if armed."""
        if not self.armed:
            return False
        if self.connected:
            return True
        async with self._lock:
            if self._connection is not None and self._connection.is_open:
                return True
            candidate_ports = [self.port, "/dev/ttyACM1", "/dev/ttyACM0", "/dev/ttyUSB0"]
            candidate_ports.extend(sorted(glob.glob("/dev/cu.usbmodem*")))
            candidate_ports.extend(sorted(glob.glob("/dev/cu.usbserial*")))
            seen: set[str] = set()
            for candidate in candidate_ports:
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                try:
                    self.port = candidate
                    await asyncio.to_thread(self._open)
                    if self.connected:
                        logger.info("Connected to SCOUT01 ESP32 serial on %s", self.port)
                        return True
                except serial.SerialException:
                    await self._close_locked()
            logger.warning("SCOUT01 ESP32 serial transmitter not detected on any candidate port.")
            return False

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

    async def execute_step(self, command: str, duration_s: float, pause_s: float = 0.5) -> bool:
        """Send a movement command for duration_s followed by 'X' (stop) and a short settle pause."""
        if not await self.ensure_connected():
            await asyncio.sleep(min(duration_s, 0.25))
            return False
        try:
            await self.send(command)
            if duration_s > 0:
                await asyncio.sleep(duration_s)
            await self.send("X")
            if pause_s > 0:
                await asyncio.sleep(pause_s)
            return True
        except serial.SerialException:
            logger.exception("Serial write error during execute_step(%s)", command)
            try:
                await self.send("X")
            except Exception:
                pass
            return False

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
