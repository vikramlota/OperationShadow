"""Dashboard WebSocket gateway (`dashboard_gateway.py` — Architecture §3, §5.2).

Responsibilities:
1. Manage multiple concurrent React dashboard WebSocket connections (`/ws/dashboard`).
2. Send a full current-state snapshot (`DashboardSnapshotEnvelope`) immediately
   upon client connection before streaming live incremental updates (§5.2).
3. Prevent updates from being lost during the snapshot-to-live transition by
   buffering concurrent broadcasts per client while the database snapshot query
   is in flight, deduplicating any events/traces already captured in the snapshot,
   and flushing buffered updates in FIFO order immediately after the snapshot frame.
4. Validate all outbound messages against `DashboardWebSocketEnvelope` and
   `DashboardSnapshotEnvelope`.
5. Enforce a bounded-resource slow-client policy: each client has a bounded
   outbound queue (`max_client_queue_size`, default 64) and a per-frame send
   timeout (`send_timeout_seconds`, default 1.0s). Slow or disconnected clients
   that overflow their queue or time out on send are immediately evicted without
   blocking delivery to healthy clients.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import uuid

from core.logging import get_logger
from db.models import Event, Mission, ReasoningTrace, Vehicle
from fastapi import WebSocket, WebSocketDisconnect
from schemas.entities import (
    EventRead,
    MissionDetailRead,
    ReasoningTraceRead,
    TaskRead,
    VehicleRead,
)
from schemas.enums import (
    MissionStatusEnum,
    VehicleConnectionStateEnum,
    VehicleIdEnum,
    VehicleModeEnum,
    VehicleStatusEnum,
)
from schemas.websocket import (
    DashboardEventEnvelope,
    DashboardMissionStatusEnvelope,
    DashboardReasoningTraceEnvelope,
    DashboardSnapshotEnvelope,
    DashboardSnapshotPayload,
    DashboardTelemetryEnvelope,
    MissionStatusPushPayload,
    TelemetryPushPayload,
    validate_dashboard_ws_envelope,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

logger = get_logger("websocket.dashboard")

SnapshotHook = Callable[[], Awaitable[None]]


@dataclass
class _BufferedEnvelope:
    """Envelope queued while a client's initial snapshot is being constructed."""

    envelope_type: str
    entity_id: str | None
    wire_dict: dict[str, Any]


@dataclass
class _DashboardClientConnection:
    """State for a single connected dashboard WebSocket viewer."""

    client_id: str
    websocket: WebSocket
    queue: asyncio.Queue[dict[str, Any]]
    syncing_snapshot: bool = True
    pre_snapshot_buffer: list[_BufferedEnvelope] = field(default_factory=list)
    sender_task: asyncio.Task[None] | None = None
    closed: bool = False


class DashboardGateway:
    """Broadcasts validated state snapshots and real-time updates to dashboard clients."""

    def __init__(
        self,
        *,
        max_client_queue_size: int = 64,
        send_timeout_seconds: float = 1.0,
        recent_events_limit: int = 50,
        recent_traces_limit: int = 50,
    ) -> None:
        if max_client_queue_size < 1:
            msg = "max_client_queue_size must be >= 1"
            raise ValueError(msg)
        if send_timeout_seconds <= 0.0:
            msg = "send_timeout_seconds must be > 0"
            raise ValueError(msg)

        self._max_client_queue_size = max_client_queue_size
        self._send_timeout_seconds = send_timeout_seconds
        self._recent_events_limit = recent_events_limit
        self._recent_traces_limit = recent_traces_limit

        self._clients: dict[str, _DashboardClientConnection] = {}
        self._sequence: int = 0
        self._evicted_slow_clients: int = 0
        self._on_during_snapshot_hook: SnapshotHook | None = None

    @property
    def active_client_count(self) -> int:
        return len(self._clients)

    @property
    def evicted_slow_client_count(self) -> int:
        return self._evicted_slow_clients

    def set_snapshot_transition_hook(self, hook: SnapshotHook | None) -> None:
        """Register an optional test hook invoked mid-snapshot to test snapshot-to-live sync."""
        self._on_during_snapshot_hook = hook

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    async def build_snapshot_envelope(
        self,
        session: AsyncSession,
    ) -> DashboardSnapshotEnvelope:
        """Query authoritative SQLite state to construct a validated DashboardSnapshotEnvelope."""
        mission_stmt = (
            select(Mission)
            .order_by(Mission.created_at.desc(), Mission.id.desc())
            .limit(1)
            .options(selectinload(Mission.tasks))
        )
        latest_mission = (await session.execute(mission_stmt)).scalar_one_or_none()
        mission_detail = (
            MissionDetailRead.model_validate(latest_mission)
            if latest_mission is not None
            else None
        )

        vehicle_rows = (
            await session.execute(
                select(Vehicle).where(Vehicle.id.in_(["SCOUT01", "HAWK01"]))
            )
        ).scalars().all()

        vehicles_map: dict[VehicleIdEnum, VehicleRead] = {}
        for v_row in vehicle_rows:
            v_read = VehicleRead.model_validate(v_row)
            vehicles_map[v_read.id] = v_read

        events_stmt = select(Event)
        traces_stmt = select(ReasoningTrace)
        if latest_mission is not None:
            events_stmt = events_stmt.where(Event.mission_id == latest_mission.id)
            traces_stmt = traces_stmt.where(ReasoningTrace.mission_id == latest_mission.id)

        events_stmt = events_stmt.order_by(Event.timestamp.asc(), Event.id.asc()).limit(
            self._recent_events_limit
        )
        traces_stmt = traces_stmt.order_by(
            ReasoningTrace.timestamp.asc(), ReasoningTrace.id.asc()
        ).limit(self._recent_traces_limit)

        event_rows = (await session.execute(events_stmt)).scalars().all()
        trace_rows = (await session.execute(traces_stmt)).scalars().all()

        if self._on_during_snapshot_hook is not None:
            await self._on_during_snapshot_hook()

        now = datetime.now(timezone.utc)
        snapshot_payload = DashboardSnapshotPayload(
            generated_at=now,
            current_mission=mission_detail,
            vehicles=vehicles_map,
            recent_events=[EventRead.model_validate(e) for e in event_rows],
            recent_reasoning_traces=[
                ReasoningTraceRead.model_validate(rt) for rt in trace_rows
            ],
        )
        return DashboardSnapshotEnvelope(
            envelope_id=f"SNAP-{uuid.uuid4().hex[:8]}",
            timestamp=now,
            sequence=self._next_sequence(),
            payload=snapshot_payload,
        )

    async def _send_frame_with_timeout(
        self,
        client: _DashboardClientConnection,
        frame: dict[str, Any],
    ) -> bool:
        """Send a JSON frame to a single client within `send_timeout_seconds`."""
        try:
            await asyncio.wait_for(
                client.websocket.send_json(frame),
                timeout=self._send_timeout_seconds,
            )
            return True
        except ( asyncio.TimeoutError, WebSocketDisconnect, RuntimeError, Exception) as exc:
            if isinstance(exc, asyncio.TimeoutError):
                self._evicted_slow_clients += 1
                logger.warning(
                    f"Evicting slow dashboard client {client.client_id} (send timeout)."
                )
            await self._remove_client(client.client_id, close_code=1008)
            return False

    async def _client_sender_loop(self, client: _DashboardClientConnection) -> None:
        """Drain queued live envelopes for a connected dashboard client."""
        try:
            while not client.closed:
                frame = await client.queue.get()
                ok = await self._send_frame_with_timeout(client, frame)
                client.queue.task_done()
                if not ok:
                    break
        except asyncio.CancelledError:
            raise

    async def connect_client(
        self,
        websocket: WebSocket,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> str:
        """Accept a dashboard WebSocket, send initial snapshot, and transition to live stream."""
        await websocket.accept()
        client_id = f"dash-{uuid.uuid4().hex[:8]}"
        client = _DashboardClientConnection(
            client_id=client_id,
            websocket=websocket,
            queue=asyncio.Queue(maxsize=self._max_client_queue_size),
            syncing_snapshot=True,
        )
        # Register BEFORE snapshot query so concurrent broadcasts buffer in pre_snapshot_buffer
        self._clients[client_id] = client

        try:
            async with session_factory() as session:
                snapshot_envelope = await self.build_snapshot_envelope(session)

            snapshot_wire = snapshot_envelope.model_dump(mode="json")
            sent = await self._send_frame_with_timeout(client, snapshot_wire)
            if not sent:
                return client_id

            # Deduplicate buffered events/traces that were already captured in the snapshot
            included_event_ids = {
                e.id for e in snapshot_envelope.payload.recent_events
            }
            included_trace_ids = {
                rt.id for rt in snapshot_envelope.payload.recent_reasoning_traces
            }

            for buffered in client.pre_snapshot_buffer:
                if (
                    buffered.envelope_type == "event"
                    and buffered.entity_id in included_event_ids
                ):
                    continue
                if (
                    buffered.envelope_type == "reasoning_trace"
                    and buffered.entity_id in included_trace_ids
                ):
                    continue
                try:
                    client.queue.put_nowait(buffered.wire_dict)
                except asyncio.QueueFull:
                    self._evicted_slow_clients += 1
                    await self._remove_client(client_id, close_code=1008)
                    return client_id

            client.pre_snapshot_buffer.clear()
            client.syncing_snapshot = False
            client.sender_task = asyncio.create_task(
                self._client_sender_loop(client),
                name=f"dashboard-ws-sender-{client_id}",
            )
            return client_id
        except Exception:
            await self._remove_client(client_id, close_code=1011)
            raise

    async def handle_client_session(
        self,
        websocket: WebSocket,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Full FastAPI WebSocket endpoint lifecycle for `/ws/dashboard`."""
        client_id = await self.connect_client(websocket, session_factory)
        try:
            while client_id in self._clients:
                # Dashboard WebSocket is push-only (§4, §5.2); await receive to detect disconnect
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await self.disconnect_client(client_id)

    async def disconnect_client(self, client_id: str) -> None:
        """Remove a disconnected dashboard client and cancel its sender task."""
        await self._remove_client(client_id, close_code=None)

    async def _remove_client(
        self,
        client_id: str,
        *,
        close_code: int | None = None,
    ) -> None:
        client = self._clients.pop(client_id, None)
        if client is None:
            return
        client.closed = True
        if client.sender_task is not None and client.sender_task is not asyncio.current_task():
            client.sender_task.cancel()
            with suppress(asyncio.CancelledError):
                await client.sender_task
        client.sender_task = None
        if close_code is not None:
            with suppress(Exception):
                await client.websocket.close(code=close_code)

    async def close_all(self) -> None:
        """Cleanly close all connected dashboard WebSocket clients on shutdown."""
        for client_id in list(self._clients.keys()):
            await self._remove_client(client_id, close_code=1001)

    async def broadcast_envelope(
        self,
        envelope: (
            DashboardTelemetryEnvelope
            | DashboardEventEnvelope
            | DashboardReasoningTraceEnvelope
            | DashboardMissionStatusEnvelope
        ),
        *,
        entity_id: str | None = None,
    ) -> int:
        """Validate and enqueue an outbound envelope across all connected dashboard clients."""
        raw_dict = envelope.model_dump(mode="json")
        validated = validate_dashboard_ws_envelope(raw_dict)
        wire_dict = validated.model_dump(mode="json")
        env_type = str(validated.type)

        delivered_count = 0
        for client_id, client in list(self._clients.items()):
            if client.closed:
                continue
            if client.syncing_snapshot:
                if len(client.pre_snapshot_buffer) >= self._max_client_queue_size:
                    self._evicted_slow_clients += 1
                    logger.warning(
                        f"Evicting dashboard client {client_id} (pre-snapshot buffer full)."
                    )
                    await self._remove_client(client_id, close_code=1008)
                else:
                    client.pre_snapshot_buffer.append(
                        _BufferedEnvelope(
                            envelope_type=env_type,
                            entity_id=entity_id,
                            wire_dict=wire_dict,
                        )
                    )
                    delivered_count += 1
                continue

            try:
                client.queue.put_nowait(wire_dict)
                delivered_count += 1
            except asyncio.QueueFull:
                self._evicted_slow_clients += 1
                logger.warning(
                    f"Evicting slow dashboard client {client_id} (outbound queue full)."
                )
                await self._remove_client(client_id, close_code=1008)

        return delivered_count

    async def broadcast_telemetry(self, vehicle: VehicleRead) -> int:
        """Construct and broadcast a validated `telemetry` envelope (§5.2)."""
        envelope = DashboardTelemetryEnvelope(
            envelope_id=f"TEL-{uuid.uuid4().hex[:8]}",
            vehicle_id=vehicle.id,
            sequence=self._next_sequence(),
            payload=TelemetryPushPayload(
                vehicle_id=vehicle.id,
                status=vehicle.status,
                connection=vehicle.connection,
                mode=vehicle.mode,
                battery=vehicle.battery,
                position_x=vehicle.position_x,
                position_y=vehicle.position_y,
                position_alt=vehicle.position_alt,
                last_heartbeat=vehicle.last_heartbeat,
            ),
        )
        return await self.broadcast_envelope(envelope, entity_id=vehicle.id.value)

    async def broadcast_vehicle_dict(self, vehicle_data: dict[str, Any]) -> int:
        """Broadcast a telemetry update from raw vehicle state fields."""
        vid = VehicleIdEnum(str(vehicle_data["id"]))
        envelope = DashboardTelemetryEnvelope(
            envelope_id=f"TEL-{uuid.uuid4().hex[:8]}",
            vehicle_id=vid,
            sequence=self._next_sequence(),
            payload=TelemetryPushPayload(
                vehicle_id=vid,
                status=VehicleStatusEnum(str(vehicle_data["status"])),
                connection=VehicleConnectionStateEnum(str(vehicle_data["connection"])),
                mode=VehicleModeEnum(str(vehicle_data["mode"])),
                battery=float(vehicle_data["battery"]),
                position_x=float(vehicle_data["position_x"]),
                position_y=float(vehicle_data["position_y"]),
                position_alt=float(vehicle_data["position_alt"]),
                last_heartbeat=vehicle_data.get("last_heartbeat"),
            ),
        )
        return await self.broadcast_envelope(envelope, entity_id=vid.value)

    async def broadcast_event(self, event_read: EventRead) -> int:
        """Construct and broadcast a validated `event` envelope (§5.2)."""
        envelope = DashboardEventEnvelope(
            envelope_id=f"EVT-{uuid.uuid4().hex[:8]}",
            timestamp=event_read.timestamp,
            sequence=self._next_sequence(),
            payload=event_read,
        )
        return await self.broadcast_envelope(envelope, entity_id=event_read.id)

    async def broadcast_reasoning_trace(self, trace_read: ReasoningTraceRead) -> int:
        """Construct and broadcast a validated `reasoning_trace` envelope (§5.2)."""
        envelope = DashboardReasoningTraceEnvelope(
            envelope_id=f"RTR-{uuid.uuid4().hex[:8]}",
            timestamp=trace_read.timestamp,
            sequence=self._next_sequence(),
            payload=trace_read,
        )
        return await self.broadcast_envelope(envelope, entity_id=trace_read.id)

    async def broadcast_mission_status(
        self,
        mission_detail: MissionDetailRead,
    ) -> int:
        """Construct and broadcast a validated `mission_status` envelope (§5.2)."""
        envelope = DashboardMissionStatusEnvelope(
            envelope_id=f"MSN-{uuid.uuid4().hex[:8]}",
            sequence=self._next_sequence(),
            payload=MissionStatusPushPayload(
                mission_id=mission_detail.id,
                objective=mission_detail.objective,
                status=MissionStatusEnum(mission_detail.status),
                tasks=[TaskRead.model_validate(t) for t in mission_detail.tasks],
                completed_at=mission_detail.completed_at,
            ),
        )
        return await self.broadcast_envelope(envelope, entity_id=mission_detail.id)


dashboard_gateway = DashboardGateway()
