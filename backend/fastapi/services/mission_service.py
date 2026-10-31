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

    if total_tasks == 1 and (
        "RECON" in upper_type
        or "return" in lower_target
        or "sweep" in lower_target
        or "scan" in lower_target
    ):
        return [
            ("W", 2.0, "NAVIGATE forward (1.5m)"),
            ("A", 1.6, "ROTATE 180-degree scan"),
            ("W", 2.0, "RETURN to start position"),
            ("A", 1.6, "ORIENT to original heading"),
            ("X", 0.0, "DIAGNOSTICS / STOP"),
        ]

    if "RETURN" in upper_type or "return" in lower_target or "base" in lower_target:
        return [
            ("A", 1.6, "ROTATE 180 degrees toward base"),
            ("W", 2.0, "RETURN to base position"),
            ("A", 1.6, "ORIENT to original heading"),
        ]
    if upper_type in ("SCAN", "INSPECT") or "scan" in lower_target or "360" in lower_target:
        return [
            ("A", 1.6, f"{upper_type} rotational sweep"),
        ]
    if upper_type == "AVOID" or "avoid" in lower_target or "reroute" in lower_target:
        return [
            ("A", 0.8, "AVOID obstacle turn left"),
            ("W", 1.2, "AVOID bypass forward"),
            ("D", 0.8, "AVOID re-align heading"),
        ]
    if upper_type in ("STOP", "REPORT") or "stop" in lower_target or "confirm" in lower_target:
        return [
            ("X", 0.0, f"{upper_type} confirm systems & hold"),
        ]
    # Default for NAVIGATE / EXPLORE / movement tasks
    return [
        ("W", 2.0, f"{upper_type or 'NAVIGATE'} forward (1.5m)"),
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
        while True:
            candidate = f"M{candidate_num:03d}"
            exists = (
                await session.execute(select(Mission.id).where(Mission.id == candidate))
            ).scalar_one_or_none()
            if exists is None:
                return candidate
            candidate_num += 1

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

        await session.commit()

        # Persist and broadcast initial mission event and reasoning trace
        await self._events.record_event(
            session,
            mission_id=mission_id,
            category=EventCategoryEnum.MISSION,
            message=f"Mission {mission_id} created: {request.objective}",
            event_id=f"{mission_id}-EVT01",
            timestamp=now,
        )
        await self._events.record_reasoning_trace(
            session,
            mission_id=mission_id,
            trigger=plan.trigger,
            rationale=plan.rationale,
            resulting_task_ids=[t.id for t in task_rows],
            trace_id=f"{mission_id}-RT01",
            timestamp=now + timedelta(milliseconds=1),
        )

        detail = await self.get_mission(session, mission_id)
        await self._gateway.broadcast_mission_status(detail)

        return MissionCreateResponse(mission_id=mission_id), plan.rationale

    async def _broadcast_scout_pose(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        x: float,
        y: float,
        status: str = "EXECUTING",
        is_real: bool = True,
    ) -> None:
        """Broadcast live SCOUT01 position to WebSocket clients and sync to SQLite/WorldModel."""
        now = datetime.now(timezone.utc)
        clean_x = round(max(0.5, min(19.5, x)), 2)
        clean_y = round(max(0.5, min(14.5, y)), 2)

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

        async with session_factory() as session:
            v_row = await session.get(Vehicle, "SCOUT01")
            if v_row is not None:
                v_row.position_x = clean_x
                v_row.position_y = clean_y
                v_row.status = status
                v_row.connection = "CONNECTED"
                v_row.last_heartbeat = now
                await session.commit()

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
        async with self._execution_lock:
            writer = serial_writer or ScoutSerialWriter(
                port=settings.scout_serial_port,
                baudrate=settings.scout_serial_baudrate,
                enabled=True,
                hardware_lockout=False,
                real_mode=True,
                failsafe_verified=True,
            )
            owns_writer = serial_writer is None
            if not writer.armed and not settings.hardware_lockout:
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

            # Dead-reckoning map tracking: start SCOUT01 at Base Station WP_BASE (1.5, 1.5)
            # heading is initially 0.0 rad (facing East, +x along Sector Alpha corridor)
            curr_x = 1.5
            curr_y = 1.5
            curr_heading = 0.0

            # Broadcast initial Base Station placement immediately
            await self._broadcast_scout_pose(
                session_factory,
                curr_x,
                curr_y,
                status="ACTIVE",
                is_real=hardware_connected,
            )

            try:
                for idx, task_obj in enumerate(sorted_tasks, start=1):
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
                        async with session_factory() as session:
                            await self._events.record_event(
                                session,
                                mission_id=mission_id,
                                category=EventCategoryEnum.TELEMETRY,
                                message=f"[{idx}/{total_tasks}] {task_id}: {step_desc} -> '{cmd}' ({duration_s:.1f}s)",
                            )

                        # Advance rover position & transmit serial
                        if cmd == "W":
                            # Move forward 1.5 meters along current heading over duration_s
                            num_ticks = max(1, int(round(duration_s / 0.4)))
                            step_dist = 1.5 / num_ticks
                            tick_dt = duration_s / num_ticks

                            if hardware_connected:
                                try:
                                    await writer.send("W")
                                except Exception:
                                    pass

                            for _ in range(num_ticks):
                                curr_x += step_dist * math.cos(curr_heading)
                                curr_y += step_dist * math.sin(curr_heading)
                                await self._broadcast_scout_pose(
                                    session_factory,
                                    curr_x,
                                    curr_y,
                                    status="EXECUTING",
                                    is_real=hardware_connected,
                                )
                                await asyncio.sleep(tick_dt)

                            if hardware_connected:
                                try:
                                    await writer.send("X")
                                except Exception:
                                    pass
                            await asyncio.sleep(0.3)

                        elif cmd == "A":
                            # Turn counterclockwise by 180° (or 90° for short turns)
                            turn_angle = math.pi if duration_s >= 1.5 else (math.pi / 2.0)
                            curr_heading = (curr_heading + turn_angle) % (2.0 * math.pi)
                            await writer.execute_step("A", duration_s, pause_s=0.3)
                            await self._broadcast_scout_pose(
                                session_factory,
                                curr_x,
                                curr_y,
                                status="EXECUTING",
                                is_real=hardware_connected,
                            )

                        elif cmd == "D":
                            # Turn clockwise
                            turn_angle = math.pi if duration_s >= 1.5 else (math.pi / 2.0)
                            curr_heading = (curr_heading - turn_angle) % (2.0 * math.pi)
                            await writer.execute_step("D", duration_s, pause_s=0.3)
                            await self._broadcast_scout_pose(
                                session_factory,
                                curr_x,
                                curr_y,
                                status="EXECUTING",
                                is_real=hardware_connected,
                            )

                        else:
                            # Stop or diagnostics
                            await writer.execute_step("X", 0.1, pause_s=0.2)
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
                if hardware_connected:
                    try:
                        await writer.send("X")
                    except Exception:
                        pass

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

        await session.commit()

        await self._events.record_event(
            session,
            mission_id=mission_id,
            category=EventCategoryEnum.MISSION,
            message=f"Mission {mission_id} completed ({len(completed_task_ids)} tasks finalized).",
            timestamp=now,
        )
        await self._events.record_reasoning_trace(
            session,
            mission_id=mission_id,
            trigger=f"SIMULATED_MISSION_COMPLETION:{mission_id}",
            rationale=(
                f"[SIMULATED_ULTRON_STUB] All assigned tasks for mission {mission_id} "
                f"('{mission_row.objective}') marked COMPLETED."
            ),
            resulting_task_ids=completed_task_ids,
            timestamp=now + timedelta(milliseconds=1),
        )

        detail = await self.get_mission(session, mission_id)
        await self._gateway.broadcast_mission_status(detail)
        return detail


mission_service = MissionService()
