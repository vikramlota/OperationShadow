"""Event, reasoning-trace, and mission-snapshot persistence and broadcast service (§3, §6, §7 + Phase 3)."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any
import uuid

from core.errors import ResourceNotFoundError
from core.logging import get_logger
from db.models import Event, Mission, MissionSnapshot, ReasoningTrace
from schemas.entities import EventRead, ReasoningTraceRead, ensure_utc_datetime
from schemas.enums import EventCategoryEnum
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from websocket.dashboard_gateway import DashboardGateway, dashboard_gateway

logger = get_logger("event_service")

MAX_SNAPSHOT_QUERY_LIMIT = 250


class EventService:
    """Persists mission events, reasoning traces, and immutable mission snapshots and broadcasts them to dashboards."""

    def __init__(self, gateway: DashboardGateway | None = None) -> None:
        self._gateway: DashboardGateway = gateway or dashboard_gateway
        self._snapshot_persistence_failures: int = 0
        self._last_snapshot_persistence_error: str | None = None

    @property
    def gateway(self) -> DashboardGateway:
        return self._gateway

    @property
    def snapshot_persistence_failures(self) -> int:
        return self._snapshot_persistence_failures

    @property
    def last_snapshot_persistence_error(self) -> str | None:
        return self._last_snapshot_persistence_error

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
        commit: bool = True,
        broadcast: bool = True,
    ) -> EventRead:
        """Persist an Event row to SQLite and broadcast over the dashboard WebSocket."""
        resolved_id = event_id or f"EVT-{uuid.uuid4().hex[:8].upper()}"
        resolved_ts = ensure_utc_datetime(timestamp) or datetime.now(timezone.utc)
        resolved_cat = (
            category.value if isinstance(category, EventCategoryEnum) else str(category)
        )

        existing = await session.get(Event, resolved_id)
        if existing is not None:
            return EventRead.model_validate(existing)

        event_row = Event(
            id=resolved_id,
            mission_id=mission_id,
            timestamp=resolved_ts,
            category=resolved_cat,
            message=message,
        )
        session.add(event_row)
        if commit:
            await session.commit()
            await session.refresh(event_row)
        else:
            await session.flush()

        event_read = EventRead(
            id=event_row.id,
            mission_id=event_row.mission_id,
            timestamp=resolved_ts,
            category=event_row.category,
            message=event_row.message,
        )
        if broadcast:
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
        commit: bool = True,
        broadcast: bool = True,
    ) -> ReasoningTraceRead:
        """Persist a ReasoningTrace row to SQLite and broadcast over the dashboard WebSocket."""
        resolved_id = trace_id or f"RT-{uuid.uuid4().hex[:8].upper()}"
        resolved_ts = ensure_utc_datetime(timestamp) or datetime.now(timezone.utc)

        existing = await session.get(ReasoningTrace, resolved_id)
        if existing is not None:
            return ReasoningTraceRead.model_validate(existing)

        trace_row = ReasoningTrace(
            id=resolved_id,
            mission_id=mission_id,
            timestamp=resolved_ts,
            trigger=trigger,
            rationale=rationale,
            resulting_task_ids=list(resulting_task_ids),
        )
        session.add(trace_row)
        if commit:
            await session.commit()
            await session.refresh(trace_row)
        else:
            await session.flush()

        trace_read = ReasoningTraceRead(
            id=trace_row.id,
            mission_id=trace_row.mission_id,
            timestamp=resolved_ts,
            trigger=trace_row.trigger,
            rationale=trace_row.rationale,
            resulting_task_ids=list(trace_row.resulting_task_ids),
        )
        if broadcast:
            await self._gateway.broadcast_reasoning_trace(trace_read)
        return trace_read

    async def record_mission_snapshot(
        self,
        session: AsyncSession,
        snapshot: Any,
        *,
        commit: bool = True,
    ) -> MissionSnapshot | None:
        """Persist an immutable `MissionMemorySnapshot` to SQLite (`mission_snapshots`).

        Never overwrites an existing snapshot row. Logs and records any persistence
        failure visibly without corrupting session state.
        """
        try:
            snap_dict: dict[str, Any] = (
                snapshot.to_persisted_dict()
                if hasattr(snapshot, "to_persisted_dict")
                else copy.deepcopy(dict(snapshot))
            )
            snap_id = str(snap_dict["snapshot_id"])
            existing = await session.get(MissionSnapshot, snap_id)
            if existing is not None:
                return existing

            ts_val = snap_dict.get("timestamp")
            if isinstance(ts_val, (int, float)):
                resolved_ts = datetime.fromtimestamp(float(ts_val), tz=timezone.utc)
            elif isinstance(ts_val, datetime):
                resolved_ts = ensure_utc_datetime(ts_val) or datetime.now(timezone.utc)
            else:
                resolved_ts = datetime.now(timezone.utc)

            row = MissionSnapshot(
                id=snap_id,
                mission_id=str(snap_dict["mission_id"]),
                sequence=max(1, int(snap_dict.get("sequence", 1))),
                timestamp=resolved_ts,
                trigger_type=str(snap_dict.get("trigger_type", "STATE_TRANSITION")),
                trigger_reason=str(snap_dict.get("trigger_reason", "State transition")),
                phase=str(snap_dict.get("phase", "STATE_TRANSITION")),
                associated_event_id=(
                    str(snap_dict["associated_event_id"])
                    if snap_dict.get("associated_event_id")
                    else None
                ),
                associated_trace_id=(
                    str(snap_dict["associated_trace_id"])
                    if snap_dict.get("associated_trace_id")
                    else None
                ),
                paired_before_snapshot_id=(
                    str(snap_dict["paired_before_snapshot_id"])
                    if snap_dict.get("paired_before_snapshot_id")
                    else None
                ),
                snapshot_payload=snap_dict,
            )
            session.add(row)
            if commit:
                await session.commit()
                await session.refresh(row)
            else:
                await session.flush()
            return row
        except Exception as exc:
            self._snapshot_persistence_failures += 1
            err_msg = f"Failed to persist MissionSnapshot to SQLite: {exc}"
            self._last_snapshot_persistence_error = err_msg
            logger.error(err_msg)
            raise

    async def list_mission_snapshots(
        self,
        session: AsyncSession,
        mission_id: str,
        *,
        limit: int = MAX_SNAPSHOT_QUERY_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return bounded chronological persisted snapshot payloads for `mission_id`."""
        bounded_limit = max(1, min(int(limit), MAX_SNAPSHOT_QUERY_LIMIT))
        stmt = (
            select(MissionSnapshot)
            .where(MissionSnapshot.mission_id == mission_id)
            .order_by(
                MissionSnapshot.timestamp.asc(),
                MissionSnapshot.sequence.asc(),
                MissionSnapshot.id.asc(),
            )
            .limit(bounded_limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [copy.deepcopy(row.snapshot_payload) for row in rows]

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
