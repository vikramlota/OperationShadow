"""Event and reasoning-trace persistence and broadcast service (§3, §6, §7)."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from core.errors import ResourceNotFoundError
from db.models import Event, Mission, ReasoningTrace
from schemas.entities import EventRead, ReasoningTraceRead, ensure_utc_datetime
from schemas.enums import EventCategoryEnum
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway


class EventService:
    """Persists mission events and reasoning traces and broadcasts them to dashboards."""

    def __init__(self, gateway: DashboardGateway | None = None) -> None:
        self._gateway: DashboardGateway = gateway or dashboard_gateway

    @property
    def gateway(self) -> DashboardGateway:
        return self._gateway

    def set_gateway(self, gateway: DashboardGateway) -> None:
        self._gateway = gateway

    async def record_event(
        self,
        session: AsyncSession,
        *,
        mission_id: str,
        category: EventCategoryEnum | str,
        message: str,
        event_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> EventRead:
        """Persist an Event row to SQLite and broadcast over the dashboard WebSocket."""
        resolved_id = event_id or f"EVT-{uuid.uuid4().hex[:8].upper()}"
        resolved_ts = ensure_utc_datetime(timestamp) or datetime.now(timezone.utc)
        resolved_cat = (
            category.value if isinstance(category, EventCategoryEnum) else str(category)
        )

        event_row = Event(
            id=resolved_id,
            mission_id=mission_id,
            timestamp=resolved_ts,
            category=resolved_cat,
            message=message,
        )
        session.add(event_row)
        await session.commit()
        await session.refresh(event_row)

        event_read = EventRead.model_validate(event_row)
        await self._gateway.broadcast_event(event_read)
        return event_read

    async def record_reasoning_trace(
        self,
        session: AsyncSession,
        *,
        mission_id: str,
        trigger: str,
        rationale: str,
        resulting_task_ids: list[str],
        trace_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> ReasoningTraceRead:
        """Persist a ReasoningTrace row to SQLite and broadcast over the dashboard WebSocket."""
        resolved_id = trace_id or f"RT-{uuid.uuid4().hex[:8].upper()}"
        resolved_ts = ensure_utc_datetime(timestamp) or datetime.now(timezone.utc)

        trace_row = ReasoningTrace(
            id=resolved_id,
            mission_id=mission_id,
            timestamp=resolved_ts,
            trigger=trigger,
            rationale=rationale,
            resulting_task_ids=list(resulting_task_ids),
        )
        session.add(trace_row)
        await session.commit()
        await session.refresh(trace_row)

        trace_read = ReasoningTraceRead.model_validate(trace_row)
        await self._gateway.broadcast_reasoning_trace(trace_read)
        return trace_read

    async def list_events(
        self,
        session: AsyncSession,
        *,
        mission_id: str | None = None,
    ) -> list[EventRead]:
        """Return chronological events, optionally filtered by `mission_id` (§7: GET /events)."""
        if mission_id is not None:
            mission_exists = (
                await session.execute(select(Mission.id).where(Mission.id == mission_id))
            ).scalar_one_or_none()
            if mission_exists is None:
                raise ResourceNotFoundError("Mission", mission_id)

        stmt = select(Event)
        if mission_id is not None:
            stmt = stmt.where(Event.mission_id == mission_id)
        stmt = stmt.order_by(Event.timestamp.asc(), Event.id.asc())

        rows = (await session.execute(stmt)).scalars().all()
        return [EventRead.model_validate(row) for row in rows]


event_service = EventService()
