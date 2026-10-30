"""Milestone 3B automated tests: REST API endpoints and Dashboard WebSocket Gateway.

Covers all Section 6 requirements:
1. Mission endpoints: POST /mission, GET /mission/{id}, GET /mission/{id}/replay, and replay chronological ordering.
2. Vehicle endpoints: GET /vehicle/{id}/status, /sensors, /observations, POST /command (auth + structured validation), POST /simulate (hardware lockout).
3. Events & Map endpoints: GET /events (with optional mission_id filter) and GET /map (provisional floor plan).
4. Request validation (422) and unknown identifiers (404).
5. Dashboard WebSocket Gateway: initial full-state snapshot on connect, live broadcasts to multiple clients, mock telemetry propagation, snapshot-to-live transition synchronization (zero lost updates + deduplication), slow/failed client eviction without blocking healthy clients, and clean shutdown.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

from core.config import Settings, settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from main import create_app
import pytest
from schemas.enums import EventCategoryEnum
from services.event_service import EventService
from services.mission_service import MissionService
from services.vehicle_service import VehicleService
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from ultron_stub import (
    ManualClock,
    MockFleetSimulator,
    StubUltronAgent,
)
from websocket.dashboard_gateway import DashboardGateway


def _build_isolated_app(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clock: ManualClock | None = None,
    max_client_queue_size: int = 64,
    send_timeout_seconds: float = 1.0,
) -> tuple[
    FastAPI,
    ManualClock,
    StubUltronAgent,
    MockFleetSimulator,
    DashboardGateway,
    EventService,
    MissionService,
    VehicleService,
]:
    """Construct a fully isolated FastAPI app and services bound to the test's temporary DB."""
    active_clock = clock or ManualClock(datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc))
    agent = StubUltronAgent()
    simulator = MockFleetSimulator(clock=active_clock)
    gateway = DashboardGateway(
        max_client_queue_size=max_client_queue_size,
        send_timeout_seconds=send_timeout_seconds,
    )
    event_svc = EventService(gateway=gateway)
    mission_svc = MissionService(agent=agent, gateway=gateway, events=event_svc)
    vehicle_svc = VehicleService(
        agent=agent,
        clock=active_clock,
        simulator=simulator,
        gateway=gateway,
        app_settings=Settings(hardware_lockout=True, default_vehicle_mode="simulated"),
        heartbeat_interval_ms=250,
        heartbeat_timeout_ms=1000,
    )
    test_app = create_app(
        db_engine=isolated_engine,
        session_factory=session_factory,
        mission_svc=mission_svc,
        vehicle_svc=vehicle_svc,
        event_svc=event_svc,
        dash_gateway=gateway,
    )
    return (
        test_app,
        active_clock,
        agent,
        simulator,
        gateway,
        event_svc,
        mission_svc,
        vehicle_svc,
    )


# ---------------------------------------------------------------------------
# 1. Mission Endpoints, Replay Ordering, Events & Map Endpoints
# ---------------------------------------------------------------------------


async def test_mission_lifecycle_replay_ordering_events_and_map(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify POST /mission, GET /mission/{id}, GET /mission/{id}/replay, GET /events, and GET /map."""
    (
        test_app,
        clock,
        _agent,
        _simulator,
        gateway,
        event_svc,
        _mission_svc,
        _vehicle_svc,
    ) = _build_isolated_app(isolated_engine, session_factory)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Invalid empty objective rejected with 422
        bad_res = await client.post("/mission", json={"objective": "   "})
        assert bad_res.status_code == 422
        assert bad_res.json()["error_code"] == "VALIDATION_ERROR"

        # 2. Valid POST /mission creates M001 and simulated tasks
        create_res = await client.post(
            "/mission",
            json={"objective": "Locate thermal source in Sector Bravo"},
        )
        assert create_res.status_code == 200
        assert create_res.json() == {"mission_id": "M001"}

        # 3. GET /mission/M001 returns full state + 2 simulated tasks
        detail_res = await client.get("/mission/M001")
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert detail["id"] == "M001"
        assert detail["objective"] == "Locate thermal source in Sector Bravo"
        assert detail["status"] == "ACTIVE"
        assert len(detail["tasks"]) == 2
        assert {t["vehicle_id"] for t in detail["tasks"]} == {"SCOUT01", "HAWK01"}

        # 4. Unknown mission returns 404
        not_found = await client.get("/mission/M999")
        assert not_found.status_code == 404
        assert not_found.json()["error_code"] == "RESOURCE_NOT_FOUND"

        not_found_replay = await client.get("/mission/M999/replay")
        assert not_found_replay.status_code == 404

        # 5. Append additional interleaved event and reasoning trace for replay test
        base_time = datetime.fromisoformat(detail["created_at"])
        async with session_factory() as session:
            await event_svc.record_event(
                session,
                mission_id="M001",
                category=EventCategoryEnum.OBSERVATION,
                message="SCOUT01 detected corridor obstacle",
                event_id="M001-EVT02",
                timestamp=base_time + timedelta(seconds=5),
            )
            await event_svc.record_reasoning_trace(
                session,
                mission_id="M001",
                trigger="Obstacle in corridor",
                rationale="[ULTRON_STUB] Holding SCOUT01, continuing HAWK01 scan.",
                resulting_task_ids=["M001-T02"],
                trace_id="M001-RT02",
                timestamp=base_time + timedelta(seconds=6),
            )

        # 6. Verify GET /mission/M001/replay returns strictly chronological timeline
        replay_res = await client.get("/mission/M001/replay")
        assert replay_res.status_code == 200
        replay = replay_res.json()
        assert replay["mission_id"] == "M001"
        assert len(replay["entries"]) == 4
        assert [e["entry_type"] for e in replay["entries"]] == [
            "event",
            "reasoning_trace",
            "event",
            "reasoning_trace",
        ]
        timestamps = [
            datetime.fromisoformat(e["timestamp"]) for e in replay["entries"]
        ]
        assert timestamps == sorted(timestamps)

        # 7. Verify GET /events and GET /events?mission_id=M001
        events_all = await client.get("/events")
        assert events_all.status_code == 200
        assert len(events_all.json()) == 2

        events_m001 = await client.get("/events", params={"mission_id": "M001"})
        assert events_m001.status_code == 200
        assert [e["id"] for e in events_m001.json()] == ["M001-EVT01", "M001-EVT02"]

        events_unknown = await client.get("/events", params={"mission_id": "M999"})
        assert events_unknown.status_code == 404

        # 8. Verify GET /map returns provisional simulation floor-plan config
        map_res = await client.get("/map")
        assert map_res.status_code == 200
        map_body = map_res.json()
        assert map_body["is_provisional_simulation_config"] is True
        assert map_body["width_m"] == 20.0
        assert map_body["height_m"] == 15.0
        assert len(map_body["rooms"]) >= 3
        assert len(map_body["waypoints"]) >= 3
        assert map_body["hawk_geofence"]["id"] == "GEOFENCE_HAWK01"

    await gateway.close_all()
    assert clock.now() is not None


# ---------------------------------------------------------------------------
# 2. Vehicle Endpoints: Status, Sensors, Observations, Auth Commands & Simulate
# ---------------------------------------------------------------------------


async def test_vehicle_endpoints_status_sensors_observations_commands_and_simulate(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify GET/POST /vehicle/{id}/* endpoints, stale detection, auth, and hardware lockout."""
    (
        test_app,
        clock,
        _agent,
        simulator,
        gateway,
        _event_svc,
        _mission_svc,
        vehicle_svc,
    ) = _build_isolated_app(isolated_engine, session_factory)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Unknown vehicle returns 404
        res_404 = await client.get("/vehicle/TANK99/status")
        assert res_404.status_code == 404

        # 2. Initial SCOUT01 status is DISCONNECTED, mode=simulated, is_stale=False
        status_initial = await client.get("/vehicle/SCOUT01/status")
        assert status_initial.status_code == 200
        s_init = status_initial.json()
        assert s_init["id"] == "SCOUT01"
        assert s_init["connection"] == "DISCONNECTED"
        assert s_init["mode"] == "simulated"
        assert s_init["is_stale"] is False

        # 3. Connect SCOUT01, step movement, and ingest telemetry
        async with session_factory() as session:
            await vehicle_svc.connect_vehicle(session, "SCOUT01")
            simulator.scout.set_target_position(2.0, 3.0)
            clock.advance(milliseconds=250)
            beat = await simulator.scout.step()
            assert beat is not None
            await vehicle_svc.ingest_telemetry(session, beat, force_persist=True)

        status_live = await client.get("/vehicle/SCOUT01/status")
        assert status_live.status_code == 200
        assert status_live.json()["connection"] == "CONNECTED"
        assert status_live.json()["is_stale"] is False

        sensors_live = await client.get("/vehicle/SCOUT01/sensors")
        assert sensors_live.status_code == 200
        sens = sensors_live.json()
        assert sens["vehicle_id"] == "SCOUT01"
        assert sens["sequence"] >= 1
        assert sens["raw_sensors"]["is_simulated"] is True

        # 4. Advance clock past heartbeat timeout (1500ms > 1000ms) -> is_stale=True
        clock.advance(milliseconds=1500)
        status_stale = await client.get("/vehicle/SCOUT01/status")
        assert status_stale.status_code == 200
        assert status_stale.json()["is_stale"] is True

        # Evaluate heartbeats -> transitions to UNKNOWN
        async with session_factory() as session:
            await vehicle_svc.evaluate_heartbeats(session)

        status_unknown = await client.get("/vehicle/SCOUT01/status")
        assert status_unknown.json()["connection"] == "UNKNOWN"
        assert status_unknown.json()["status"] == "UNKNOWN"

        # Reconnect SCOUT01
        async with session_factory() as session:
            await vehicle_svc.reconnect_vehicle(session, "SCOUT01")

        # 5. Create mission & record observation for HAWK01, then query GET /vehicle/HAWK01/observations
        m_res = await client.post("/mission", json={"objective": "Observation test"})
        mission_id = m_res.json()["mission_id"]

        async with session_factory() as session:
            await vehicle_svc.connect_vehicle(session, "HAWK01")
            obs_stub = await simulator.hawk.emit_observation(
                observation_id="OBS_V_01",
                mission_id=mission_id,
                obs_type="target_marker",
                confidence=0.95,
                location_x=6.0,
                location_y=7.0,
                raw={"marker_id": "M-12"},
            )
            await vehicle_svc.record_observation(session, obs_stub)

        obs_res = await client.get(
            "/vehicle/HAWK01/observations",
            params={"mission_id": mission_id},
        )
        assert obs_res.status_code == 200
        obs_list = obs_res.json()
        assert len(obs_list) == 1
        assert obs_list[0]["id"] == "OBS_V_01"
        assert obs_list[0]["raw"] == {"marker_id": "M-12"}

        # 6. POST /vehicle/SCOUT01/command requires shared-secret token and structured command
        unauth_cmd = await client.post(
            "/vehicle/SCOUT01/command",
            json={"command_type": "HOLD_POSITION", "parameters": {}},
        )
        assert unauth_cmd.status_code == 401

        invalid_text_cmd = await client.post(
            "/vehicle/SCOUT01/command",
            headers={"X-Vehicle-Token": settings.vehicle_shared_secret},
            json={"command_type": "drive forward ten meters", "parameters": {}},
        )
        assert invalid_text_cmd.status_code == 422

        valid_cmd = await client.post(
            "/vehicle/SCOUT01/command",
            headers={"X-Vehicle-Token": settings.vehicle_shared_secret},
            json={
                "command_id": "CMD_MANUAL_01",
                "command_type": "HOLD_POSITION",
                "parameters": {"duration_s": 5},
            },
        )
        assert valid_cmd.status_code == 200
        ack = valid_cmd.json()
        assert ack["command_id"] == "CMD_MANUAL_01"
        assert ack["status"] == "ACCEPTED"
        assert ack["is_simulated"] is True

        # 7. POST /vehicle/SCOUT01/simulate: enabled=True succeeds, enabled=False rejected by hardware lockout
        sim_on = await client.post(
            "/vehicle/SCOUT01/simulate",
            json={"enabled": True},
        )
        assert sim_on.status_code == 200
        assert sim_on.json()["mode"] == "simulated"
        assert sim_on.json()["enabled"] is True

        sim_off = await client.post(
            "/vehicle/SCOUT01/simulate",
            json={"enabled": False},
        )
        assert sim_off.status_code == 409
        assert sim_off.json()["error_code"] == "HARDWARE_LOCKOUT_ACTIVE"

    await gateway.close_all()
    await vehicle_svc.stop()


# ---------------------------------------------------------------------------
# 3. Dashboard WebSocket Gateway: Snapshot, Multi-Client Broadcast & Sync
# ---------------------------------------------------------------------------


async def test_dashboard_ws_snapshot_multi_client_broadcast_and_telemetry(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify /ws/dashboard initial snapshot and live broadcasts to multiple connected clients."""
    (
        test_app,
        clock,
        _agent,
        simulator,
        gateway,
        _event_svc,
        mission_svc,
        vehicle_svc,
    ) = _build_isolated_app(isolated_engine, session_factory)

    # Create an active mission before clients connect so the initial snapshot includes it
    async with session_factory() as session:
        from schemas.entities import MissionCreateRequest

        await mission_svc.create_mission(
            session,
            MissionCreateRequest(objective="Pre-existing mission for snapshot"),
        )
        await vehicle_svc.connect_vehicle(session, "SCOUT01")

    with TestClient(test_app) as sync_client:
        with (
            sync_client.websocket_connect("/ws/dashboard") as ws1,
            sync_client.websocket_connect("/ws/dashboard") as ws2,
        ):
            snap1 = ws1.receive_json()
            snap2 = ws2.receive_json()

            assert snap1["type"] == "snapshot"
            assert snap1["protocol_version"] == "1.0"
            assert snap1["payload"]["current_mission"]["id"] == "M001"
            assert "SCOUT01" in snap1["payload"]["vehicles"]
            assert "HAWK01" in snap1["payload"]["vehicles"]
            assert len(snap1["payload"]["recent_events"]) >= 1
            assert len(snap1["payload"]["recent_reasoning_traces"]) >= 1

            assert snap2["type"] == "snapshot"
            assert snap2["payload"]["current_mission"]["id"] == "M001"
            assert gateway.active_client_count == 2

            # Trigger a mock vehicle telemetry update via REST simulate endpoint
            sim_resp = sync_client.post(
                "/vehicle/HAWK01/simulate",
                json={"enabled": True},
            )
            assert sim_resp.status_code == 200

            push1 = ws1.receive_json()
            push2 = ws2.receive_json()
            assert push1["type"] == "telemetry"
            assert push1["payload"]["vehicle_id"] == "HAWK01"
            assert push2["type"] == "telemetry"
            assert push2["payload"]["vehicle_id"] == "HAWK01"

        # After exiting context managers, disconnected clients are cleaned up
        await asyncio.sleep(0.05)
        assert gateway.active_client_count == 0

    await gateway.close_all()
    await vehicle_svc.stop()
    assert simulator.scout is not None
    assert clock.now() is not None


async def test_dashboard_ws_snapshot_to_live_transition_and_slow_client_isolation(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify updates during snapshot build are buffered (never lost) and slow clients are evicted."""
    (
        _test_app,
        clock,
        _agent,
        simulator,
        gateway,
        event_svc,
        mission_svc,
        vehicle_svc,
    ) = _build_isolated_app(
        isolated_engine,
        session_factory,
        max_client_queue_size=2,
        send_timeout_seconds=0.05,
    )

    async with session_factory() as session:
        from schemas.entities import MissionCreateRequest

        await mission_svc.create_mission(
            session,
            MissionCreateRequest(objective="Test snapshot-to-live barrier"),
        )
        await vehicle_svc.connect_vehicle(session, "SCOUT01")

    # Configure a mid-snapshot hook that emits a live telemetry push + new event
    # while the client's initial snapshot is still being constructed!
    async def _mid_snapshot_concurrent_updates() -> None:
        async with session_factory() as hook_session:
            clock.advance(milliseconds=250)
            simulator.scout.set_target_position(4.0, 4.0)
            beat = await simulator.scout.step()
            assert beat is not None
            await vehicle_svc.ingest_telemetry(hook_session, beat, force_persist=True)
            await event_svc.record_event(
                hook_session,
                mission_id="M001",
                category=EventCategoryEnum.TELEMETRY,
                message="Concurrent event emitted during snapshot construction",
                event_id="M001-EVT-CONCURRENT",
            )

    gateway.set_snapshot_transition_hook(_mid_snapshot_concurrent_updates)

    sent_frames_healthy: list[dict[str, Any]] = []
    mock_ws_healthy = AsyncMock()
    mock_ws_healthy.send_json.side_effect = lambda data: sent_frames_healthy.append(data)

    client_id_healthy = await gateway.connect_client(mock_ws_healthy, session_factory)
    gateway.set_snapshot_transition_hook(None)

    # Wait briefly for sender task to drain the pre-snapshot buffer
    await asyncio.sleep(0.05)

    # First frame MUST be the snapshot; subsequent frames MUST be the buffered telemetry + event
    assert len(sent_frames_healthy) >= 3
    assert sent_frames_healthy[0]["type"] == "snapshot"
    subsequent_types = [f["type"] for f in sent_frames_healthy[1:]]
    assert "telemetry" in subsequent_types
    assert "event" in subsequent_types
    concurrent_event_frames = [
        f
        for f in sent_frames_healthy[1:]
        if f["type"] == "event" and f["payload"]["id"] == "M001-EVT-CONCURRENT"
    ]
    assert len(concurrent_event_frames) == 1

    # Now connect a slow/stalled client whose send_json hangs past send_timeout_seconds (0.05s)
    async def _slow_send(_data: dict[str, Any]) -> None:
        await asyncio.sleep(0.5)

    mock_ws_slow = AsyncMock()
    mock_ws_slow.send_json.side_effect = _slow_send

    # Initial snapshot send on slow client times out and evicts the slow client immediately
    await gateway.connect_client(mock_ws_slow, session_factory)
    assert gateway.evicted_slow_client_count >= 1
    assert gateway.active_client_count == 1  # Healthy client remains connected!

    # Broadcasting another update still reaches the healthy client without error
    async with session_factory() as session:
        await event_svc.record_event(
            session,
            mission_id="M001",
            category=EventCategoryEnum.SYSTEM,
            message="Post-eviction broadcast to healthy client",
            event_id="M001-EVT-POST",
        )
    await asyncio.sleep(0.05)

    assert any(
        f["type"] == "event" and f["payload"]["id"] == "M001-EVT-POST"
        for f in sent_frames_healthy
    )

    await gateway.disconnect_client(client_id_healthy)
    await gateway.close_all()
    await vehicle_svc.stop()
    assert gateway.active_client_count == 0
