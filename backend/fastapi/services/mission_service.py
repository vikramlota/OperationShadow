"""Mission lifecycle and replay service wrapping the in-process ULTRON stub (§3, §4, §7).

Uses `UltronAgentPort` (`StubUltronAgent`) for explicitly simulated mission creation
and task decomposition, persists `Mission`, `Task`, `Event`, and `ReasoningTrace`
records to SQLite, and broadcasts live updates over `DashboardGateway`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.errors import ResourceNotFoundError
from db.models import Event, Mission, ReasoningTrace, Task
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from ultron_stub import StubUltronAgent, UltronAgentPort
from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway


class MissionService:
    """Adapter bridging the FastAPI service layer to ULTRON, SQLite, and DashboardGateway."""

    def __init__(
        self,
        agent: UltronAgentPort | None = None,
        *,
        gateway: DashboardGateway | None = None,
        events: EventService | None = None,
    ) -> None:
        self._agent: UltronAgentPort = agent if agent is not None else StubUltronAgent()
        self._gateway: DashboardGateway = gateway or dashboard_gateway
        self._events: EventService = events or event_service

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
        """Create a mission via ULTRON stub, persist records, and broadcast updates (§4, §7)."""
        mission_id = await self._next_mission_id(session)
        now = datetime.now(timezone.utc)

        plan = await self._agent.create_simulated_mission(
            mission_id=mission_id,
            objective=request.objective,
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

        # Persist and broadcast initial mission event and stub reasoning trace
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

        return MissionCreateResponse(mission_id=mission_id)

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
        """Complete a simulated mission, finalize tasks, persist audit records, and broadcast."""
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

