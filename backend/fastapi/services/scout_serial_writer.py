"""Gated USB serial output for the SCOUT01 ESP-NOW transmitter with latched Emergency Stop.

Hardware Safety Architecture:
1. Physical serial writes remain strictly disabled unless all four safety gates are cleared:
   `enabled=True`, `hardware_lockout=False`, `real_mode=True`, and `failsafe_verified=True`.
2. Authoritative Emergency Stop Latch (`latch_global_emergency_stop` / `latch_emergency_stop`):
   - Once Emergency Stop is latched, ALL movement commands (`W`, `A`, `S`, `D`) are
     blocked inside `ScoutSerialWriter.send()` immediately with zero timing gap,
     until an explicit authorized reset (`reset_global_emergency_stop(authorized=True)`).
   - Any in-flight step delay (`execute_step` or `wait_unless_emergency_stopped`)
     wakes up immediately when Emergency Stop is triggered rather than waiting for
     a sleep timer to expire.
3. Stop-Command Fault Reporting:
   - Never silently treats a failed `'X'` (stop) write as successful stopping.
   - Raises `SerialStopCommandFaultError` and logs a `CRITICAL` fault if transmitting `'X'`
     fails while armed or connected.
4. Independent Hardware/Firmware Requirement:
   - Software E-Stop and serial `'X'` frames are supplementary; physical deployment
     requires an independent onboard ESP32/Scout Mini dead-man watchdog timer (`< 500 ms`)
     and physical hardware E-Stop kill switch.
"""

from __future__ import annotations

import asyncio
import glob

import serial

from core.logging import get_logger

logger = get_logger("scout_serial_writer")

_GLOBAL_ESTOP_LATCHED: bool = False
_GLOBAL_ESTOP_SIGNALS: set[asyncio.Event] = set()


class EmergencyStopLatchedError(RuntimeError):
    """Raised when a movement command is attempted while Emergency Stop is latched."""


class SerialStopCommandFaultError(RuntimeError):
    """Raised when sending the hardware stop command ('X') fails over serial."""


def is_global_emergency_stop_latched() -> bool:
    """Return True if the process-wide Emergency Stop latch is active."""
    return _GLOBAL_ESTOP_LATCHED


def latch_global_emergency_stop() -> None:
    """Immediately latch Emergency Stop across all ScoutSerialWriter instances and wake waiting tasks."""
    global _GLOBAL_ESTOP_LATCHED
    _GLOBAL_ESTOP_LATCHED = True
    for ev in list(_GLOBAL_ESTOP_SIGNALS):
        try:
            ev.set()
        except Exception:
            pass


def reset_global_emergency_stop(*, authorized: bool = False) -> None:
    """Clear the global Emergency Stop latch only upon explicit authorized operator reset."""
    global _GLOBAL_ESTOP_LATCHED
    if not authorized:
        raise PermissionError(
            "Resetting the Emergency Stop latch requires explicit authorization (authorized=True)."
        )
    _GLOBAL_ESTOP_LATCHED = False
    for ev in list(_GLOBAL_ESTOP_SIGNALS):
        try:
            ev.clear()
        except Exception:
            pass


async def wait_unless_emergency_stopped(duration_s: float) -> bool:
    """Sleep for `duration_s` seconds, returning False immediately if Emergency Stop is latched."""
    if _GLOBAL_ESTOP_LATCHED:
        return False
    if duration_s <= 0.0:
        return not _GLOBAL_ESTOP_LATCHED

    ev = asyncio.Event()
    _GLOBAL_ESTOP_SIGNALS.add(ev)
    try:
        if _GLOBAL_ESTOP_LATCHED:
            return False
        await asyncio.wait_for(ev.wait(), timeout=duration_s)
        return False
    except asyncio.TimeoutError:
        return not _GLOBAL_ESTOP_LATCHED
    finally:
        _GLOBAL_ESTOP_SIGNALS.discard(ev)


class ScoutSerialWriter:
    """Write single-byte commands only when hardware safety gates are cleared and E-Stop is not latched."""

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
        self._local_estop_latched: bool = False
        self.stop_fault_count: int = 0
        self.last_stop_fault_reason: str | None = None

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._connection.is_open

    @property
    def emergency_stop_latched(self) -> bool:
        return self._local_estop_latched or _GLOBAL_ESTOP_LATCHED

    def latch_emergency_stop(self) -> None:
        """Latch Emergency Stop on this writer and globally."""
        self._local_estop_latched = True
        latch_global_emergency_stop()

    def reset_emergency_stop(self, *, authorized: bool = False) -> None:
        """Reset Emergency Stop latch when explicitly authorized."""
        if not authorized:
            raise PermissionError(
                "Resetting Emergency Stop on ScoutSerialWriter requires authorized=True."
            )
        self._local_estop_latched = False
        reset_global_emergency_stop(authorized=True)

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
            logger.exception(
                "Could not open or initialize SCOUT01 serial port %s.", self.port
            )
            await self.close()

    async def ensure_connected(self) -> bool:
        """Attempt to open the serial port (or auto-detect fallback USB serial ports) if armed."""
        if not self.armed or self.emergency_stop_latched:
            return False
        if self.connected:
            return True
        async with self._lock:
            if self._connection is not None and self._connection.is_open:
                return True
            candidate_ports = [
                self.port,
                "/dev/ttyACM1",
                "/dev/ttyACM0",
                "/dev/ttyUSB0",
            ]
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
                        logger.info(
                            "Connected to SCOUT01 ESP32 serial on %s", self.port
                        )
                        return True
                except serial.SerialException:
                    await self._close_locked()
            logger.warning(
                "SCOUT01 ESP32 serial transmitter not detected on any candidate port."
            )
            return False

    async def send(self, command: str) -> None:
        if command not in {"W", "A", "S", "D", "X"}:
            raise ValueError("SCOUT01 serial command must be W, A, S, D, or X.")

        # Block any movement command immediately if Emergency Stop is latched
        if command != "X" and self.emergency_stop_latched:
            msg = (
                f"Blocked movement command '{command}' on {self.port}: "
                "Emergency Stop is latched."
            )
            logger.critical(msg)
            raise EmergencyStopLatchedError(msg)

        if not self.armed:
            return

        async with self._lock:
            # Re-verify latch inside lock right before hardware write to eliminate any timing gap
            if command != "X" and self.emergency_stop_latched:
                msg = (
                    f"Blocked movement command '{command}' inside serial lock on {self.port}: "
                    "Emergency Stop is latched."
                )
                logger.critical(msg)
                raise EmergencyStopLatchedError(msg)

            connection = self._connection
            if connection is None or not connection.is_open:
                err_msg = f"SCOUT01 serial transmitter is offline on {self.port}."
                if command == "X":
                    self.stop_fault_count += 1
                    self.last_stop_fault_reason = err_msg
                    logger.critical(
                        "CRITICAL STOP FAULT: Cannot transmit stop command 'X' — %s",
                        err_msg,
                    )
                raise serial.SerialException(err_msg)
            try:
                written = await asyncio.to_thread(
                    connection.write, command.encode("ascii")
                )
                if written != 1:
                    short_msg = (
                        f"Short write to SCOUT01 serial port {self.port}: "
                        f"{written} byte(s)."
                    )
                    if command == "X":
                        self.stop_fault_count += 1
                        self.last_stop_fault_reason = short_msg
                        logger.critical(
                            "CRITICAL STOP FAULT: Short write transmitting 'X' — %s",
                            short_msg,
                        )
                    raise serial.SerialException(short_msg)
                await asyncio.to_thread(connection.flush)
            except serial.SerialException as exc:
                if command == "X":
                    self.stop_fault_count += 1
                    self.last_stop_fault_reason = str(exc)
                    logger.critical(
                        "CRITICAL STOP FAULT: Serial exception transmitting 'X' on %s: %s",
                        self.port,
                        exc,
                    )
                await self._close_locked()
                raise

    async def send_verified_stop(self) -> None:
        """Send 'X' (stop) and raise SerialStopCommandFaultError if transmission fails while armed."""
        if not self.armed:
            return
        try:
            await self.send("X")
        except Exception as exc:
            fault_msg = (
                f"CRITICAL FAULT: Failed to transmit stop command 'X' to SCOUT01 "
                f"on serial port {self.port}: {exc}"
            )
            self.stop_fault_count += 1
            self.last_stop_fault_reason = fault_msg
            logger.critical(fault_msg)
            raise SerialStopCommandFaultError(fault_msg) from exc

    async def execute_step(
        self,
        command: str,
        duration_s: float,
        pause_s: float = 0.5,
    ) -> bool:
        """Send a movement command for duration_s followed by 'X' (stop) and a short settle pause.

        Immediately aborts and sends 'X' if Emergency Stop is latched before or during the step.
        """
        if command != "X" and self.emergency_stop_latched:
            await self._safe_stop_on_abort()
            return False

        if not await self.ensure_connected():
            await wait_unless_emergency_stopped(min(duration_s, 0.25))
            return False

        try:
            await self.send(command)
            if duration_s > 0:
                completed_wait = await wait_unless_emergency_stopped(duration_s)
                if not completed_wait:
                    await self.send_verified_stop()
                    return False
            await self.send_verified_stop()
            if pause_s > 0:
                await wait_unless_emergency_stopped(pause_s)
            return True
        except EmergencyStopLatchedError:
            await self._safe_stop_on_abort()
            return False
        except SerialStopCommandFaultError:
            raise
        except serial.SerialException as exc:
            logger.error("Serial write error during execute_step(%s): %s", command, exc)
            await self.send_verified_stop()
            return False

    async def _safe_stop_on_abort(self) -> None:
        if self.armed and self.connected:
            await self.send_verified_stop()

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
                logger.exception(
                    "Error closing SCOUT01 serial port %s.", self.port
                )
