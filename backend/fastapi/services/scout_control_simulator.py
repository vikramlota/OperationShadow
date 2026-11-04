"""SCOUT01 control state with optional safety-gated USB serial output."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Literal

import serial

from core.logging import get_logger
from services.scout_serial_writer import ScoutSerialWriter

ControlCommand = Literal["W", "A", "S", "D", "X"]
LEASE_SECONDS = 0.75
WATCHDOG_INTERVAL_SECONDS = 0.1
logger = get_logger("scout_control")


class ScoutControlSimulator:
    """Track one browser movement lease and optionally dispatch serial commands."""

    def __init__(self, serial_writer: ScoutSerialWriter | None = None) -> None:
        self._serial_writer = serial_writer
        self._command: ControlCommand = "X"
        self._clients: set[str] = set()
        self._movement_owner: str | None = None
        self._mission_executor_active: bool = False
        self._last_update: datetime | None = None
        self._lease_deadline = 0.0
        self._watchdog: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def serial_writer(self) -> ScoutSerialWriter | None:
        return self._serial_writer

    @property
    def mission_executor_active(self) -> bool:
        return self._mission_executor_active

    async def acquire_mission_executor_lease(self) -> None:
        """Reserve exclusive SCOUT01 control for the mission executor and clear any manual movement lease."""
        async with self._lock:
            self._mission_executor_active = True
            self._command = "X"
            self._movement_owner = None
            self._lease_deadline = 0.0

    async def release_mission_executor_lease(self) -> None:
        """Release the mission executor lease after mission execution finishes."""
        async with self._lock:
            self._mission_executor_active = False
            self._command = "X"
            self._movement_owner = None
            self._lease_deadline = 0.0

    async def start(self) -> None:
        if self._serial_writer is not None:
            await self._serial_writer.start()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._run_watchdog())

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop_locked()
            self._clients.clear()
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            self._watchdog = None
        if self._serial_writer is not None:
            await self._serial_writer.close()

    async def connect_client(self) -> str:
        async with self._lock:
            client_id = secrets.token_urlsafe(16)
            self._clients.add(client_id)
            return client_id

    async def disconnect_client(self, client_id: str) -> None:
        async with self._lock:
            self._clients.discard(client_id)
            if self._movement_owner == client_id:
                await self._stop_locked()

    async def update_command(self, client_id: str, command: ControlCommand) -> bool:
        async with self._lock:
            if client_id not in self._clients:
                return False
            if (
                self._serial_writer is not None
                and self._serial_writer.armed
                and not self._serial_writer.connected
            ):
                await self._serial_writer.ensure_connected()
            if command == "X":
                await self._stop_locked(propagate_serial_error=True)
            else:
                if self._mission_executor_active or self._movement_owner not in (None, client_id):
                    return False
                try:
                    await self._send_serial(command)
                except serial.SerialException:
                    await self._stop_locked()
                    raise
                self._movement_owner = client_id
                self._command = command
                self._last_update = datetime.now(timezone.utc)
                self._lease_deadline = asyncio.get_running_loop().time() + LEASE_SECONDS
            return True

    async def keepalive(self, client_id: str) -> bool:
        async with self._lock:
            if client_id not in self._clients:
                return False
            if self._mission_executor_active:
                return False
            if self._command != "X":
                if self._movement_owner != client_id:
                    return False
                try:
                    await self._send_serial(self._command)
                except serial.SerialException:
                    await self._stop_locked()
                    raise
                self._last_update = datetime.now(timezone.utc)
                self._lease_deadline = asyncio.get_running_loop().time() + LEASE_SECONDS
            return True

    async def status(self) -> dict[str, str | bool | None]:
        async with self._lock:
            if (
                self._serial_writer is not None
                and self._serial_writer.armed
                and not self._serial_writer.connected
            ):
                await self._serial_writer.ensure_connected()
            return {
                "mode": (
                    "serial"
                    if self._serial_writer is not None and self._serial_writer.armed
                    else "simulation_only"
                ),
                "transmitter_connected": bool(
                    self._serial_writer is not None and self._serial_writer.connected
                ),
                "control_client_connected": bool(self._clients),
                "current_command": self._command,
                "last_update": self._last_update.isoformat() if self._last_update else None,
                "physical_control_available": bool(
                    self._serial_writer is not None
                    and self._serial_writer.armed
                    and self._serial_writer.connected
                ),
            }

    async def _run_watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            async with self._lock:
                if (
                    self._command != "X"
                    and asyncio.get_running_loop().time() >= self._lease_deadline
                ):
                    await self._stop_locked()

    async def _stop_locked(self, *, propagate_serial_error: bool = False) -> None:
        self._command = "X"
        self._movement_owner = None
        self._lease_deadline = 0.0
        self._last_update = datetime.now(timezone.utc)
        try:
            await self._send_serial("X")
        except serial.SerialException:
            if propagate_serial_error:
                raise
            logger.exception("Could not transmit SCOUT01 X stop command.")

    async def _send_serial(self, command: ControlCommand) -> None:
        if self._serial_writer is not None:
            await self._serial_writer.send(command)
