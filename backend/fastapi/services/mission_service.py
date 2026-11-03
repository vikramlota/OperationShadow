"""Mission lifecycle, physical SCOUT01 serial execution, dead-reckoning map tracking, replay, and deterministic after-action report service (§3, §4, §7 + Phase 3).

Uses `UltronAgentPort` (`UltronLangGraphAgent` / `StubUltronAgent`) for mission creation
and task decomposition, persists `Mission`, `Task`, `Event`, `ReasoningTrace`, and
immutable `MissionSnapshot` records to SQLite, broadcasts live updates over
`DashboardGateway`, reconstructs before/after replay timelines, and generates
evidence-backed After-Action Mission Reports without invoking an LLM.
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
from db.models import Event, Mission, Observation, ReasoningTrace, Task, Vehicle
from schemas.entities import (
    EventRead,
    MissionCreateRequest,
    MissionCreateResponse,
    MissionDetailRead,
    MissionReplayResponse,
    MissionReplayTimelineEntry,
    MissionReportBatteryRecord,
    MissionReportDecisionHistory,
    MissionReportDecisionRecord,
    MissionReportEnvironmentalFindings,
    MissionReportEscalationRecord,
    MissionReportEventRecord,
    MissionReportHazardFinding,
    MissionReportObjectFinding,
    MissionReportObservationFinding,
    MissionReportOverview,
    MissionReportResponse,
    MissionReportSafetyEvent,
    MissionReportSafetySummary,
    MissionReportStandingOrderRecord,
    MissionReportTaskRecord,
    MissionReportTaskSummary,
    MissionReportTriageRecord,
    MissionReportUnresolvedItems,
    MissionReportVehicleSummary,
    ReasoningTraceRead,
    TaskEvidenceStageLiteral,
    TaskRead,
    ensure_utc_datetime,
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

    async def generate_mission_report(
        self,
        session: AsyncSession,
        mission_id: str,
    ) -> MissionReportResponse:
        """Deterministically generate a structured After-Action Mission Report from persisted records (Phase 3).

        Never invokes an LLM, never mutates mission state, never infers physical
        execution from a plan or simulated completion, and never projects
        camera-relative coordinates onto the global map.
        """
        stmt = (
            select(Mission)
            .where(Mission.id == mission_id)
            .options(
                selectinload(Mission.tasks),
                selectinload(Mission.observations),
                selectinload(Mission.events),
                selectinload(Mission.reasoning_traces),
            )
        )
        mission_row = (await session.execute(stmt)).scalar_one_or_none()
        if mission_row is None:
            raise ResourceNotFoundError("Mission", mission_id)

        # Flush any buffered in-memory routed events, traces, tasks, or snapshots for this mission
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
                    f"Failed to sync buffered event intelligence before report for {mission_id}: {exc}"
                )
        await self._persist_unsynced_agent_snapshots(session, mission_id, commit=True)

        # Reload persisted collections in deterministic order
        task_rows = (
            await session.execute(
                select(Task)
                .where(Task.mission_id == mission_id)
                .order_by(Task.priority.asc(), Task.id.asc())
            )
        ).scalars().all()
        obs_rows = (
            await session.execute(
                select(Observation)
                .where(Observation.mission_id == mission_id)
                .order_by(Observation.timestamp.asc(), Observation.id.asc())
                .limit(250)
            )
        ).scalars().all()
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

        v_row = await session.get(Vehicle, "SCOUT01")
        vehicle_mode = (v_row.mode if v_row is not None else settings.default_vehicle_mode) or "simulated"
        hw_lockout = bool(settings.hardware_lockout)

        sim_completion_used = any(
            "SIMULATED_MISSION_COMPLETION" in rt.trigger
            or "SIMULATED_ULTRON_STUB" in rt.rationale
            for rt in trace_rows
        )
        is_simulated_exec = bool(
            hw_lockout or vehicle_mode == "simulated" or sim_completion_used
        )

        created_at = ensure_utc_datetime(mission_row.created_at) or datetime.now(timezone.utc)
        completed_at = ensure_utc_datetime(mission_row.completed_at)
        duration_sec: float | None = None
        if completed_at is not None and completed_at >= created_at:
            duration_sec = round((completed_at - created_at).total_seconds(), 3)

        # Check if any snapshot recorded DEGRADED mode
        degraded_observed = any(
            str(s.get("mission_status", "")).upper() == "DEGRADED"
            or str(s.get("trigger_type", "")).upper() == "REPLAN_DEGRADED"
            for s in persisted_snapshots
        ) or any(rt.id.startswith("DT-DEG-") for rt in trace_rows)

        final_status_str = (
            "DEGRADED"
            if degraded_observed and mission_row.status in ("ACTIVE", "IN_PROGRESS")
            else mission_row.status
        )

        exec_provenance = (
            "SIMULATED_ONLY — Simulated vehicle mode or hardware lockout active; no physical rover actuation occurred."
            if is_simulated_exec
            else "SUPERVISED_REAL_MODE — Physical serial dispatch enabled; closed-loop physical completion remains unconfirmed."
        )

        overview = MissionReportOverview(
            mission_id=mission_id,
            objective=mission_row.objective,
            created_at=created_at,
            completed_at=completed_at,
            duration_seconds=duration_sec,
            final_status=final_status_str,
            vehicle_mode=vehicle_mode,
            is_simulated_execution=is_simulated_exec,
            hardware_lockout_active=hw_lockout,
            execution_provenance=exec_provenance,
        )

        # 2. Task Summary
        traces_by_task_id: dict[str, list[str]] = {}
        for rt in trace_rows:
            for tid in rt.resulting_task_ids:
                traces_by_task_id.setdefault(tid, []).append(rt.id)

        serial_step_events_by_task: set[str] = set()
        for ev in event_rows:
            if ev.category == "TELEMETRY" and "-> '" in ev.message:
                for t in task_rows:
                    if f" {t.id}:" in ev.message:
                        serial_step_events_by_task.add(t.id)

        task_records: list[MissionReportTaskRecord] = []
        for t in task_rows:
            t_status = t.status.upper()
            stage: TaskEvidenceStageLiteral
            if t_status == "COMPLETED":
                if is_simulated_exec:
                    stage = "SIMULATED_COMPLETED"
                    p_note = (
                        "Task marked COMPLETED in simulation (complete_mission helper or mock adapter); "
                        "no physical rover execution occurred."
                    )
                elif t.id in serial_step_events_by_task:
                    stage = "COMMAND_DISPATCHED"
                    p_note = (
                        "Serial step commands dispatched; physical waypoint arrival not verified by closed-loop localization."
                    )
                else:
                    stage = "ASSIGNED"
                    p_note = "Task marked COMPLETED without recorded physical command dispatch evidence."
            elif t_status == "IN_PROGRESS":
                stage = "IN_PROGRESS"
                p_note = "Task was in progress but did not reach completion."
            elif t_status == "CANCELLED":
                stage = "CANCELLED"
                p_note = "Task was cancelled prior to completion (e.g., safety stop, E-Stop, or replanning)."
            elif t_status == "FAILED":
                stage = "FAILED"
                p_note = "Task failed during execution."
            elif t_status == "ASSIGNED":
                stage = "ASSIGNED"
                p_note = "Task was planned and assigned to SCOUT01; never physically or simulatedly executed."
            else:
                stage = "PLANNED"
                p_note = "Task was planned only and remained pending."

            task_records.append(
                MissionReportTaskRecord(
                    task_id=t.id,
                    vehicle_id=t.vehicle_id or "SCOUT01",
                    task_type=t.type,
                    area=t.area,
                    target=t.target,
                    priority=t.priority,
                    status=t_status,
                    highest_evidence_stage=stage,
                    is_simulated=is_simulated_exec,
                    source_trace_ids=traces_by_task_id.get(t.id, []),
                    provenance_note=p_note,
                )
            )

        completed_cnt = sum(1 for tr in task_records if tr.status == "COMPLETED")
        failed_cnt = sum(1 for tr in task_records if tr.status == "FAILED")
        cancelled_cnt = sum(1 for tr in task_records if tr.status == "CANCELLED")
        assigned_cnt = sum(1 for tr in task_records if tr.status == "ASSIGNED")
        in_prog_cnt = sum(1 for tr in task_records if tr.status == "IN_PROGRESS")
        pending_only_cnt = sum(1 for tr in task_records if tr.status == "PENDING")
        pending_total_cnt = pending_only_cnt + assigned_cnt + in_prog_cnt

        task_summary = MissionReportTaskSummary(
            total_tasks=len(task_records),
            completed_tasks=completed_cnt,
            pending_tasks=pending_total_cnt,
            assigned_tasks=assigned_cnt,
            in_progress_tasks=in_prog_cnt,
            failed_tasks=failed_cnt,
            cancelled_tasks=cancelled_cnt,
            tasks=task_records,
        )

        # 3. Environmental Findings (from persisted snapshots + SQLite observations)
        objective_lower = mission_row.objective.lower()
        task_targets_lower = {
            (t.target or "").lower() for t in task_rows if t.target
        }

        tracked_by_id: dict[str, dict[str, Any]] = {}
        hazards_by_id: dict[str, dict[str, Any]] = {}
        obs_by_id: dict[str, MissionReportObservationFinding] = {}
        evidence_ref_set: set[str] = set()
        unconfirmed_frames: set[str] = set()
        transform_map_validated = False
        extrinsics_validated = False

        for snap in persisted_snapshots:
            prov = snap.get("evidence_provenance") or {}
            if prov.get("transform_map_validated"):
                transform_map_validated = True
            if prov.get("extrinsics_camera_lidar_validated"):
                extrinsics_validated = True
            for uf in prov.get("unconfirmed_coordinate_frames", []):
                unconfirmed_frames.add(str(uf))

            for tobj in snap.get("tracked_objects", []):
                if isinstance(tobj, dict) and tobj.get("object_id"):
                    tracked_by_id[str(tobj["object_id"])] = tobj
            for haz in snap.get("hazards", []):
                if isinstance(haz, dict) and haz.get("id"):
                    hazards_by_id[str(haz["id"])] = haz
            for oref in snap.get("observation_refs", []):
                if isinstance(oref, dict) and oref.get("observation_id"):
                    oid = str(oref["observation_id"])
                    evidence_ref_set.add(oid)
                    frame_str = str(oref.get("coordinate_frame", "oak_rgb_camera_optical_frame"))
                    if frame_str != "map" or not transform_map_validated:
                        unconfirmed_frames.add(frame_str)
                    ts_val = float(oref.get("sensor_timestamp", snap.get("timestamp", 0.0)))
                    obs_by_id[oid] = MissionReportObservationFinding(
                        observation_id=oid,
                        vehicle_id=str(oref.get("vehicle_id") or "SCOUT01"),
                        sensor_type=str(oref.get("sensor_type", "depth_camera")),
                        obs_type=str(oref.get("classification") or "detection"),
                        confidence=float(oref.get("confidence") or 0.85),
                        coordinate_frame=frame_str,
                        position_x=float(oref.get("position_x", 0.0)),
                        position_y=float(oref.get("position_y", 0.0)),
                        position_z=float(oref.get("position_z", 0.0)),
                        range_m=(
                            float(oref["range_m"])
                            if oref.get("range_m") is not None
                            else None
                        ),
                        timestamp=datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat(),
                        source_mode=str(oref.get("source_mode", "simulated")),
                        epistemic_status=str(oref.get("epistemic_status", "OBSERVED")),
                        is_stale=bool(oref.get("is_stale", False)),
                    )

        # Merge SQLite `Observation` rows
        for o_row in obs_rows:
            evidence_ref_set.add(o_row.id)
            raw_dict = o_row.raw if isinstance(o_row.raw, dict) else {}
            frame_str = str(raw_dict.get("frame", "map"))
            is_conf_map = bool(raw_dict.get("coordinates_are_map_frame", frame_str == "map" and transform_map_validated))
            if not is_conf_map:
                unconfirmed_frames.add(frame_str)
            pos_dict = raw_dict.get("position") if isinstance(raw_dict.get("position"), dict) else {}
            px = float(pos_dict.get("x", o_row.location_x))
            py = float(pos_dict.get("y", o_row.location_y))
            pz = float(pos_dict.get("z", 0.0))
            rng = float(raw_dict["range_m"]) if raw_dict.get("range_m") is not None else None
            src_mode = "physical" if raw_dict.get("source") == "rosbridge" else "simulated"
            obs_ts = ensure_utc_datetime(o_row.timestamp) or created_at

            if o_row.id not in obs_by_id:
                obs_by_id[o_row.id] = MissionReportObservationFinding(
                    observation_id=o_row.id,
                    vehicle_id=o_row.vehicle_id,
                    sensor_type=str(raw_dict.get("sensor", "depth_camera")),
                    obs_type=o_row.obs_type,
                    confidence=float(o_row.confidence),
                    coordinate_frame=frame_str,
                    position_x=px,
                    position_y=py,
                    position_z=pz,
                    range_m=rng,
                    timestamp=obs_ts.isoformat(),
                    source_mode=src_mode,
                    epistemic_status="OBSERVED",
                    is_stale=False,
                )

            # Ensure SQLite observations are also reflected in objects/hazards if not already tracked
            label_clean = o_row.obs_type.replace("detection:", "").strip()
            is_haz = any(
                kw in label_clean.lower()
                for kw in ("hazard", "fire", "smoke", "thermal", "gas")
            )
            is_obs = any(
                kw in label_clean.lower()
                for kw in ("obstacle", "chair", "bench", "box", "wall", "barrier", "table")
            )
            if is_haz and not any(
                h.get("observation_id") == o_row.id for h in hazards_by_id.values()
            ):
                hid = f"HAZ-{o_row.id}"
                hazards_by_id[hid] = {
                    "id": hid,
                    "type": label_clean,
                    "coordinate_frame": frame_str,
                    "location": {"x": px, "y": py},
                    "confidence": float(o_row.confidence),
                    "epistemic_status": "OBSERVED",
                    "source_mode": src_mode,
                    "source_vehicle": o_row.vehicle_id,
                    "observation_id": o_row.id,
                    "handled": False,
                }
            elif not any(
                o_row.id in (t.get("contributing_observation_ids") or [])
                for t in tracked_by_id.values()
            ):
                oid_synth = f"OBJ-{o_row.id}"
                tracked_by_id[oid_synth] = {
                    "object_id": oid_synth,
                    "label": label_clean,
                    "coordinate_frame": frame_str,
                    "position": {"x": px, "y": py, "z": pz, "range_m": rng},
                    "classification_confidence": float(o_row.confidence),
                    "epistemic_status": "OBSERVED",
                    "tracking_status": "ACTIVE",
                    "source_mode": src_mode,
                    "is_obstacle": is_obs,
                    "is_hazard": is_haz,
                    "contributing_observation_ids": [o_row.id],
                }

        detected_objects: list[MissionReportObjectFinding] = []
        identified_targets: list[MissionReportObjectFinding] = []
        obstacles_list: list[MissionReportObjectFinding] = []

        for oid, tobj in sorted(tracked_by_id.items()):
            evidence_ref_set.add(oid)
            lbl = str(tobj.get("label", "unknown"))
            frame_str = str(tobj.get("coordinate_frame", "oak_rgb_camera_optical_frame"))
            pos = tobj.get("position") or {}
            px = float(pos.get("x", 0.0))
            py = float(pos.get("y", 0.0))
            pz = float(pos.get("z", 0.0))
            rng = float(pos["range_m"]) if pos.get("range_m") is not None else None
            is_conf_map = frame_str == "map" and transform_map_validated
            map_x = px if is_conf_map else None
            map_y = py if is_conf_map else None
            frame_note = (
                "Confirmed global map frame."
                if is_conf_map
                else f"Camera/sensor-relative frame '{frame_str}'; global floor-map coordinates are NOT confirmed and were not projected."
            )
            contrib_ids = [str(x) for x in (tobj.get("contributing_observation_ids") or [])]
            for cid in contrib_ids:
                evidence_ref_set.add(cid)

            is_obs = bool(tobj.get("is_obstacle", False))
            is_haz = bool(tobj.get("is_hazard", False))
            matches_target = bool(
                lbl.lower() in objective_lower
                or lbl.lower() in task_targets_lower
                or any(lbl.lower() in tgt for tgt in task_targets_lower)
                or (not is_obs and not is_haz and lbl.lower() not in ("unknown", "obstacle"))
            )

            finding = MissionReportObjectFinding(
                object_id=oid,
                label=lbl,
                coordinate_frame=frame_str,
                position_x=px,
                position_y=py,
                position_z=pz,
                range_m=rng,
                map_position_x=map_x,
                map_position_y=map_y,
                confidence=float(tobj.get("classification_confidence", 0.85)),
                epistemic_status=str(tobj.get("epistemic_status", "OBSERVED")),
                tracking_status=str(tobj.get("tracking_status", "ACTIVE")),
                source_mode=str(tobj.get("source_mode", "simulated")),
                is_obstacle=is_obs,
                is_hazard=is_haz,
                matches_objective_target=matches_target,
                evidence_ids=contrib_ids,
                coordinate_frame_note=frame_note,
            )
            detected_objects.append(finding)
            if is_obs:
                obstacles_list.append(finding)
            if matches_target and not is_obs:
                identified_targets.append(finding)

        reported_hazards: list[MissionReportHazardFinding] = []
        for hid, hdict in sorted(hazards_by_id.items()):
            evidence_ref_set.add(hid)
            frame_str = str(hdict.get("coordinate_frame", "map"))
            loc = hdict.get("location") or {}
            hx = float(loc.get("x", 0.0))
            hy = float(loc.get("y", 0.0))
            obs_id = hdict.get("observation_id")
            ev_ids = [str(obs_id)] if obs_id else []
            for eid in ev_ids:
                evidence_ref_set.add(eid)
            is_conf_map = frame_str == "map" and transform_map_validated
            frame_note = (
                "Confirmed global map frame."
                if is_conf_map
                else f"Coordinate recorded in '{frame_str}' (global map transform unconfirmed)."
            )
            reported_hazards.append(
                MissionReportHazardFinding(
                    hazard_id=hid,
                    hazard_type=str(hdict.get("type", "possible_hazard")),
                    coordinate_frame=frame_str,
                    position_x=hx,
                    position_y=hy,
                    confidence=float(hdict.get("confidence", 0.9)),
                    epistemic_status=str(hdict.get("epistemic_status", "OBSERVED")),
                    source_mode=str(hdict.get("source_mode", "simulated")),
                    source_vehicle=str(hdict.get("source_vehicle", "SCOUT01")),
                    evidence_ids=ev_ids,
                    handled=bool(hdict.get("handled", False)),
                    coordinate_frame_note=frame_note,
                )
            )

        frame_limitations: list[str] = []
        if not transform_map_validated:
            frame_limitations.append(
                "Global map transform (map -> odom -> oak_parent_frame -> oak_rgb_camera_optical_frame) is unconfirmed; camera detections remain strictly camera-relative."
            )
        if not extrinsics_validated:
            frame_limitations.append(
                "Camera-to-LiDAR extrinsics are unconfirmed; cross-sensor spatial corroboration is gated."
            )
        for uf in sorted(unconfirmed_frames):
            frame_limitations.append(
                f"Coordinate frame '{uf}' is unconfirmed for global floor-map projection."
            )

        sensor_obs_list = sorted(
            obs_by_id.values(), key=lambda item: (item.timestamp, item.observation_id)
        )
        coverage_note = (
            "No sensor observations were recorded during this mission. Absence of observations does NOT prove that objects or hazards were absent."
            if not sensor_obs_list and not detected_objects and not reported_hazards
            else "Observations reflect point-in-time single-sensor detections. 360° LiDAR geometric coverage is unavailable; uninspected areas are NOT verified clear."
        )

        environmental_findings = MissionReportEnvironmentalFindings(
            detected_objects=detected_objects,
            identified_targets=identified_targets,
            reported_hazards=reported_hazards,
            obstacles=obstacles_list,
            sensor_observations=sensor_obs_list,
            evidence_references=sorted(evidence_ref_set),
            coordinate_frame_limitations=frame_limitations,
            coverage_note=coverage_note,
        )

        # 4. ULTRON Decision History
        snap_by_event_id: dict[str, dict[str, Any]] = {}
        snap_by_trace_id: dict[str, dict[str, Any]] = {}
        for snap in persisted_snapshots:
            aeid = snap.get("associated_event_id")
            if aeid and (aeid not in snap_by_event_id or snap.get("phase") == "BEFORE"):
                snap_by_event_id[str(aeid)] = snap
            atid = snap.get("associated_trace_id")
            if atid and (atid not in snap_by_trace_id or snap.get("phase") == "AFTER"):
                snap_by_trace_id[str(atid)] = snap

        important_events: list[MissionReportEventRecord] = []
        for ev in event_rows:
            ev_ts = ensure_utc_datetime(ev.timestamp) or created_at
            ev_snap = snap_by_event_id.get(ev.id)
            dec_ctx = (ev_snap.get("decision_context") or {}) if ev_snap else {}
            ev_type = dec_ctx.get("event_type")
            if not ev_type and ev.message.startswith("[") and "]" in ev.message:
                ev_type = ev.message[1 : ev.message.index("]")]
            sev = (
                str(dec_ctx.get("event_priority"))
                if dec_ctx.get("event_priority")
                else ("CRITICAL" if ev.category == "SAFETY" else "INFO")
            )
            important_events.append(
                MissionReportEventRecord(
                    event_id=ev.id,
                    timestamp=ev_ts,
                    category=ev.category,
                    event_type=ev_type,
                    severity=sev,
                    source=str(dec_ctx.get("event_source") or ev.category.lower()),
                    source_mode=str(dec_ctx.get("event_source_mode") or "simulated"),
                    message=ev.message,
                    evidence_ids=[str(x) for x in dec_ctx.get("evidence_ids", [])],
                )
            )

        standing_orders_applied: list[MissionReportStandingOrderRecord] = []
        llm_triage_outcomes: list[MissionReportTriageRecord] = []
        replanning_operations: list[MissionReportDecisionRecord] = []
        rejected_decisions: list[MissionReportDecisionRecord] = []
        accepted_plans: list[MissionReportDecisionRecord] = []
        human_escalations: list[MissionReportEscalationRecord] = []

        for rt in trace_rows:
            rt_ts = ensure_utc_datetime(rt.timestamp) or created_at
            rt_snap = snap_by_trace_id.get(rt.id)
            dec_ctx = (rt_snap.get("decision_context") or {}) if rt_snap else {}
            src_ev_id = dec_ctx.get("event_id") or (
                rt_snap.get("associated_event_id") if rt_snap else None
            )
            so_id = dec_ctx.get("standing_order_id")
            if not so_id and rt.trigger.startswith("[STANDING_ORDER:") and "]" in rt.trigger:
                so_id = rt.trigger.split(":", 1)[1].split("]", 1)[0].strip()

            triage_disp = dec_ctx.get("triage_disposition")
            val_passed = bool(dec_ctx.get("validation_passed", not rt.id.startswith("DT-DEG-")))
            val_errors = [str(x) for x in dec_ctx.get("validation_errors", [])]
            ev_ids = [str(x) for x in dec_ctx.get("evidence_ids", [])]
            before_sid = rt_snap.get("paired_before_snapshot_id") if rt_snap else None
            after_sid = rt_snap.get("snapshot_id") if rt_snap else None

            if rt.trigger.startswith("EVENT_TRIAGE:"):
                parts = rt.trigger.split(":")
                ev_type_parsed = parts[1] if len(parts) > 1 else dec_ctx.get("event_type")
                ev_id_parsed = parts[2] if len(parts) > 2 else src_ev_id
                disp_str = str(triage_disp or "LOG_ONLY")
                llm_triage_outcomes.append(
                    MissionReportTriageRecord(
                        trace_id=rt.id,
                        event_id=ev_id_parsed,
                        event_type=ev_type_parsed,
                        disposition=disp_str,
                        severity=str(dec_ctx.get("triage_severity") or "MEDIUM"),
                        triage_source=str(dec_ctx.get("triage_source") or "fallback"),
                        rationale=rt.rationale,
                        evidence_ids=ev_ids,
                        timestamp=rt_ts.isoformat(),
                    )
                )
                if disp_str == "ESCALATE_TO_OPERATOR":
                    human_escalations.append(
                        MissionReportEscalationRecord(
                            record_id=rt.id,
                            timestamp=rt_ts.isoformat(),
                            reason=f"Triage escalated event {ev_id_parsed} to operator: {rt.rationale}",
                            source_event_id=ev_id_parsed,
                        )
                    )
                continue

            dec_record = MissionReportDecisionRecord(
                trace_id=rt.id,
                timestamp=rt_ts,
                trigger=rt.trigger,
                rationale=rt.rationale,
                source_event_id=str(src_ev_id) if src_ev_id else None,
                standing_order_id=str(so_id) if so_id else None,
                triage_disposition=str(triage_disp) if triage_disp else None,
                planner_source=dec_ctx.get("planner_source"),
                validation_passed=val_passed,
                validation_errors=val_errors,
                resulting_task_ids=list(rt.resulting_task_ids),
                evidence_ids=ev_ids,
                before_snapshot_id=str(before_sid) if before_sid else None,
                after_snapshot_id=str(after_sid) if after_sid else None,
            )

            if so_id:
                standing_orders_applied.append(
                    MissionReportStandingOrderRecord(
                        standing_order_id=str(so_id),
                        source_event_id=str(src_ev_id) if src_ev_id else None,
                        trace_id=rt.id,
                        timestamp=rt_ts.isoformat(),
                        rationale=rt.rationale,
                        resulting_task_ids=list(rt.resulting_task_ids),
                        evidence_ids=ev_ids,
                    )
                )
                if so_id in ("SO-01-CRITICAL-BATTERY", "SO-04-HAZARD-HOLD"):
                    human_escalations.append(
                        MissionReportEscalationRecord(
                            record_id=rt.id,
                            timestamp=rt_ts.isoformat(),
                            reason=f"Standing Order {so_id} requires operator intervention: {rt.rationale}",
                            source_event_id=str(src_ev_id) if src_ev_id else None,
                            standing_order_id=str(so_id),
                        )
                    )

            if not val_passed or rt.id.startswith("DT-DEG-"):
                rejected_decisions.append(dec_record)
                human_escalations.append(
                    MissionReportEscalationRecord(
                        record_id=rt.id,
                        timestamp=rt_ts.isoformat(),
                        reason=f"Safety validator rejected proposed plan ({rt.trigger}); entered DEGRADED hold.",
                        source_event_id=str(src_ev_id) if src_ev_id else None,
                    )
                )
            else:
                if not rt.trigger.startswith("SIMULATED_MISSION_COMPLETION"):
                    accepted_plans.append(dec_record)
                if rt.trigger not in (
                    "OPERATOR_MISSION_DISPATCH",
                    "INITIAL_MISSION_PLANNING",
                ) and not rt.trigger.startswith("SIMULATED_MISSION_COMPLETION"):
                    replanning_operations.append(dec_record)

        decision_history = MissionReportDecisionHistory(
            important_events=important_events,
            standing_orders_applied=standing_orders_applied,
            llm_triage_outcomes=llm_triage_outcomes,
            replanning_operations=replanning_operations,
            rejected_decisions=rejected_decisions,
            accepted_plans=accepted_plans,
            human_escalation_events=human_escalations,
        )

        # 5. Vehicle Summary
        conn_history: list[str] = []
        battery_samples: list[float] = []
        comm_failures: list[str] = []

        for snap in persisted_snapshots:
            v_snap = (snap.get("vehicles") or {}).get("SCOUT01")
            if isinstance(v_snap, dict):
                c_val = str(v_snap.get("connection", "ONLINE"))
                if not conn_history or conn_history[-1] != c_val:
                    conn_history.append(c_val)
                if c_val in ("OFFLINE", "DISCONNECTED", "UNKNOWN"):
                    comm_failures.append(
                        f"[{snap.get('timestamp_iso')}] SCOUT01 connection state was {c_val} ({snap.get('trigger_reason')})."
                    )
                bat_val = v_snap.get("battery")
                if isinstance(bat_val, (int, float)) and math.isfinite(float(bat_val)):
                    battery_samples.append(float(bat_val))

        if v_row is not None:
            if not conn_history or conn_history[-1] != v_row.connection:
                conn_history.append(v_row.connection)
            if math.isfinite(float(v_row.battery)):
                battery_samples.append(float(v_row.battery))

        for ev in event_rows:
            if "VEHICLE_DISCONNECTED" in ev.message or "CRITICAL STOP FAULT" in ev.message:
                comm_failures.append(
                    f"[{ensure_utc_datetime(ev.timestamp).isoformat() if ev.timestamp else ''}] {ev.message}"
                )

        if not battery_samples:
            battery_samples = [100.0]

        min_bat = round(min(battery_samples), 2)
        max_bat = round(max(battery_samples), 2)
        final_bat = round(battery_samples[-1], 2)

        vehicle_summary = MissionReportVehicleSummary(
            vehicle_id="SCOUT01",
            vehicle_type="ground",
            mode=vehicle_mode,
            final_status=(v_row.status if v_row is not None else "IDLE"),
            final_connection=(v_row.connection if v_row is not None else "DISCONNECTED"),
            connection_history=conn_history or ["DISCONNECTED"],
            battery_observations=MissionReportBatteryRecord(
                min_battery=min_bat,
                max_battery=max_bat,
                final_battery=final_bat,
                sample_count=len(battery_samples),
                critical_low_battery_observed=(min_bat < 20.0),
            ),
            task_participation_ids=[
                t.task_id for t in task_records if t.vehicle_id == "SCOUT01"
            ],
            communication_failures=comm_failures,
            operational_limitations=[
                "HAWK01 aerial vehicle is simulated / roadmap only and is not integrated.",
                (
                    "SHADOW_HARDWARE_LOCKOUT=true: physical actuator commands are locked out."
                    if hw_lockout
                    else "Hardware lockout is disabled; supervised serial safety rules apply."
                ),
                "Onboard thermal and gas telemetry sensors are unimplemented on SCOUT01.",
                "360° LiDAR free-space scan and camera-to-LiDAR extrinsics are unconfirmed.",
            ],
        )

        # 6. Safety Summary
        estop_events: list[MissionReportSafetyEvent] = []
        val_failure_events: list[MissionReportSafetyEvent] = []
        crit_fault_events: list[MissionReportSafetyEvent] = []
        unresolved_safety: list[str] = []

        for snap in persisted_snapshots:
            if snap.get("trigger_type") == "EMERGENCY_STOP_TRIGGERED":
                estop_events.append(
                    MissionReportSafetyEvent(
                        record_id=str(snap.get("snapshot_id")),
                        timestamp=str(snap.get("timestamp_iso")),
                        category="EMERGENCY_STOP",
                        description=str(snap.get("trigger_reason")),
                        stop_dispatched=True,
                        vehicle_acknowledged=is_simulated_exec,
                        physical_stop_confirmed=False,
                        is_simulated=is_simulated_exec,
                    )
                )

        for ev in event_rows:
            ev_iso = (ensure_utc_datetime(ev.timestamp) or created_at).isoformat()
            msg_upper = ev.message.upper()
            if "CRITICAL STOP FAULT" in msg_upper or "[VEHICLE_FAULT]" in msg_upper:
                crit_fault_events.append(
                    MissionReportSafetyEvent(
                        record_id=ev.id,
                        timestamp=ev_iso,
                        category="CRITICAL_FAULT",
                        description=ev.message,
                        stop_dispatched=False,
                        vehicle_acknowledged=False,
                        physical_stop_confirmed=False,
                        is_simulated=is_simulated_exec,
                    )
                )
            elif "EMERGENCY STOP" in msg_upper or "E-STOP" in msg_upper or "EMERGENCY_STOPPED" in msg_upper:
                estop_events.append(
                    MissionReportSafetyEvent(
                        record_id=ev.id,
                        timestamp=ev_iso,
                        category="EMERGENCY_STOP",
                        description=ev.message,
                        stop_dispatched="FAILED" not in msg_upper,
                        vehicle_acknowledged=("CONFIRMED" in msg_upper and "NONE_CONFIRMED" not in msg_upper),
                        physical_stop_confirmed=False,
                        is_simulated=is_simulated_exec,
                    )
                )
            elif ev.category == "SAFETY" and "SAFETY CHECK" in msg_upper:
                val_failure_events.append(
                    MissionReportSafetyEvent(
                        record_id=ev.id,
                        timestamp=ev_iso,
                        category="VALIDATION_FAILURE",
                        description=ev.message,
                        is_simulated=is_simulated_exec,
                    )
                )

        for rej in rejected_decisions:
            val_failure_events.append(
                MissionReportSafetyEvent(
                    record_id=rej.trace_id,
                    timestamp=rej.timestamp.isoformat(),
                    category="VALIDATION_FAILURE",
                    description=f"Safety validator rejected plan ({rej.trigger}): {'; '.join(rej.validation_errors) or rej.rationale}",
                    is_simulated=is_simulated_exec,
                )
            )

        if v_row is not None and v_row.status == "EMERGENCY_STOPPED":
            if not estop_events:
                estop_events.append(
                    MissionReportSafetyEvent(
                        record_id=f"{mission_id}-ESTOP-STATE",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        category="EMERGENCY_STOP",
                        description="SCOUT01 is in latched EMERGENCY_STOPPED state.",
                        stop_dispatched=True,
                        vehicle_acknowledged=is_simulated_exec,
                        physical_stop_confirmed=False,
                        is_simulated=is_simulated_exec,
                    )
                )
            unresolved_safety.append("SCOUT01 remains in latched EMERGENCY_STOPPED state.")

        if v_row is not None and v_row.status == "FAULT":
            unresolved_safety.append("SCOUT01 is in FAULT status.")
        if crit_fault_events:
            unresolved_safety.append(
                f"{len(crit_fault_events)} critical stop/vehicle fault(s) recorded."
            )
        if min_bat <= 20.0:
            unresolved_safety.append(
                f"Critical low battery observed on SCOUT01 ({min_bat:.1f}% <= 20.0%)."
            )
        for rh in reported_hazards:
            if not rh.handled:
                unresolved_safety.append(
                    f"Reported hazard {rh.hazard_id} ({rh.hazard_type}) remains unhandled."
                )

        stop_status_str = (
            "No Emergency Stop events triggered during this mission."
            if not estop_events
            else (
                "Emergency Stop triggered in SIMULATED mode; no physical hardware stop occurred."
                if is_simulated_exec
                else "Emergency Stop triggered; verify physical chassis kill switch and serial stop status."
            )
        )

        safety_summary = MissionReportSafetySummary(
            emergency_stop_events=estop_events,
            safety_validation_failures=val_failure_events,
            critical_faults=crit_fault_events,
            stop_dispatch_acknowledged_status=stop_status_str,
            unresolved_safety_issues=unresolved_safety,
        )

        # 7. Unresolved Items
        incomplete_tasks_list = [
            f"{tr.task_id} ({tr.task_type}, status={tr.status}, stage={tr.highest_evidence_stage})"
            for tr in task_records
            if tr.status != "COMPLETED"
        ]
        missing_obs_list: list[str] = []
        if not sensor_obs_list and not detected_objects and not reported_hazards:
            missing_obs_list.append(
                "Zero sensor observations were persisted for this mission; environmental state was not empirically observed."
            )

        stale_items_list = [
            f"Observation {so.observation_id} ({so.obs_type}) was marked STALE."
            for so in sensor_obs_list
            if so.is_stale or so.epistemic_status == "STALE"
        ]
        for obj in detected_objects:
            if obj.epistemic_status == "STALE" or obj.tracking_status == "STALE":
                stale_items_list.append(
                    f"Tracked object {obj.object_id} ({obj.label}) transitioned to STALE."
                )
        for ev in event_rows:
            if "OBSERVATION_STALE" in ev.message:
                stale_items_list.append(f"Event {ev.id}: {ev.message}")

        conflicting_list = [
            f"Object {obj.object_id} ({obj.label}) has CONFLICTED sensor measurements."
            for obj in detected_objects
            if obj.epistemic_status == "CONFLICTED"
        ]
        for ev in event_rows:
            if "SENSOR_CONFLICT" in ev.message and not any(ev.id in c for c in conflicting_list):
                conflicting_list.append(f"Event {ev.id}: {ev.message}")

        unverified_signals = [
            "LiDAR sensor stream is UNAVAILABLE (topic, message type, frame, and extrinsics unconfirmed).",
            "Global map transform (map -> odom -> oak_parent_frame) is unconfirmed.",
            "Thermal and gas telemetry sensors on SCOUT01 are unimplemented.",
        ]

        unknown_outcomes: list[str] = []
        if final_status_str != "COMPLETED":
            unknown_outcomes.append(
                f"Mission is in '{final_status_str}' state and has not completed."
            )
        if is_simulated_exec:
            unknown_outcomes.append(
                "All mission planning and task transitions occurred in SIMULATED mode; physical rover execution was not performed."
            )
        elif any(tr.highest_evidence_stage != "PHYSICAL_EXECUTION_CONFIRMED" for tr in task_records):
            unknown_outcomes.append(
                "Physical task completion is not confirmed by closed-loop localization."
            )
        if not persisted_snapshots:
            unknown_outcomes.append(
                "No historical World Model snapshots were persisted for this mission (pre-Phase-3 or legacy mission record)."
            )

        unresolved_items = MissionReportUnresolvedItems(
            incomplete_tasks=incomplete_tasks_list,
            missing_observations=missing_obs_list,
            stale_telemetry_or_observations=stale_items_list,
            conflicting_evidence=conflicting_list,
            unverified_sensor_signals=unverified_signals,
            unknown_outcomes=unknown_outcomes,
        )

        mode_tag = "SIMULATED EXECUTION" if is_simulated_exec else "REAL MODE"
        summary_text = (
            f"[{mode_tag}] Mission {mission_id} ('{mission_row.objective}') status: {final_status_str}. "
            f"Tasks: {completed_cnt}/{len(task_records)} completed "
            f"({pending_total_cnt} pending/assigned/in-progress, {cancelled_cnt} cancelled, {failed_cnt} failed). "
            f"Findings: {len(detected_objects)} object(s), {len(reported_hazards)} hazard(s), "
            f"{len(obstacles_list)} obstacle(s). "
            f"Decisions: {len(standing_orders_applied)} Standing Order(s), "
            f"{len(llm_triage_outcomes)} triage evaluation(s), "
            f"{len(replanning_operations)} replan(s)."
        )

        generated_at = datetime.now(timezone.utc)
        md_lines = [
            f"# AFTER-ACTION MISSION REPORT — {mission_id}",
            "",
            f"- **Generated At (UTC)**: {generated_at.isoformat()}",
            f"- **Execution Mode**: `{mode_tag}` (`vehicle_mode={vehicle_mode}`, `hardware_lockout={hw_lockout}`)",
            f"- **Provenance**: {exec_provenance}",
            f"- **Deterministic Report**: `True` (`llm_used_for_report=False`)",
            "",
            "## 1. Mission Overview",
            f"- **Mission ID**: `{mission_id}`",
            f"- **Objective**: {mission_row.objective}",
            f"- **Final Status**: `{final_status_str}`",
            f"- **Created At**: `{created_at.isoformat()}`",
            f"- **Completed At**: `{completed_at.isoformat() if completed_at else 'INCOMPLETE'}`",
            f"- **Duration (s)**: `{duration_sec if duration_sec is not None else 'N/A'}`",
            "",
            "## 2. Task Summary",
            f"- **Total Tasks**: {len(task_records)} | **Completed**: {completed_cnt} | **Pending/Active**: {pending_total_cnt} | **Cancelled**: {cancelled_cnt} | **Failed**: {failed_cnt}",
        ]
        for tr in task_records:
            md_lines.append(
                f"  - `{tr.task_id}` [{tr.vehicle_id}] `{tr.task_type}` (P{tr.priority}) — Status: `{tr.status}`, Stage: `{tr.highest_evidence_stage}` ({tr.provenance_note})"
            )

        md_lines.extend(
            [
                "",
                "## 3. Environmental Findings",
                f"- **Coverage Note**: {coverage_note}",
                f"- **Detected Objects ({len(detected_objects)})**: "
                + (
                    ", ".join(
                        f"`{o.object_id}` ({o.label} in `{o.coordinate_frame}`, status=`{o.epistemic_status}`)"
                        for o in detected_objects
                    )
                    or "None recorded"
                ),
                f"- **Reported Hazards ({len(reported_hazards)})**: "
                + (
                    ", ".join(
                        f"`{h.hazard_id}` ({h.hazard_type} in `{h.coordinate_frame}`, conf={h.confidence:.2f})"
                        for h in reported_hazards
                    )
                    or "None recorded"
                ),
                f"- **Coordinate-Frame Limitations**: {'; '.join(frame_limitations) or 'None'}",
                "",
                "## 4. ULTRON Decision History",
                f"- **Standing Orders Applied ({len(standing_orders_applied)})**: "
                + (
                    ", ".join(
                        f"`{so.standing_order_id}` (event `{so.source_event_id}` -> tasks {so.resulting_task_ids})"
                        for so in standing_orders_applied
                    )
                    or "None"
                ),
                f"- **LLM Triage Evaluations ({len(llm_triage_outcomes)})**: "
                + (
                    ", ".join(
                        f"`{tr_item.trace_id}` ({tr_item.event_id} -> `{tr_item.disposition}`)"
                        for tr_item in llm_triage_outcomes
                    )
                    or "None"
                ),
                f"- **Replanning Operations ({len(replanning_operations)})**: "
                + (
                    ", ".join(
                        f"`{rp.trace_id}` ({rp.trigger})" for rp in replanning_operations
                    )
                    or "None"
                ),
                f"- **Rejected Decisions ({len(rejected_decisions)})**: "
                + (
                    ", ".join(f"`{rj.trace_id}`" for rj in rejected_decisions)
                    or "None"
                ),
                "",
                "## 5. Vehicle & Safety Summary",
                f"- **Vehicle**: `SCOUT01` (mode=`{vehicle_mode}`, status=`{vehicle_summary.final_status}`, connection=`{vehicle_summary.final_connection}`)",
                f"- **Battery**: min=`{min_bat:.1f}%`, max=`{max_bat:.1f}%`, final=`{final_bat:.1f}%`",
                f"- **Emergency Stop Status**: {stop_status_str}",
                f"- **Unresolved Safety Issues**: {'; '.join(unresolved_safety) or 'None'}",
                "",
                "## 6. Unresolved Items & Gaps",
                f"- **Incomplete Tasks**: {'; '.join(incomplete_tasks_list) or 'None'}",
                f"- **Missing Observations**: {'; '.join(missing_obs_list) or 'None'}",
                f"- **Conflicting Evidence**: {'; '.join(conflicting_list) or 'None'}",
                f"- **Unknown Outcomes**: {'; '.join(unknown_outcomes) or 'None'}",
            ]
        )

        return MissionReportResponse(
            mission_id=mission_id,
            generated_at=generated_at,
            report_deterministic=True,
            llm_used_for_report=False,
            historical_snapshots_count=len(persisted_snapshots),
            historical_state_available=len(persisted_snapshots) > 0,
            summary_text=summary_text,
            overview=overview,
            task_summary=task_summary,
            environmental_findings=environmental_findings,
            decision_history=decision_history,
            vehicle_summary=vehicle_summary,
            safety_summary=safety_summary,
            unresolved_items=unresolved_items,
            markdown_export="\n".join(md_lines),
        )

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
