"""Milestone 6 automated tests: End-to-End Integration, Cold-Restart Safety, and Load/Latency.

Covers all Milestone 6 verification requirements:
1. Complete 17-step simulated mission workflow end to end:
   - Health check (`GET /health`), floor map (`GET /map`), and initial WebSocket snapshot (`/ws/dashboard`).
   - Mission submission (`POST /mission`), task decomposition, and persisted reasoning trace + event.
   - Simulated `SCOUT01` (ground) and `HAWK01` (aerial + altitude) movement and telemetry broadcast.
   - Simulated observation persistence (`GET /vehicle/{id}/observations`) and world-model propagation.
   - Simulated vehicle disconnect / heartbeat timeout -> `UNKNOWN` transition -> reconnect recovery.
   - Simulated mission completion (`POST /mission/{id}/complete`) and replay timeline verification (`GET /mission/{id}/replay`).
   - Individual (`SCOUT01`) and fleet-wide (`ALL`) emergency stop, plus partial-failure isolation when one vehicle fails.
   - Dashboard WebSocket disconnect and reconnect resynchronization from a fresh snapshot.
2. Cold-restart safety:
   - Restarting `init_db()` / `seed_default_vehicles()` against a database containing stale `CONNECTED` vehicles resets them to `DISCONNECTED` and `IDLE` so stale connections from a prior process are never reported as healthy.
3. Sustained load & latency measurement:
   - 2 simulated vehicles emitting telemetry at 250ms cadence with active dashboard WebSocket subscribers and emergency-stop fast-path dispatch latency measurement (recording median and max latency).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import statistics
import time
from typing import Any
from unittest.mock import AsyncMock

from core.config import Settings
from db.models import Vehicle
from db.session import seed_default_vehicles
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from main import create_app
from services.event_service import EventService
from services.mission_service import MissionService
from services.vehicle_service import VehicleService
from sqlalchemy import select
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
from websocket.vehicle_gateway import VehicleGatewayFastPath


def _build_m6_app(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[
    FastAPI,
    ManualClock,
    StubUltronAgent,
    MockFleetSimulator,
    DashboardGateway,
    VehicleGatewayFastPath,
    EventService,
    MissionService,
    VehicleService,
]:
    clock = ManualClock(datetime(2026, 10, 28, 14, 0, 0, tzinfo=timezone.utc))
    agent = StubUltronAgent()
    simulator = MockFleetSimulator(clock=clock)
    gateway = DashboardGateway()
    fast_path = VehicleGatewayFastPath()
    event_svc = EventService(gateway=gateway)
    mission_svc = MissionService(agent=agent, gateway=gateway, events=event_svc)
    vehicle_svc = VehicleService(
        agent=agent,
        clock=clock,
        simulator=simulator,
        gateway=gateway,
        app_settings=Settings(hardware_lockout=True, default_vehicle_mode="simulated"),
        heartbeat_interval_ms=250,
        heartbeat_timeout_ms=1000,
    )
    app = create_app(
        db_engine=isolated_engine,
        session_factory=session_factory,
        mission_svc=mission_svc,
        vehicle_svc=vehicle_svc,
        event_svc=event_svc,
        dash_gateway=gateway,
        estop_fast_path=fast_path,
    )
    return (
        app,
        clock,
        agent,
        simulator,
        gateway,
        fast_path,
        event_svc,
        mission_svc,
        vehicle_svc,
    )


async def test_complete_end_to_end_simulated_mission_workflow(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Execute the full 17-step simulated mission workflow end to end."""
    (
        test_app,
        clock,
        agent,
        simulator,
        gateway,
        fast_path,
        _event_svc,
        _mission_svc,
        vehicle_svc,
    ) = _build_m6_app(isolated_engine, session_factory)

    with TestClient(test_app) as client:
        # Steps 1-2: Verify backend health and provisional floor map
        health_res = client.get("/health")
        assert health_res.status_code == 200
        health = health_res.json()
        assert health["status"] == "ok"
        assert health["hardware_lockout"] is True
        assert health["default_vehicle_mode"] == "simulated"
        assert health["ultron_is_stub"] is True

        map_res = client.get("/map")
        assert map_res.status_code == 200
        assert map_res.json()["is_provisional_simulation_config"] is True

        # Step 3-4: Connect dashboard WebSocket and verify initial state snapshot
        with client.websocket_connect("/ws/dashboard") as ws1:
            snap1 = ws1.receive_json()
            assert snap1["type"] == "snapshot"
            assert snap1["payload"]["current_mission"] is None
            assert snap1["payload"]["vehicles"]["SCOUT01"]["mode"] == "simulated"
            assert snap1["payload"]["vehicles"]["HAWK01"]["mode"] == "simulated"

            # Step 5-6: Submit a simulated mission objective and verify tasks, event, and trace
            create_res = client.post(
                "/mission",
                json={"objective": "Sweep Sector Alpha and verify thermal signature"},
            )
            assert create_res.status_code == 200
            mission_id = create_res.json()["mission_id"]
            assert mission_id == "M001"

            # Drain WS messages emitted during mission creation (event, reasoning_trace, mission_status)
            ws_msgs_m1 = [ws1.receive_json(), ws1.receive_json(), ws1.receive_json()]
            ws_types_m1 = {m["type"] for m in ws_msgs_m1}
            assert ws_types_m1 == {"event", "reasoning_trace", "mission_status"}

            mission_res = client.get(f"/mission/{mission_id}")
            assert mission_res.status_code == 200
            m_detail = mission_res.json()
            assert m_detail["status"] == "ACTIVE"
            assert len(m_detail["tasks"]) == 2
            assert {t["vehicle_id"] for t in m_detail["tasks"]} == {"SCOUT01", "HAWK01"}

            # Step 7-8: Verify simulated mode toggle & hardware lockout, connect vehicles, step telemetry, and record observations
            sim_scout = client.post("/vehicle/SCOUT01/simulate", json={"enabled": True})
            sim_hawk = client.post("/vehicle/HAWK01/simulate", json={"enabled": True})
            assert sim_scout.status_code == 200
            assert sim_hawk.status_code == 200
            _ = ws1.receive_json()  # SCOUT01 mode broadcast
            _ = ws1.receive_json()  # HAWK01 mode broadcast

            lockout_res = client.post("/vehicle/SCOUT01/simulate", json={"enabled": False})
            assert lockout_res.status_code == 409
            assert lockout_res.json()["error_code"] == "HARDWARE_LOCKOUT_ACTIVE"

            async with session_factory() as session:
                await vehicle_svc.connect_vehicle(session, "SCOUT01")
                await vehicle_svc.connect_vehicle(session, "HAWK01")
            _ = ws1.receive_json()  # SCOUT01 connected broadcast
            _ = ws1.receive_json()  # HAWK01 connected broadcast

            simulator.scout.set_target_position(6.0, 4.0)
            simulator.hawk.set_target_position(10.0, 8.0, alt=2.2)

            async with session_factory() as session:
                clock.advance(milliseconds=250)
                beats = await simulator.step_all()
                for beat in beats.values():
                    assert beat is not None
                    await vehicle_svc.ingest_telemetry(session, beat, force_persist=True)

                obs_stub = await simulator.scout.emit_observation(
                    observation_id="OBS_E2E_01",
                    mission_id=mission_id,
                    obs_type="thermal_signature",
                    confidence=0.92,
                    location_x=3.0,
                    location_y=2.5,
                    raw={"temp_c": 41.8, "sector": "SECTOR_A"},
                )
                await vehicle_svc.record_observation(session, obs_stub)

            t_msg_1 = ws1.receive_json()
            t_msg_2 = ws1.receive_json()
            assert {t_msg_1["payload"]["vehicle_id"], t_msg_2["payload"]["vehicle_id"]} == {
                "SCOUT01",
                "HAWK01",
            }

            obs_res = client.get(
                "/vehicle/SCOUT01/observations",
                params={"mission_id": mission_id},
            )
            assert obs_res.status_code == 200
            assert len(obs_res.json()) == 1
            assert obs_res.json()[0]["id"] == "OBS_E2E_01"
            wm_snap = await agent.get_world_model_snapshot()
            assert len(wm_snap.observations) == 1
            assert wm_snap.observations[0]["id"] == "OBS_E2E_01"

            # Step 9-10: Simulate missed heartbeats -> UNKNOWN -> reconnect recovery
            clock.advance(milliseconds=1500)
            stale_status = client.get("/vehicle/SCOUT01/status").json()
            assert stale_status["is_stale"] is True

            async with session_factory() as session:
                timed_out = await vehicle_svc.evaluate_heartbeats(session)
                assert set(timed_out) == {"SCOUT01", "HAWK01"}

            unknown_frame_1 = ws1.receive_json()
            unknown_frame_2 = ws1.receive_json()
            assert unknown_frame_1["payload"]["connection"] == "UNKNOWN"
            assert unknown_frame_2["payload"]["connection"] == "UNKNOWN"

            async with session_factory() as session:
                await vehicle_svc.reconnect_vehicle(session, "SCOUT01")
                await vehicle_svc.reconnect_vehicle(session, "HAWK01")
            rec_1 = ws1.receive_json()
            rec_2 = ws1.receive_json()
            assert rec_1["payload"]["connection"] == "CONNECTED"
            assert rec_2["payload"]["connection"] == "CONNECTED"

            # Step 11-12: Complete the simulated mission and verify replay timeline
            complete_res = client.post(f"/mission/{mission_id}/complete")
            assert complete_res.status_code == 200
            completed_body = complete_res.json()
            assert completed_body["status"] == "COMPLETED"
            assert completed_body["completed_at"] is not None
            assert all(t["status"] == "COMPLETED" for t in completed_body["tasks"])

            # Drain completion WS messages (event, reasoning_trace, mission_status)
            comp_msgs = [ws1.receive_json(), ws1.receive_json(), ws1.receive_json()]
            assert {m["type"] for m in comp_msgs} == {
                "event",
                "reasoning_trace",
                "mission_status",
            }

            replay_res = client.get(f"/mission/{mission_id}/replay")
            assert replay_res.status_code == 200
            replay_entries = replay_res.json()["entries"]
            assert len(replay_entries) >= 4
            replay_timestamps = [
                datetime.fromisoformat(e["timestamp"]) for e in replay_entries
            ]
            assert replay_timestamps == sorted(replay_timestamps)

            # Step 13-15: Individual E-Stop, ALL E-Stop, and partial failure when one vehicle fails
            estop_scout = client.post("/emergency-stop", json={"vehicle_id": "SCOUT01"})
            assert estop_scout.status_code == 200
            assert estop_scout.json()["overall_outcome"] == "ALL_CONFIRMED_STOPPED"
            assert (
                estop_scout.json()["vehicle_outcomes"]["SCOUT01"][
                    "vehicle_confirmed_stopped"
                ]
                is True
            )
            # Drain E-Stop telemetry + safety event frames on ws1 so background logging completes
            _ = ws1.receive_json()
            _ = ws1.receive_json()

            # Inject transport fault on SCOUT01 and trigger ALL E-Stop -> PARTIAL_STOPPED
            simulator.scout.configure_faults(fail_emergency_stop_dispatch=True)
            estop_partial = client.post("/emergency-stop", json={"vehicle_id": "ALL"})
            assert estop_partial.status_code == 200
            p_body = estop_partial.json()
            assert p_body["overall_outcome"] == "PARTIAL_STOPPED"
            assert p_body["vehicle_outcomes"]["SCOUT01"]["dispatch_succeeded"] is False
            assert (
                p_body["vehicle_outcomes"]["HAWK01"]["vehicle_confirmed_stopped"]
                is True
            )
            # Drain partial E-Stop telemetry + safety event frames on ws1
            _ = ws1.receive_json()
            _ = ws1.receive_json()

        # Step 16: Reconnect dashboard WebSocket and verify full resynchronization snapshot
        with client.websocket_connect("/ws/dashboard") as ws2:
            snap2 = ws2.receive_json()
            assert snap2["type"] == "snapshot"
            assert snap2["payload"]["current_mission"]["id"] == mission_id
            assert snap2["payload"]["current_mission"]["status"] == "COMPLETED"
            assert (
                snap2["payload"]["vehicles"]["HAWK01"]["status"]
                == "EMERGENCY_STOPPED"
            )

    await fast_path.shutdown()
    await gateway.close_all()
    await vehicle_svc.stop()


async def test_cold_restart_resets_stale_connected_vehicles(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify restarting the backend resets stale CONNECTED vehicle rows to DISCONNECTED/IDLE."""
    async with session_factory() as session:
        scout = await session.get(Vehicle, "SCOUT01")
        assert scout is not None
        scout.connection = "CONNECTED"
        scout.status = "EXECUTING"
        await session.commit()

    # Simulate cold boot startup seeding against the existing database
    async with session_factory() as session:
        await seed_default_vehicles(session)

    async with session_factory() as session:
        result = await session.execute(
            select(Vehicle).where(Vehicle.id == "SCOUT01")
        )
        refreshed = result.scalar_one()
        assert refreshed.connection == "DISCONNECTED"
        assert refreshed.status == "IDLE"


async def test_sustained_two_vehicle_telemetry_and_emergency_stop_latency_benchmark(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Measure telemetry-to-dashboard latency and emergency-stop dispatch latency under 2-vehicle load."""
    (
        test_app,
        clock,
        _agent,
        simulator,
        gateway,
        fast_path,
        _event_svc,
        _mission_svc,
        vehicle_svc,
    ) = _build_m6_app(isolated_engine, session_factory)

    async with session_factory() as session:
        await vehicle_svc.connect_vehicle(session, "SCOUT01")
        await vehicle_svc.connect_vehicle(session, "HAWK01")

    simulator.scout.set_target_position(15.0, 10.0)
    simulator.hawk.set_target_position(12.0, 8.0, alt=2.5)

    # Register an in-memory WebSocket subscriber to measure telemetry ingest -> WS client delivery
    received_frames: list[dict[str, Any]] = []
    mock_ws = AsyncMock()
    mock_ws.send_json.side_effect = lambda data: received_frames.append(data)

    ws_client_id = await gateway.connect_client(mock_ws, session_factory)

    telemetry_latencies_ms: list[float] = []
    # Run 25 ticks across 2 vehicles = 50 telemetry updates (representing 6.25s of 4Hz fleet traffic)
    async with session_factory() as session:
        for _ in range(25):
            clock.advance(milliseconds=250)
            beats = await simulator.step_all()
            for beat in beats.values():
                assert beat is not None
                t0 = time.perf_counter()
                await vehicle_svc.ingest_telemetry(session, beat)
                await asyncio.sleep(0)  # Yield once so client sender task delivers frame
                t1 = time.perf_counter()
                telemetry_latencies_ms.append((t1 - t0) * 1000.0)

    estop_dispatch_ms: list[float] = []
    estop_http_ms: list[float] = []
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(20):
            t0 = time.perf_counter()
            res = await client.post("/emergency-stop", json={"vehicle_id": "ALL"})
            t1 = time.perf_counter()
            assert res.status_code == 200
            body = res.json()
            assert body["overall_outcome"] == "ALL_CONFIRMED_STOPPED"
            estop_dispatch_ms.append(float(body["dispatch_elapsed_ms"]))
            estop_http_ms.append((t1 - t0) * 1000.0)

    await fast_path.wait_for_pending_logs()

    telemetry_median = statistics.median(telemetry_latencies_ms)
    telemetry_max = max(telemetry_latencies_ms)
    estop_dispatch_median = statistics.median(estop_dispatch_ms)
    estop_dispatch_max = max(estop_dispatch_ms)
    estop_http_median = statistics.median(estop_http_ms)
    estop_http_max = max(estop_http_ms)

    print(
        f"\n[BENCHMARK] Telemetry ingest->WS (n={len(telemetry_latencies_ms)}): "
        f"median={telemetry_median:.3f}ms, max={telemetry_max:.3f}ms | "
        f"E-Stop fast-path dispatch (n={len(estop_dispatch_ms)}): "
        f"median={estop_dispatch_median:.3f}ms, max={estop_dispatch_max:.3f}ms | "
        f"E-Stop ASGI HTTP roundtrip (n={len(estop_http_ms)}): "
        f"median={estop_http_median:.3f}ms, max={estop_http_max:.3f}ms"
    )

    # Verify performance targets (< 500ms telemetry pipeline, < 100ms simulated E-Stop fast path)
    assert len(received_frames) >= 51  # 1 initial snapshot + 50 telemetry broadcasts
    assert telemetry_median < 50.0
    assert telemetry_max < 500.0
    assert estop_dispatch_median < 10.0
    assert estop_dispatch_max < 100.0
    assert estop_http_median < 50.0
    assert estop_http_max < 100.0

    await gateway.disconnect_client(ws_client_id)
    await fast_path.shutdown()
    await gateway.close_all()
    await vehicle_svc.stop()
