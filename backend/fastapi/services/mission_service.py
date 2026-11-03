"""Mission lifecycle, physical SCOUT01 serial execution, dead-reckoning map tracking, and replay service (§3, §4, §7 + Phase 3 Mission Memory & Replay).

Uses `UltronAgentPort` (`UltronLangGraphAgent` / `StubUltronAgent`) for mission creation
and task decomposition, persists `Mission`, `Task`, `Event`, `ReasoningTrace`, and
immutable `MissionSnapshot` records to SQLite, broadcasts live updates over
`DashboardGateway`, and executes assigned tasks over USB serial (`ScoutSerialWriter`
on `/dev/ttyACM1`) while updating SCOUT01's position on the SVG Tactical Floor Map.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any

_project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

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
from ultron.memory.store import (
    SnapshotDecisionContext,
    SnapshotPhaseEnum,
    SnapshotTaskState,
    SnapshotTriggerTypeEnum,
    build_mission_snapshot,
)
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import MissionStatus as UltronMissionStatus, VehicleID
from ultron.schemas.world import (
    ConnectionStatus as UltronConnectionStatus,
    MissionState,
    ScoutState,
    VehicleMode as UltronVehicleMode,
    WorldModel,
)
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

    async def _persist_unsynced_agent_snapshots(
        self,
        session: AsyncSession,
        mission_id: str,
        *,
        commit: bool = False,
    ) -> int:
        """Persist any unsynced `MissionMemorySnapshot` records from `self._agent.memory_store` to SQLite."""
        mem_store = getattr(self._agent, "memory_store", None)
        if mem_store is None or not hasattr(self._events, "record_mission_snapshot"):
            return 0

        unsynced = mem_store.get_unsynced_snapshots(mission_id)
        persisted = 0
        for snap in unsynced:
            try:
                await self._events.record_mission_snapshot(
                    session,
                    snap,
                    commit=False,
                )
                mem_store.mark_snapshot_synced(snap.snapshot_id)
                persisted += 1
            except Exception as exc:
                logger.error(
                    f"Failed to persist snapshot {snap.snapshot_id} for mission {mission_id}: {exc}"
                )
        if commit and persisted > 0:
            await session.commit()
        return persisted

    async def _build_world_for_stub_snapshot(
        self,
        session: AsyncSession,
        mission_id: str,
        objective: str,
        status: str,
        tasks: list[Task],
    ) -> WorldModel:
        """Construct a grounded `WorldModel` representation from SQLite state when `StubUltronAgent` is used."""
        v_row = await session.get(Vehicle, "SCOUT01")
        if v_row is not None:
            conn_enum = (
                UltronConnectionStatus.ONLINE
                if v_row.connection == "CONNECTED"
                else (
                    UltronConnectionStatus.OFFLINE
                    if v_row.connection == "DISCONNECTED"
                    else UltronConnectionStatus.UNKNOWN
                )
            )
            mode_enum = (
                UltronVehicleMode.REAL
                if v_row.mode == "real"
                else UltronVehicleMode.SIMULATED
            )
            scout_state = ScoutState(
                mode=mode_enum,
                position=Position2D(x=v_row.position_x, y=v_row.position_y),
                pose_valid=(v_row.connection == "CONNECTED"),
                battery=v_row.battery,
                status=v_row.status,
                connection=conn_enum,
                last_heartbeat_timestamp=(
                    v_row.last_heartbeat.timestamp() if v_row.last_heartbeat else None
                ),
            )
        else:
            scout_state = ScoutState()

        m_status = (
            UltronMissionStatus.COMPLETED
            if status == "COMPLETED"
            else (
                UltronMissionStatus.FAILED
                if status in ("FAILED", "ABORTED")
                else UltronMissionStatus.IN_PROGRESS
            )
        )
        world = WorldModel(
            mission=MissionState(id=mission_id, objective=objective, status=m_status),
            vehicles={"SCOUT01": scout_state},
        )
        completed_ids = [t.id for t in tasks if t.status == "COMPLETED"]
        active_tasks = [t.id for t in tasks if t.status in ("ASSIGNED", "IN_PROGRESS")]
        world.mission.tasks.completed = completed_ids
        world.mission.tasks.active[VehicleID.SCOUT] = active_tasks[0] if active_tasks else None
        world.mission.tasks.pending = active_tasks[1:] if len(active_tasks) > 1 else []
        return world

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
        """Create a mission, persist records & snapshots, and return (response, rationale)."""
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

        # Persist mission, tasks, initial event, reasoning trace, and snapshots in a single atomic commit
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

        persisted_snaps = await self._persist_unsynced_agent_snapshots(
            session, mission_id, commit=False
        )
        if persisted_snaps == 0 and hasattr(self._events, "record_mission_snapshot"):
            # Fallback snapshot capture when StubUltronAgent (without MemoryStore) is used
            stub_world_before = await self._build_world_for_stub_snapshot(
                session, mission_id, request.objective, "PLANNING", []
            )
            before_snap = build_mission_snapshot(
                stub_world_before,
                mission_id=mission_id,
                sequence=1,
                timestamp=now.timestamp(),
                trigger_type=SnapshotTriggerTypeEnum.MISSION_CREATED,
                trigger_reason=f"Mission {mission_id} created: {request.objective}",
                phase=SnapshotPhaseEnum.BEFORE,
                associated_event_id=event_read.id,
                task_states=[],
            )
            stub_world_after = await self._build_world_for_stub_snapshot(
                session, mission_id, request.objective, plan.status, task_rows
            )
            after_snap = build_mission_snapshot(
                stub_world_after,
                mission_id=mission_id,
                sequence=2,
                timestamp=(now + timedelta(milliseconds=1)).timestamp(),
                trigger_type=SnapshotTriggerTypeEnum.INITIAL_PLAN_ACCEPTED,
                trigger_reason=f"Initial plan accepted for {mission_id}",
                phase=SnapshotPhaseEnum.AFTER,
                associated_event_id=event_read.id,
                associated_trace_id=trace_read.id,
                paired_before_snapshot_id=before_snap.snapshot_id,
                task_states=[
                    SnapshotTaskState(
                        task_id=t.id,
                        vehicle_id=t.vehicle_id or "SCOUT01",
                        task_type=t.type,
                        area=t.area,
                        target=t.target,
                        priority=t.priority,
                        status=t.status,
                        execution_stage="ASSIGNED",
                        is_simulated=True,
                    )
                    for t in task_rows
                ],
                decision_context=SnapshotDecisionContext(
                    event_id=event_read.id,
                    event_type="MISSION_STATE_CHANGED",
                    event_priority="LOW",
                    event_source="mission_lifecycle",
                    event_source_mode="simulated",
                    planner_source="stub" if plan.is_stub else "planner",
                    validation_passed=True,
                    resulting_task_ids=tuple(t.id for t in task_rows),
                    rationale=plan.rationale,
                ),
            )
            try:
                await self._events.record_mission_snapshot(
                    session, before_snap, commit=False
                )
                await self._events.record_mission_snapshot(
                    session, after_snap, commit=False
                )
            except Exception as exc:
                logger.error(f"Failed to persist initial mission snapshots: {exc}")

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
        from services.scout_serial_writer import is_global_emergency_stop_latched

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
                    if is_global_emergency_stop_latched():
                        v_row.status = "EMERGENCY_STOPPED"
                    else:
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

    @staticmethod
    def _summarize_snapshot_state(snap: dict[str, Any]) -> dict[str, Any]:
        """Extract a clean, structured historical state summary from a persisted `MissionMemorySnapshot` dict."""
        prov = snap.get("evidence_provenance") or {}
        return {
            "snapshot_id": snap.get("snapshot_id"),
            "sequence": snap.get("sequence"),
            "timestamp": snap.get("timestamp"),
            "timestamp_iso": snap.get("timestamp_iso"),
            "phase": snap.get("phase"),
            "trigger_type": snap.get("trigger_type"),
            "trigger_reason": snap.get("trigger_reason"),
            "mission_status": snap.get("mission_status"),
            "mission_objective": snap.get("mission_objective"),
            "tasks": list(snap.get("task_states", [])),
            "active_task_ids": dict(snap.get("active_task_ids", {})),
            "completed_task_ids": list(snap.get("completed_task_ids", [])),
            "pending_task_ids": list(snap.get("pending_task_ids", [])),
            "vehicles": dict(snap.get("vehicles", {})),
            "tracked_objects": list(snap.get("tracked_objects", [])),
            "hazards": list(snap.get("hazards", [])),
            "known_obstacles": list(snap.get("known_obstacles", [])),
            "observation_refs": list(snap.get("observation_refs", [])),
            "total_observation_count": int(snap.get("total_observation_count", 0)),
            "sensor_health": dict(snap.get("sensor_health", {})),
            "evidence_provenance": dict(prov),
            "unknowns": list(prov.get("unknowns", [])),
        }

    async def get_mission_replay(
        self,
        session: AsyncSession,
        mission_id: str,
    ) -> MissionReplayResponse:
        """Build deterministic chronological replay timeline with persisted before/after World Model snapshots (§7, Phase 3)."""
        mission_exists = (
            await session.execute(select(Mission.id).where(Mission.id == mission_id))
        ).scalar_one_or_none()
        if mission_exists is None:
            raise ResourceNotFoundError("Mission", mission_id)

        # Flush any buffered in-memory routed events, traces, replanned tasks, or snapshots for this mission
        if hasattr(self._agent, "sync_event_intelligence_to_event_service"):
            try:
                await self._agent.sync_event_intelligence_to_event_service(
                    session,
                    self._events,
                    mission_id=mission_id,
                    broadcast=False,
                )
            except Exception as exc:
                logger.error(
                    f"Failed to sync buffered event intelligence before replay for {mission_id}: {exc}"
                )
        await self._persist_unsynced_agent_snapshots(session, mission_id, commit=True)

        event_rows = (
            await session.execute(
                select(Event)
                .where(Event.mission_id == mission_id)
                .order_by(Event.timestamp.asc(), Event.id.asc())
                .limit(250)
            )
        ).scalars().all()

        trace_rows = (
            await session.execute(
                select(ReasoningTrace)
                .where(ReasoningTrace.mission_id == mission_id)
                .order_by(ReasoningTrace.timestamp.asc(), ReasoningTrace.id.asc())
                .limit(250)
            )
        ).scalars().all()

        persisted_snapshots: list[dict[str, Any]] = []
        if hasattr(self._events, "list_mission_snapshots"):
            persisted_snapshots = await self._events.list_mission_snapshots(
                session, mission_id, limit=250
            )

        snapshots_by_id: dict[str, dict[str, Any]] = {}
        snapshots_by_trace_id: dict[str, dict[str, Any]] = {}
        before_snapshots_by_event_id: dict[str, dict[str, Any]] = {}
        event_snapshots_by_event_id: dict[str, dict[str, Any]] = {}

        for snap in persisted_snapshots:
            sid = str(snap.get("snapshot_id", ""))
            if not sid:
                continue
            snapshots_by_id[sid] = snap
            assoc_tid = snap.get("associated_trace_id")
            if assoc_tid:
                if (
                    assoc_tid not in snapshots_by_trace_id
                    or snap.get("phase") == "AFTER"
                ):
                    snapshots_by_trace_id[str(assoc_tid)] = snap

            assoc_eid = snap.get("associated_event_id")
            if assoc_eid:
                eid_str = str(assoc_eid)
                if snap.get("phase") == "BEFORE":
                    before_snapshots_by_event_id[eid_str] = snap
                if (
                    eid_str not in event_snapshots_by_event_id
                    or snap.get("phase") in ("BEFORE", "STATE_TRANSITION")
                ):
                    event_snapshots_by_event_id[eid_str] = snap

        entries: list[MissionReplayTimelineEntry] = []
        for e_row in event_rows:
            e_read = EventRead.model_validate(e_row)
            ev_snap = event_snapshots_by_event_id.get(e_read.id)
            wm_event_snap: dict[str, Any] | None = None
            if ev_snap is not None:
                dec_ctx = ev_snap.get("decision_context") or {}
                summarized = self._summarize_snapshot_state(ev_snap)
                wm_event_snap = {
                    "historical_state_available": True,
                    "is_stub": False,
                    "snapshot_id": ev_snap.get("snapshot_id"),
                    "sequence": ev_snap.get("sequence"),
                    "phase": ev_snap.get("phase"),
                    "trigger_type": ev_snap.get("trigger_type"),
                    "trigger_reason": ev_snap.get("trigger_reason"),
                    "event_category": e_read.category,
                    "event_type": dec_ctx.get("event_type"),
                    "severity": dec_ctx.get("event_priority")
                    or ("CRITICAL" if e_read.category == "SAFETY" else "INFO"),
                    "trigger_source": dec_ctx.get("event_source")
                    or e_read.category.lower(),
                    "source_mode": dec_ctx.get("event_source_mode") or "simulated",
                    "evidence_ids": list(dec_ctx.get("evidence_ids", [])),
                    "state_at_event": summarized,
                    "before_state": summarized,
                    "after_state": None,
                }
            entries.append(
                MissionReplayTimelineEntry(
                    entry_type="event",
                    timestamp=e_read.timestamp,
                    event=e_read,
                    world_model_snapshot=wm_event_snap,
                )
            )

        for rt_row in trace_rows:
            rt_read = ReasoningTraceRead.model_validate(rt_row)
            after_snap = snapshots_by_trace_id.get(rt_read.id)
            if after_snap is not None:
                paired_before_id = after_snap.get("paired_before_snapshot_id")
                before_snap = (
                    snapshots_by_id.get(str(paired_before_id))
                    if paired_before_id
                    else None
                )
                dec_ctx = after_snap.get("decision_context") or {}
                if before_snap is None:
                    orig_eid = dec_ctx.get("event_id") or after_snap.get(
                        "associated_event_id"
                    )
                    if orig_eid:
                        before_snap = before_snapshots_by_event_id.get(str(orig_eid))

                wm_trace_snap: dict[str, Any] = {
                    "historical_state_available": True,
                    "is_stub": False,
                    "snapshot_id": after_snap.get("snapshot_id"),
                    "sequence": after_snap.get("sequence"),
                    "before_snapshot_id": (
                        before_snap.get("snapshot_id") if before_snap else None
                    ),
                    "after_snapshot_id": after_snap.get("snapshot_id"),
                    "resulting_task_ids": list(rt_read.resulting_task_ids),
                    "before_state": (
                        self._summarize_snapshot_state(before_snap)
                        if before_snap
                        else None
                    ),
                    "trigger": {
                        "event_id": dec_ctx.get("event_id")
                        or after_snap.get("associated_event_id"),
                        "event_type": dec_ctx.get("event_type"),
                        "severity": dec_ctx.get("triage_severity")
                        or dec_ctx.get("event_priority")
                        or "INFO",
                        "source": dec_ctx.get("event_source") or "mission_lifecycle",
                        "source_mode": dec_ctx.get("event_source_mode") or "simulated",
                        "evidence_ids": list(dec_ctx.get("evidence_ids", [])),
                        "standing_order_id": dec_ctx.get("standing_order_id"),
                        "triage_disposition": dec_ctx.get("triage_disposition"),
                        "triage_source": dec_ctx.get("triage_source"),
                    },
                    "decision": {
                        "trace_id": rt_read.id,
                        "trigger": rt_read.trigger,
                        "rationale": rt_read.rationale,
                        "planner_source": dec_ctx.get("planner_source"),
                        "standing_order_id": dec_ctx.get("standing_order_id"),
                        "triage_disposition": dec_ctx.get("triage_disposition"),
                        "validation_passed": bool(
                            dec_ctx.get("validation_passed", True)
                        ),
                        "validation_errors": list(
                            dec_ctx.get("validation_errors", [])
                        ),
                        "plan_accepted": bool(dec_ctx.get("validation_passed", True)),
                        "resulting_task_ids": list(rt_read.resulting_task_ids),
                    },
                    "after_state": self._summarize_snapshot_state(after_snap),
                }
            else:
                wm_trace_snap = {
                    "historical_state_available": False,
                    "unavailable_reason": "No persisted World Model snapshot was recorded for this decision entry.",
                    "is_stub": True,
                    "resulting_task_ids": list(rt_read.resulting_task_ids),
                    "before_state": None,
                    "trigger": None,
                    "decision": {
                        "trace_id": rt_read.id,
                        "trigger": rt_read.trigger,
                        "rationale": rt_read.rationale,
                        "validation_passed": True,
                        "validation_errors": [],
                        "plan_accepted": True,
                        "resulting_task_ids": list(rt_read.resulting_task_ids),
                    },
                    "after_state": None,
                }

            entries.append(
                MissionReplayTimelineEntry(
                    entry_type="reasoning_trace",
                    timestamp=rt_read.timestamp,
                    reasoning_trace=rt_read,
                    world_model_snapshot=wm_trace_snap,
                )
            )

        entries.sort(
            key=lambda item: (
                item.timestamp,
                0 if item.entry_type == "event" else 1,
                int((item.world_model_snapshot or {}).get("sequence", 999_999)),
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
        """Complete a mission, finalize tasks, persist audit records & completion snapshots, and broadcast."""
        stmt = (
            select(Mission)
            .where(Mission.id == mission_id)
            .options(selectinload(Mission.tasks))
        )
        mission_row = (await session.execute(stmt)).scalar_one_or_none()
        if mission_row is None:
            raise ResourceNotFoundError("Mission", mission_id)

        now = datetime.now(timezone.utc)
        pre_tasks = [
            SnapshotTaskState(
                task_id=t.id,
                vehicle_id=t.vehicle_id or "SCOUT01",
                task_type=t.type,
                area=t.area,
                target=t.target,
                priority=t.priority,
                status=t.status,
                execution_stage="ASSIGNED",
                is_simulated=True,
            )
            for t in sorted(mission_row.tasks, key=lambda item: (item.priority, item.id))
        ]

        mission_row.status = MissionStatusEnum.COMPLETED.value
        mission_row.completed_at = now

        completed_task_ids: list[str] = []
        for task in mission_row.tasks:
            if task.status not in ("COMPLETED", "CANCELLED", "FAILED"):
                task.status = "COMPLETED"
            completed_task_ids.append(task.id)

        post_tasks = [
            SnapshotTaskState(
                task_id=t.id,
                vehicle_id=t.vehicle_id or "SCOUT01",
                task_type=t.type,
                area=t.area,
                target=t.target,
                priority=t.priority,
                status=t.status,
                execution_stage="SIMULATED_COMPLETED",
                is_simulated=True,
            )
            for t in sorted(mission_row.tasks, key=lambda item: (item.priority, item.id))
        ]

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

        # Capture BEFORE and AFTER completion snapshots
        mem_store = getattr(self._agent, "memory_store", None)
        agent_world = getattr(self._agent, "world", None)
        if mem_store is not None and agent_world is not None:
            before_comp_snap = mem_store.record_transition_snapshot(
                agent_world,
                mission_id=mission_id,
                timestamp=now.timestamp(),
                trigger_type=SnapshotTriggerTypeEnum.TASK_STATE_TRANSITION,
                trigger_reason=f"Before simulated completion of mission {mission_id}",
                phase=SnapshotPhaseEnum.BEFORE,
                associated_event_id=event_read.id,
                task_states=pre_tasks,
            )
            if agent_world.mission.id == mission_id:
                agent_world.mission.status = UltronMissionStatus.COMPLETED
                agent_world.mission.tasks.completed = list(completed_task_ids)
                agent_world.mission.tasks.active.clear()
                agent_world.mission.tasks.pending.clear()
            if hasattr(self._agent, "update_task_execution_states"):
                self._agent.update_task_execution_states(post_tasks)
            mem_store.record_transition_snapshot(
                agent_world,
                mission_id=mission_id,
                timestamp=(now + timedelta(milliseconds=1)).timestamp(),
                trigger_type=SnapshotTriggerTypeEnum.MISSION_COMPLETED,
                trigger_reason=f"Simulated completion of mission {mission_id} ({len(completed_task_ids)} tasks finalized)",
                phase=SnapshotPhaseEnum.AFTER,
                associated_event_id=event_read.id,
                associated_trace_id=trace_read.id,
                paired_before_snapshot_id=before_comp_snap.snapshot_id,
                task_states=post_tasks,
                decision_context=SnapshotDecisionContext(
                    event_id=event_read.id,
                    event_type="MISSION_STATE_CHANGED",
                    event_priority="LOW",
                    event_source="mission_lifecycle",
                    event_source_mode="simulated",
                    planner_source="simulated_completion_helper",
                    validation_passed=True,
                    resulting_task_ids=tuple(completed_task_ids),
                    rationale=trace_read.rationale,
                ),
            )
            await self._persist_unsynced_agent_snapshots(
                session, mission_id, commit=False
            )
        elif hasattr(self._events, "record_mission_snapshot"):
            stub_world_after = await self._build_world_for_stub_snapshot(
                session,
                mission_id,
                mission_row.objective,
                "COMPLETED",
                list(mission_row.tasks),
            )
            comp_snap = build_mission_snapshot(
                stub_world_after,
                mission_id=mission_id,
                sequence=99,
                timestamp=(now + timedelta(milliseconds=1)).timestamp(),
                trigger_type=SnapshotTriggerTypeEnum.MISSION_COMPLETED,
                trigger_reason=f"Simulated completion of mission {mission_id}",
                phase=SnapshotPhaseEnum.AFTER,
                associated_event_id=event_read.id,
                associated_trace_id=trace_read.id,
                task_states=post_tasks,
                decision_context=SnapshotDecisionContext(
                    event_id=event_read.id,
                    event_type="MISSION_STATE_CHANGED",
                    event_priority="LOW",
                    event_source="mission_lifecycle",
                    event_source_mode="simulated",
                    planner_source="simulated_completion_helper",
                    validation_passed=True,
                    resulting_task_ids=tuple(completed_task_ids),
                    rationale=trace_read.rationale,
                ),
            )
            try:
                await self._events.record_mission_snapshot(
                    session, comp_snap, commit=False
                )
            except Exception as exc:
                logger.error(f"Failed to persist completion snapshot: {exc}")

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
