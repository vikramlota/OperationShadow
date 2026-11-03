"""Mission lifecycle, physical SCOUT01 serial execution, dead-reckoning map tracking, and replay service (§3, §4, §7).

Uses `UltronAgentPort` (`UltronLangGraphAgent` / `StubUltronAgent`) for mission creation
and task decomposition, persists `Mission`, `Task`, `Event`, and `ReasoningTrace`
records to SQLite, broadcasts live updates over `DashboardGateway`, and executes
assigned tasks over USB serial (`ScoutSerialWriter` on `/dev/ttyACM1`) while updating
SCOUT01's position on the SVG Tactical Floor Map in real time.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import math
from typing import TYPE_CHECKING

from core.config import settings
from core.errors import ResourceNotFoundError
from core.logging import get_logger
from db.models import Event, Mission, ReasoningTrace, Task, Vehicle
from schemas.entities import (
    EventRead,
    MissionCreateRequest,
    MissionCreateResponse,
    MissionDetailRead,
    MissionReplayResponse,
    MissionReplayTimelineEntry,
    ReasoningTraceRead,
    TaskRead,
)
from schemas.enums import EventCategoryEnum, MissionStatusEnum
from services.event_service import EventService, event_service
from services.scout_serial_writer import ScoutSerialWriter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from ultron_stub import StubUltronAgent, UltronAgentPort
from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway

if TYPE_CHECKING:
    pass

logger = get_logger("mission_service")


def _task_serial_steps(
    task_type: str,
    target: str | None,
    total_tasks: int,
) -> list[tuple[str, float, str]]:
    """Map an ULTRON task to physical (command, duration_s, description) steps on SCOUT01."""
    upper_type = (task_type or "").upper()
    lower_target = (target or "").lower()

    # Extended realistic scanning and multi-phase reconnaissance sequence (~22-25 seconds)
    if "DEEP" in upper_type or "corridor" in lower_target or "lobby" in lower_target:
        return [
            ("W", 3.0, "NAVIGATE forward down corridor (2.0m)"),
            ("A", 1.8, "SCAN 90° sweep toward perimeter seating"),
            ("X", 1.5, "DWELL hold for optical depth point cloud"),
            ("D", 2.6, "SCAN pan toward central partition obstacle"),
            ("X", 1.5, "DWELL hold for barrier boundary logging"),
            ("A", 1.2, "ALIGN heading with corridor center"),
            ("W", 3.0, "PROBE advance past partition"),
            ("A", 2.8, "PANORAMA 360° visual inspection"),
            ("W", 3.5, "RETURN traverse back to base station"),
            ("A", 1.8, "ORIENT re-align to base heading"),
            ("X", 0.0, "DIAGNOSTICS hold & all clear"),
        ]

    # Standard full 5-stage reconnaissance loop (~9-10 seconds)
    if (
        total_tasks == 1
        or "RECON" in upper_type
        or "return" in lower_target
        or "sweep" in lower_target
        or "scan" in lower_target
    ):
        return [
            ("W", 2.5, "NAVIGATE forward down aisle (2.0m)"),
            ("A", 2.2, "SCAN 180° visual sweep of perimeter"),
            ("X", 1.0, "DWELL hold for object detection"),
            ("W", 2.5, "RETURN traverse back to start position"),
            ("A", 1.8, "ORIENT re-align to original heading"),
            ("X", 0.0, "DIAGNOSTICS / STOP"),
        ]

    if "RETURN" in upper_type or "return" in lower_target or "base" in lower_target:
        return [
            ("A", 1.8, "ROTATE 180 degrees toward base"),
            ("W", 2.5, "RETURN to base position"),
            ("A", 1.6, "ORIENT to original heading"),
        ]
    if upper_type in ("SCAN", "INSPECT") or "scan" in lower_target or "360" in lower_target:
        return [
            ("A", 2.4, f"{upper_type} rotational sweep"),
            ("X", 1.5, "DWELL capture optical frame"),
            ("D", 1.2, "COUNTER-SCAN re-center heading"),
        ]
    if upper_type == "AVOID" or "avoid" in lower_target or "reroute" in lower_target:
        return [
            ("A", 1.0, "AVOID obstacle turn left"),
            ("W", 1.6, "AVOID bypass forward"),
            ("D", 1.0, "AVOID re-align heading"),
        ]
    if upper_type in ("STOP", "REPORT") or "stop" in lower_target or "confirm" in lower_target:
        return [
            ("X", 0.0, f"{upper_type} confirm systems & hold"),
        ]
    # Default for movement tasks
    return [
        ("W", 2.5, f"{upper_type or 'NAVIGATE'} forward"),
    ]


class MissionService:
    """Adapter bridging the FastAPI service layer to ULTRON LangGraph, SQLite, and DashboardGateway."""

    def __init__(
        self,
        agent: UltronAgentPort | None = None,
        *,
        gateway: DashboardGateway | None = None,
        events: EventService | None = None,
    ) -> None:
        if agent is not None:
            self._agent = agent
        else:
            try:
                from services.ultron_agent_adapter import UltronLangGraphAgent
                self._agent = UltronLangGraphAgent()
            except Exception:
                self._agent = StubUltronAgent()
        self._gateway: DashboardGateway = gateway or dashboard_gateway
        self._events: EventService = events or event_service
        self._execution_lock = asyncio.Lock()

    @property
    def agent(self) -> UltronAgentPort:
        return self._agent

    @property
    def gateway(self) -> DashboardGateway:
        return self._gateway

    def set_gateway(self, gateway: DashboardGateway) -> None:
        self._gateway = gateway
        self._events.set_gateway(gateway)

    async def _next_mission_id(self, session: AsyncSession) -> str:
        """Generate a sequential human-readable mission ID (M001, M002, ...) per §7."""
        count_result = await session.execute(select(func.count()).select_from(Mission))
        count = int(count_result.scalar_one() or 0)
        candidate_num = count + 1
        for _ in range(10_000):
            candidate = f"M{candidate_num:03d}"
            exists = (
                await session.execute(select(Mission.id).where(Mission.id == candidate))
            ).scalar_one_or_none()
            if exists is None:
                return candidate
            candidate_num += 1
        return f"M{candidate_num:03d}"

    async def create_mission(
        self,
        session: AsyncSession,
        request: MissionCreateRequest,
    ) -> MissionCreateResponse:
        """Create a mission via ULTRON LangGraph workflow, persist records, and broadcast updates (§4, §7)."""
        response, _ = await self.create_mission_with_rationale(session, request)
        return response

    async def create_mission_with_rationale(
        self,
        session: AsyncSession,
        request: MissionCreateRequest,
    ) -> tuple[MissionCreateResponse, str | None]:
        """Create a mission, persist records, and return (response, rationale) for downstream tasks (e.g. TTS)."""
        mission_id = await self._next_mission_id(session)
        now = datetime.now(timezone.utc)

        plan = await self._agent.create_simulated_mission(
            mission_id=mission_id,
            objective=request.objective,
            language=request.language,
        )

        mission_row = Mission(
            id=mission_id,
            objective=request.objective,
            status=plan.status,
            created_at=now,
            completed_at=None,
        )
        session.add(mission_row)

        task_rows: list[Task] = []
        for spec in plan.tasks:
            t_row = Task(
                id=spec.task_id,
                mission_id=mission_id,
                vehicle_id=spec.vehicle_id,
                type=spec.task_type,
                area=spec.area,
                target=spec.target,
                priority=spec.priority,
                status=spec.status,
            )
            session.add(t_row)
            task_rows.append(t_row)

        # Persist mission, tasks, initial event, and reasoning trace in a single atomic commit
        event_read = await self._events.record_event(
            session,
            mission_id=mission_id,
            category=EventCategoryEnum.MISSION,
            message=f"Mission {mission_id} created: {request.objective}",
            event_id=f"{mission_id}-EVT01",
            timestamp=now,
            commit=False,
            broadcast=False,
        )
        trace_read = await self._events.record_reasoning_trace(
            session,
            mission_id=mission_id,
            trigger=plan.trigger,
            rationale=plan.rationale,
            resulting_task_ids=[t.id for t in task_rows],
            trace_id=f"{mission_id}-RT01",
            timestamp=now + timedelta(milliseconds=1),
            commit=False,
            broadcast=False,
        )

        await session.commit()

        sorted_tasks = sorted(task_rows, key=lambda t: (t.priority, t.id))
        detail = MissionDetailRead(
            id=mission_row.id,
            objective=mission_row.objective,
            status=MissionStatusEnum(mission_row.status),
            created_at=mission_row.created_at,
            completed_at=mission_row.completed_at,
            tasks=[TaskRead.model_validate(t) for t in sorted_tasks],
        )
        await self._gateway.broadcast_event(event_read)
        await self._gateway.broadcast_reasoning_trace(trace_read)
        await self._gateway.broadcast_mission_status(detail)

        return MissionCreateResponse(mission_id=mission_id), plan.rationale

    async def _is_emergency_stopped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> bool:
        """Check whether SCOUT01 is currently in a latched or DB/WorldModel EMERGENCY_STOPPED state."""
        from services.scout_serial_writer import (
            is_global_emergency_stop_latched,
            latch_global_emergency_stop,
        )

        if is_global_emergency_stop_latched():
            return True
        if hasattr(self._agent, "world"):
            scout = self._agent.world.vehicles.get("SCOUT01")
            if scout is not None and getattr(scout, "status", "") == "EMERGENCY_STOPPED":
                latch_global_emergency_stop()
                return True
        async with session_factory() as session:
            v_row = await session.get(Vehicle, "SCOUT01")
            if v_row is not None and v_row.status == "EMERGENCY_STOPPED":
                latch_global_emergency_stop()
                return True
        return False

    async def _validate_scout_execution_safety(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> tuple[bool, str]:
        """Independently check SCOUT01 safety state before dispatching movement commands."""
        if await self._is_emergency_stopped(session_factory):
            return False, "SCOUT01 is EMERGENCY_STOPPED (latched)."

        if hasattr(self._agent, "world"):
            scout = self._agent.world.vehicles.get("SCOUT01")
            if scout is None:
                return False, "SCOUT01 is missing from the ULTRON World Model."
            conn_str = getattr(
                scout.connection, "value", str(scout.connection)
            ).strip().upper()
            if conn_str in {"OFFLINE", "UNKNOWN"}:
                return False, f"SCOUT01 World Model connection is {conn_str}."
            bat = getattr(scout, "battery", None)
            if (
                bat is None
                or isinstance(bat, bool)
                or not isinstance(bat, (int, float))
                or not math.isfinite(float(bat))
                or float(bat) <= 20.0
            ):
                return False, f"SCOUT01 battery is unsafe or unavailable ({bat!r})."

        async with session_factory() as session:
            v_row = await session.get(Vehicle, "SCOUT01")
            if v_row is not None:
                if v_row.status in {"EMERGENCY_STOPPED", "FAULT", "UNKNOWN"}:
                    return False, f"SCOUT01 database status is {v_row.status}."
                if v_row.connection == "UNKNOWN":
                    return False, "SCOUT01 database connection is UNKNOWN."
                if (
                    v_row.battery is None
                    or not math.isfinite(float(v_row.battery))
                    or float(v_row.battery) <= 20.0
                ):
                    return False, f"SCOUT01 database battery is {v_row.battery}% (<= 20%)."

        return True, "ok"

    async def _send_stop_and_report_fault_if_failed(
        self,
        writer: ScoutSerialWriter,
        session_factory: async_sessionmaker[AsyncSession],
        mission_id: str,
        *,
        context: str,
    ) -> bool:
        """Send 'X' (stop) to SCOUT01 and record a CRITICAL SAFETY fault if transmission fails."""
        if not writer.armed:
            return True
        try:
            await writer.send_verified_stop()
            return True
        except Exception as exc:
            fault_msg = (
                f"CRITICAL STOP FAULT during {context} on mission {mission_id}: "
                f"failed to transmit 'X' to SCOUT01 on {writer.port} ({exc})"
            )
            logger.critical(fault_msg)
            async with session_factory() as session:
                v_row = await session.get(Vehicle, "SCOUT01")
                if v_row is not None and v_row.status != "EMERGENCY_STOPPED":
                    v_row.status = "FAULT"
                await self._events.record_event(
                    session,
                    mission_id=mission_id,
                    category=EventCategoryEnum.SAFETY,
                    message=fault_msg,
                )
            return False

    async def _broadcast_scout_pose(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        x: float,
        y: float,
        status: str = "EXECUTING",
        is_real: bool = True,
    ) -> None:
        """Broadcast live SCOUT01 position to WebSocket clients and sync to SQLite/WorldModel."""
        if await self._is_emergency_stopped(session_factory):
            return

        now = datetime.now(timezone.utc)
        clean_x = round(max(0.5, min(19.5, x)), 2)
        clean_y = round(max(0.5, min(14.5, y)), 2)

        async with session_factory() as session:
            v_row = await session.get(Vehicle, "SCOUT01")
            if v_row is not None:
                if v_row.status == "EMERGENCY_STOPPED":
                    return
                v_row.position_x = clean_x
                v_row.position_y = clean_y
                v_row.status = status
                v_row.connection = "CONNECTED"
                v_row.last_heartbeat = now
                await session.commit()

        await self._gateway.broadcast_vehicle_dict(
            {
                "id": "SCOUT01",
                "status": status,
                "connection": "CONNECTED",
                "mode": "real" if is_real else "simulated",
                "battery": 94.0,
                "position_x": clean_x,
                "position_y": clean_y,
                "position_alt": 0.0,
                "last_heartbeat": now,
            }
        )

        if hasattr(self._agent, "update_vehicle_state"):
            await self._agent.update_vehicle_state(
                "SCOUT01",
                position_x=clean_x,
                position_y=clean_y,
                status=status,
                connection="CONNECTED",
                last_heartbeat=now,
            )

    async def execute_mission_on_rover(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        mission_id: str,
        serial_writer: ScoutSerialWriter | None = None,
    ) -> MissionDetailRead:
        """Execute a mission's planned tasks on SCOUT01 via ESP32 serial and dead-reckon position on the map."""
        from services.scout_serial_writer import wait_unless_emergency_stopped

        async with self._execution_lock:
            writer = serial_writer or ScoutSerialWriter(
                port=settings.scout_serial_port,
                baudrate=settings.scout_serial_baudrate,
                enabled=settings.scout_serial_enabled,
                hardware_lockout=settings.hardware_lockout,
                real_mode=(settings.default_vehicle_mode == "real"),
                failsafe_verified=settings.scout_serial_failsafe_verified,
            )
            owns_writer = serial_writer is None
            if (
                not writer.armed
                and not settings.hardware_lockout
                and settings.scout_serial_enabled
                and settings.default_vehicle_mode == "real"
                and settings.scout_serial_failsafe_verified
            ):
                writer.armed = True

            hardware_connected = await writer.ensure_connected()

            async with session_factory() as session:
                stmt = (
                    select(Mission)
                    .where(Mission.id == mission_id)
                    .options(selectinload(Mission.tasks))
                )
                mission_row = (await session.execute(stmt)).scalar_one_or_none()
                if mission_row is None:
                    raise ResourceNotFoundError("Mission", mission_id)

                sorted_tasks = sorted(mission_row.tasks, key=lambda t: (t.priority, t.id))
                total_tasks = len(sorted_tasks)
                mode_label = (
                    f"ESP32 serial ({writer.port})"
                    if hardware_connected
                    else f"simulated fallback ({writer.port} not connected)"
                )

                await self._events.record_event(
                    session,
                    mission_id=mission_id,
                    category=EventCategoryEnum.MISSION,
                    message=f"Starting SCOUT01 execution for {mission_id} via {mode_label} ({total_tasks} tasks).",
                )

            safe_ok, safe_reason = await self._validate_scout_execution_safety(
                session_factory
            )
            if not safe_ok:
                logger.warning(
                    f"Aborting execute_mission_on_rover for {mission_id}: {safe_reason}"
                )
                async with session_factory() as session:
                    await self._events.record_event(
                        session,
                        mission_id=mission_id,
                        category=EventCategoryEnum.SAFETY,
                        message=f"Execution blocked by SCOUT01 safety check: {safe_reason}",
                    )
                    return await self.get_mission(session, mission_id)

            # Dead-reckoning map tracking: start SCOUT01 at Base Station WP_BASE (1.5, 1.5)
            curr_x = 1.5
            curr_y = 1.5
            curr_heading = 0.0

            await self._broadcast_scout_pose(
                session_factory,
                curr_x,
                curr_y,
                status="ACTIVE",
                is_real=hardware_connected,
            )

            try:
                for idx, task_obj in enumerate(sorted_tasks, start=1):
                    safe_ok, safe_reason = await self._validate_scout_execution_safety(
                        session_factory
                    )
                    if not safe_ok:
                        if hardware_connected or writer.armed:
                            await self._send_stop_and_report_fault_if_failed(
                                writer,
                                session_factory,
                                mission_id,
                                context=f"pre-task abort ({safe_reason})",
                            )
                        async with session_factory() as session:
                            return await self.get_mission(session, mission_id)

                    task_id = task_obj.id
                    task_type = task_obj.type
                    task_target = task_obj.target

                    # Mark task IN_PROGRESS and broadcast to dashboard
                    async with session_factory() as session:
                        t_row = await session.get(Task, task_id)
                        if t_row is not None:
                            t_row.status = "IN_PROGRESS"
                            await session.commit()
                        detail = await self.get_mission(session, mission_id)
                        await self._gateway.broadcast_mission_status(detail)

                    steps = _task_serial_steps(task_type, task_target, total_tasks)
                    for cmd, duration_s, step_desc in steps:
                        safe_ok, safe_reason = await self._validate_scout_execution_safety(
                            session_factory
                        )
                        if not safe_ok:
                            if hardware_connected or writer.armed:
                                await self._send_stop_and_report_fault_if_failed(
                                    writer,
                                    session_factory,
                                    mission_id,
                                    context=f"pre-step abort on {task_id} ({safe_reason})",
                                )
                            async with session_factory() as session:
                                t_row = await session.get(Task, task_id)
                                if t_row is not None and t_row.status == "IN_PROGRESS":
                                    t_row.status = "CANCELLED"
                                    await session.commit()
                                detail = await self.get_mission(session, mission_id)
                                await self._gateway.broadcast_mission_status(detail)
                                return detail

                        async with session_factory() as session:
                            await self._events.record_event(
                                session,
                                mission_id=mission_id,
                                category=EventCategoryEnum.TELEMETRY,
                                message=f"[{idx}/{total_tasks}] {task_id}: {step_desc} -> '{cmd}' ({duration_s:.1f}s)",
                            )

                        # Advance rover position & transmit serial
                        if cmd == "W":
                            num_ticks = max(1, int(round(duration_s / 0.4)))
                            step_dist = 1.5 / num_ticks
                            tick_dt = duration_s / num_ticks

                            # Final safety/E-Stop check immediately before serial write
                            if await self._is_emergency_stopped(session_factory):
                                await self._send_stop_and_report_fault_if_failed(
                                    writer,
                                    session_factory,
                                    mission_id,
                                    context=f"immediate pre-write E-Stop on {task_id}",
                                )
                                async with session_factory() as session:
                                    t_row = await session.get(Task, task_id)
                                    if t_row is not None and t_row.status == "IN_PROGRESS":
                                        t_row.status = "CANCELLED"
                                        await session.commit()
                                    detail = await self.get_mission(session, mission_id)
                                    await self._gateway.broadcast_mission_status(detail)
                                    return detail

                            if hardware_connected:
                                try:
                                    await writer.send("W")
                                except Exception as send_exc:
                                    logger.error(
                                        f"Serial write error sending 'W' on {writer.port}: {send_exc}"
                                    )
                                    await self._send_stop_and_report_fault_if_failed(
                                        writer,
                                        session_factory,
                                        mission_id,
                                        context=f"serial write failure during 'W' on {task_id}",
                                    )
                                    hardware_connected = False

                            for _ in range(num_ticks):
                                if await self._is_emergency_stopped(session_factory):
                                    if hardware_connected or writer.armed:
                                        await self._send_stop_and_report_fault_if_failed(
                                            writer,
                                            session_factory,
                                            mission_id,
                                            context=f"mid-tick E-Stop on {task_id}",
                                        )
                                    async with session_factory() as session:
                                        t_row = await session.get(Task, task_id)
                                        if t_row is not None and t_row.status == "IN_PROGRESS":
                                            t_row.status = "CANCELLED"
                                            await session.commit()
                                        detail = await self.get_mission(session, mission_id)
                                        await self._gateway.broadcast_mission_status(detail)
                                        return detail

                                curr_x += step_dist * math.cos(curr_heading)
                                curr_y += step_dist * math.sin(curr_heading)
                                await self._broadcast_scout_pose(
                                    session_factory,
                                    curr_x,
                                    curr_y,
                                    status="EXECUTING",
                                    is_real=hardware_connected,
                                )
                                completed_tick = await wait_unless_emergency_stopped(tick_dt)
                                if not completed_tick or await self._is_emergency_stopped(
                                    session_factory
                                ):
                                    if hardware_connected or writer.armed:
                                        await self._send_stop_and_report_fault_if_failed(
                                            writer,
                                            session_factory,
                                            mission_id,
                                            context=f"interrupted tick sleep E-Stop on {task_id}",
                                        )
                                    async with session_factory() as session:
                                        t_row = await session.get(Task, task_id)
                                        if t_row is not None and t_row.status == "IN_PROGRESS":
                                            t_row.status = "CANCELLED"
                                            await session.commit()
                                        detail = await self.get_mission(session, mission_id)
                                        await self._gateway.broadcast_mission_status(detail)
                                        return detail

                            if hardware_connected or writer.armed:
                                await self._send_stop_and_report_fault_if_failed(
                                    writer,
                                    session_factory,
                                    mission_id,
                                    context=f"end of forward step on {task_id}",
                                )
                            if not await wait_unless_emergency_stopped(0.3):
                                if hardware_connected or writer.armed:
                                    await self._send_stop_and_report_fault_if_failed(
                                        writer,
                                        session_factory,
                                        mission_id,
                                        context=f"post-step pause E-Stop on {task_id}",
                                    )
                                async with session_factory() as session:
                                    t_row = await session.get(Task, task_id)
                                    if t_row is not None and t_row.status == "IN_PROGRESS":
                                        t_row.status = "CANCELLED"
                                        await session.commit()
                                    detail = await self.get_mission(session, mission_id)
                                    await self._gateway.broadcast_mission_status(detail)
                                    return detail

                        elif cmd == "A":
                            turn_angle = math.pi if duration_s >= 1.5 else (math.pi / 2.0)
                            curr_heading = (curr_heading + turn_angle) % (2.0 * math.pi)
                            await writer.execute_step("A", duration_s, pause_s=0.3)
                            if await self._is_emergency_stopped(session_factory):
                                async with session_factory() as session:
                                    t_row = await session.get(Task, task_id)
                                    if t_row is not None and t_row.status == "IN_PROGRESS":
                                        t_row.status = "CANCELLED"
                                        await session.commit()
                                    return await self.get_mission(session, mission_id)
                            await self._broadcast_scout_pose(
                                session_factory,
                                curr_x,
                                curr_y,
                                status="EXECUTING",
                                is_real=hardware_connected,
                            )

                        elif cmd == "D":
                            turn_angle = math.pi if duration_s >= 1.5 else (math.pi / 2.0)
                            curr_heading = (curr_heading - turn_angle) % (2.0 * math.pi)
                            await writer.execute_step("D", duration_s, pause_s=0.3)
                            if await self._is_emergency_stopped(session_factory):
                                async with session_factory() as session:
                                    t_row = await session.get(Task, task_id)
                                    if t_row is not None and t_row.status == "IN_PROGRESS":
                                        t_row.status = "CANCELLED"
                                        await session.commit()
                                    return await self.get_mission(session, mission_id)
                            await self._broadcast_scout_pose(
                                session_factory,
                                curr_x,
                                curr_y,
                                status="EXECUTING",
                                is_real=hardware_connected,
                            )

                        else:
                            await writer.execute_step("X", 0.1, pause_s=0.2)
                            if await self._is_emergency_stopped(session_factory):
                                async with session_factory() as session:
                                    t_row = await session.get(Task, task_id)
                                    if t_row is not None and t_row.status == "IN_PROGRESS":
                                        t_row.status = "CANCELLED"
                                        await session.commit()
                                    return await self.get_mission(session, mission_id)
                            await self._broadcast_scout_pose(
                                session_factory,
                                curr_x,
                                curr_y,
                                status="IDLE",
                                is_real=hardware_connected,
                            )

                    # Mark task COMPLETED and broadcast to dashboard
                    async with session_factory() as session:
                        t_row = await session.get(Task, task_id)
                        if t_row is not None:
                            t_row.status = "COMPLETED"
                            await session.commit()
                        detail = await self.get_mission(session, mission_id)
                        await self._gateway.broadcast_mission_status(detail)

                # Ensure final STOP ('X') is sent and final pose is settled
                if hardware_connected or writer.armed:
                    await self._send_stop_and_report_fault_if_failed(
                        writer,
                        session_factory,
                        mission_id,
                        context="mission completion stop",
                    )

                await self._broadcast_scout_pose(
                    session_factory,
                    curr_x,
                    curr_y,
                    status="IDLE",
                    is_real=hardware_connected,
                )

                # Finalize mission as COMPLETED
                async with session_factory() as session:
                    return await self.complete_mission(session, mission_id)
            finally:
                if owns_writer:
                    await writer.close()

    async def get_mission(
        self,
        session: AsyncSession,
        mission_id: str,
    ) -> MissionDetailRead:
        """Retrieve full mission state including ordered tasks (§7: GET /mission/{id})."""
        stmt = (
            select(Mission)
            .where(Mission.id == mission_id)
            .options(selectinload(Mission.tasks))
        )
        mission_row = (await session.execute(stmt)).scalar_one_or_none()
        if mission_row is None:
            raise ResourceNotFoundError("Mission", mission_id)

        sorted_tasks = sorted(mission_row.tasks, key=lambda t: (t.priority, t.id))
        return MissionDetailRead(
            id=mission_row.id,
            objective=mission_row.objective,
            status=MissionStatusEnum(mission_row.status),
            created_at=mission_row.created_at,
            completed_at=mission_row.completed_at,
            tasks=[TaskRead.model_validate(t) for t in sorted_tasks],
        )

    async def get_mission_replay(
        self,
        session: AsyncSession,
        mission_id: str,
    ) -> MissionReplayResponse:
        """Build ordered timeline of events and reasoning traces (§7: GET /mission/{id}/replay)."""
        mission_exists = (
            await session.execute(select(Mission.id).where(Mission.id == mission_id))
        ).scalar_one_or_none()
        if mission_exists is None:
            raise ResourceNotFoundError("Mission", mission_id)

        event_rows = (
            await session.execute(
                select(Event)
                .where(Event.mission_id == mission_id)
                .order_by(Event.timestamp.asc(), Event.id.asc())
            )
        ).scalars().all()

        trace_rows = (
            await session.execute(
                select(ReasoningTrace)
                .where(ReasoningTrace.mission_id == mission_id)
                .order_by(ReasoningTrace.timestamp.asc(), ReasoningTrace.id.asc())
            )
        ).scalars().all()

        entries: list[MissionReplayTimelineEntry] = []
        for e_row in event_rows:
            e_read = EventRead.model_validate(e_row)
            entries.append(
                MissionReplayTimelineEntry(
                    entry_type="event",
                    timestamp=e_read.timestamp,
                    event=e_read,
                    world_model_snapshot=None,
                )
            )

        for rt_row in trace_rows:
            rt_read = ReasoningTraceRead.model_validate(rt_row)
            entries.append(
                MissionReplayTimelineEntry(
                    entry_type="reasoning_trace",
                    timestamp=rt_read.timestamp,
                    reasoning_trace=rt_read,
                    world_model_snapshot={
                        "is_stub": True,
                        "resulting_task_ids": list(rt_read.resulting_task_ids),
                    },
                )
            )

        entries.sort(
            key=lambda item: (
                item.timestamp,
                0 if item.entry_type == "event" else 1,
                item.event.id if item.event is not None else (
                    item.reasoning_trace.id if item.reasoning_trace is not None else ""
                ),
            )
        )
        return MissionReplayResponse(mission_id=mission_id, entries=entries)

    async def complete_mission(
        self,
        session: AsyncSession,
        mission_id: str,
    ) -> MissionDetailRead:
        """Complete a mission, finalize tasks, persist audit records, and broadcast."""
        stmt = (
            select(Mission)
            .where(Mission.id == mission_id)
            .options(selectinload(Mission.tasks))
        )
        mission_row = (await session.execute(stmt)).scalar_one_or_none()
        if mission_row is None:
            raise ResourceNotFoundError("Mission", mission_id)

        now = datetime.now(timezone.utc)
        mission_row.status = MissionStatusEnum.COMPLETED.value
        mission_row.completed_at = now

        completed_task_ids: list[str] = []
        for task in mission_row.tasks:
            if task.status not in ("COMPLETED", "CANCELLED", "FAILED"):
                task.status = "COMPLETED"
            completed_task_ids.append(task.id)

        event_read = await self._events.record_event(
            session,
            mission_id=mission_id,
            category=EventCategoryEnum.MISSION,
            message=f"Mission {mission_id} completed ({len(completed_task_ids)} tasks finalized).",
            timestamp=now,
            commit=False,
            broadcast=False,
        )
        trace_read = await self._events.record_reasoning_trace(
            session,
            mission_id=mission_id,
            trigger=f"SIMULATED_MISSION_COMPLETION:{mission_id}",
            rationale=(
                f"[SIMULATED_ULTRON_STUB] All assigned tasks for mission {mission_id} "
                f"('{mission_row.objective}') marked COMPLETED."
            ),
            resulting_task_ids=completed_task_ids,
            timestamp=now + timedelta(milliseconds=1),
            commit=False,
            broadcast=False,
        )

        await session.commit()

        sorted_tasks = sorted(mission_row.tasks, key=lambda t: (t.priority, t.id))
        detail = MissionDetailRead(
            id=mission_row.id,
            objective=mission_row.objective,
            status=MissionStatusEnum(mission_row.status),
            created_at=mission_row.created_at,
            completed_at=mission_row.completed_at,
            tasks=[TaskRead.model_validate(t) for t in sorted_tasks],
        )
        await self._gateway.broadcast_event(event_read)
        await self._gateway.broadcast_reasoning_trace(trace_read)
        await self._gateway.broadcast_mission_status(detail)
        return detail


mission_service = MissionService()
