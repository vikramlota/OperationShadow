"""Milestone 4 automated tests: Emergency Stop Fast Path (§7, §10, §11).

Covers all 12 required verification areas:
1. Individual simulated vehicle (`SCOUT01` or `HAWK01`) receives stop request without affecting the other.
2. `ALL` attempts dispatch to both simulated vehicles.
3. Failure for one vehicle does not prevent an attempt and stop confirmation for the other (`PARTIAL_STOPPED`).
4. Disconnected or `UNKNOWN` vehicle produces an accurate failure/unconfirmed outcome.
5. Agent planning and ordinary task/command processing (`send_command`) are bypassed by the emergency-stop path.
6. Database logging failure does not block or fail emergency-stop dispatch.
7. Deterministic ordering check: dispatch is initiated and completed strictly before event logging starts.
8. Malformed requests and invalid vehicle identifiers are rejected with 422.
9. Repeated emergency-stop requests are handled safely (`was_already_stopped=True`).
10. Real-hardware control remains locked out (`LockedRealVehicleAdapter` reports `real_hardware_available=False`).
11. HTTP 200 acknowledgement (`request_accepted=True`) is explicitly distinguished from `vehicle_confirmed_stopped`.
12. Simulated backend fast-path latency benchmark (< 100ms target for simulated in-process dispatch).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import time

from core.config import Settings
from db.models import Event, Vehicle
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from main import create_app
import pytest
from schemas.entities import MissionCreateRequest
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
    LockedRealVehicleAdapter,
    ManualClock,
    MockFleetSimulator,
    StubUltronAgent,
)
from websocket.dashboard_gateway import DashboardGateway
from websocket.vehicle_gateway import VehicleGatewayFastPath


def _build_m4_app(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[
    FastAPI,
    ManualClock,
    StubUltronAgent,
    MockFleetSimulator,
    DashboardGateway,
    VehicleGatewayFastPath,
    MissionService,
    VehicleService,
]:
    clock = ManualClock(datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc))
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
    return app, clock, agent, simulator, gateway, fast_path, mission_svc, vehicle_svc


# ---------------------------------------------------------------------------
# 1, 2, 8 & 9: Individual Stop, ALL Stop, Validation & Repeated Request Safety
# ---------------------------------------------------------------------------


async def test_individual_and_all_simulated_emergency_stop_and_idempotency(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify individual stop, ALL stop, repeated request handling, and malformed rejection."""
    (
        test_app,
        _clock,
        _agent,
        simulator,
        gateway,
        fast_path,
        mission_svc,
        vehicle_svc,
    ) = _build_m4_app(isolated_engine, session_factory)

    async with session_factory() as session:
        await mission_svc.create_mission(
            session,
            MissionCreateRequest(objective="Active mission during E-Stop test"),
        )
        await vehicle_svc.connect_vehicle(session, "SCOUT01")
        await vehicle_svc.connect_vehicle(session, "HAWK01")

    simulator.scout.set_target_position(8.0, 4.0)
    simulator.scout.transition_task_state("M001-T01", "IN_PROGRESS")
    simulator.hawk.set_target_position(5.0, 5.0, alt=2.0)
    simulator.hawk.transition_task_state("M001-T02", "IN_PROGRESS")

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 8. Reject invalid target and malformed request
        bad_target = await client.post("/emergency-stop", json={"vehicle_id": "TANK99"})
        assert bad_target.status_code == 422

        missing_body = await client.post("/emergency-stop", json={})
        assert missing_body.status_code == 422

        # 1. Stop SCOUT01 individually; HAWK01 must continue unaffected
        res_scout = await client.post("/emergency-stop", json={"vehicle_id": "SCOUT01"})
        assert res_scout.status_code == 200
        body_scout = res_scout.json()
        assert body_scout["request_accepted"] is True
        assert body_scout["target"] == "SCOUT01"
        assert body_scout["overall_outcome"] == "ALL_CONFIRMED_STOPPED"
        assert body_scout["all_confirmed_stopped"] is True
        assert body_scout["any_dispatch_failed"] is False
        assert body_scout["real_hardware_estop_available"] is False
        assert body_scout["hardware_lockout_active"] is True

        scout_outcome = body_scout["vehicle_outcomes"]["SCOUT01"]
        assert scout_outcome["is_simulated"] is True
        assert scout_outcome["dispatch_attempted"] is True
        assert scout_outcome["dispatch_succeeded"] is True
        assert scout_outcome["vehicle_acknowledged"] is True
        assert scout_outcome["vehicle_confirmed_stopped"] is True
        assert scout_outcome["resulting_status"] == "EMERGENCY_STOPPED"
        assert scout_outcome["was_already_stopped"] is False

        # Confirm HAWK01 was not stopped by SCOUT01's individual stop
        assert simulator.hawk.get_state().status == "EXECUTING"

        # 9. Repeated stop on SCOUT01 is handled safely and reports was_already_stopped=True
        res_repeat = await client.post("/emergency-stop", json={"vehicle_id": "SCOUT01"})
        assert res_repeat.status_code == 200
        repeat_outcome = res_repeat.json()["vehicle_outcomes"]["SCOUT01"]
        assert repeat_outcome["was_already_stopped"] is True
        assert repeat_outcome["vehicle_confirmed_stopped"] is True

        # 2. Trigger ALL stop -> stops both SCOUT01 and HAWK01
        res_all = await client.post("/emergency-stop", json={"vehicle_id": "ALL"})
        assert res_all.status_code == 200
        body_all = res_all.json()
        assert body_all["target"] == "ALL"
        assert body_all["overall_outcome"] == "ALL_CONFIRMED_STOPPED"
        assert set(body_all["vehicle_outcomes"].keys()) == {"SCOUT01", "HAWK01"}
        assert body_all["vehicle_outcomes"]["HAWK01"]["vehicle_confirmed_stopped"] is True
        assert body_all["vehicle_outcomes"]["HAWK01"]["was_already_stopped"] is False
        assert body_all["vehicle_outcomes"]["SCOUT01"]["was_already_stopped"] is True

    await fast_path.wait_for_pending_logs()

    # Verify post-dispatch async DB logging recorded SAFETY events and updated vehicle status
    async with session_factory() as session:
        v_rows = (await session.execute(select(Vehicle))).scalars().all()
        assert all(v.status == "EMERGENCY_STOPPED" for v in v_rows)
        safety_events = (
            await session.execute(
                select(Event).where(Event.category == "SAFETY")
            )
        ).scalars().all()
        assert len(safety_events) == 3

    await fast_path.shutdown()
    await gateway.close_all()
    await vehicle_svc.stop()


# ---------------------------------------------------------------------------
# 3, 4 & 11: Independent Per-Vehicle Failure Isolation & Unconfirmed States
# ---------------------------------------------------------------------------


async def test_partial_failure_disconnected_and_unknown_distinguish_acceptance_from_stopped(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify failure on one vehicle does not block the other, and HTTP 200 != confirmed stop."""
    (
        test_app,
        _clock,
        _agent,
        simulator,
        gateway,
        fast_path,
        _mission_svc,
        vehicle_svc,
    ) = _build_m4_app(isolated_engine, session_factory)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 4 & 11. Both vehicles start DISCONNECTED -> HTTP 200 request_accepted=True,
        # but overall_outcome == NONE_CONFIRMED_STOPPED and vehicle_confirmed_stopped is False!
        res_disc = await client.post("/emergency-stop", json={"vehicle_id": "ALL"})
        assert res_disc.status_code == 200
        body_disc = res_disc.json()
        assert body_disc["request_accepted"] is True
        assert body_disc["overall_outcome"] == "NONE_CONFIRMED_STOPPED"
        assert body_disc["all_confirmed_stopped"] is False
        assert body_disc["any_dispatch_failed"] is True
        for vid in ("SCOUT01", "HAWK01"):
            outcome = body_disc["vehicle_outcomes"][vid]
            assert outcome["connection_at_dispatch"] == "DISCONNECTED"
            assert outcome["dispatch_attempted"] is True
            assert outcome["dispatch_succeeded"] is False
            assert outcome["vehicle_acknowledged"] is False
            assert outcome["vehicle_confirmed_stopped"] is False

        # Connect both vehicles, then mark SCOUT01 as UNKNOWN (missed heartbeat)
        async with session_factory() as session:
            await vehicle_svc.connect_vehicle(session, "SCOUT01")
            await vehicle_svc.connect_vehicle(session, "HAWK01")
        simulator.scout.mark_unknown_by_monitor()

        res_unknown = await client.post("/emergency-stop", json={"vehicle_id": "SCOUT01"})
        assert res_unknown.status_code == 200
        u_out = res_unknown.json()["vehicle_outcomes"]["SCOUT01"]
        assert u_out["connection_at_dispatch"] == "UNKNOWN"
        assert u_out["dispatch_attempted"] is True
        assert u_out["dispatch_succeeded"] is False
        assert u_out["vehicle_acknowledged"] is False
        assert u_out["vehicle_confirmed_stopped"] is False
        assert u_out["resulting_status"] == "UNKNOWN"

        # 3. Reconnect SCOUT01, then inject transport failure on SCOUT01 only;
        # ALL must still stop HAWK01 and report PARTIAL_STOPPED!
        async with session_factory() as session:
            await vehicle_svc.reconnect_vehicle(session, "SCOUT01")
        simulator.scout.configure_faults(fail_emergency_stop_dispatch=True)

        res_partial = await client.post("/emergency-stop", json={"vehicle_id": "ALL"})
        assert res_partial.status_code == 200
        body_partial = res_partial.json()
        assert body_partial["overall_outcome"] == "PARTIAL_STOPPED"
        assert body_partial["all_confirmed_stopped"] is False
        assert body_partial["any_dispatch_failed"] is True
        assert body_partial["vehicle_outcomes"]["SCOUT01"]["dispatch_succeeded"] is False
        assert body_partial["vehicle_outcomes"]["SCOUT01"]["vehicle_confirmed_stopped"] is False
        assert body_partial["vehicle_outcomes"]["HAWK01"]["dispatch_succeeded"] is True
        assert body_partial["vehicle_outcomes"]["HAWK01"]["vehicle_confirmed_stopped"] is True
        assert simulator.hawk.get_state().status == "EMERGENCY_STOPPED"

    await fast_path.shutdown()
    await gateway.close_all()
    await vehicle_svc.stop()


# ---------------------------------------------------------------------------
# 5, 6, 7, 10 & 12: Bypass ULTRON/DB, Deterministic Ordering, Lockout & Latency
# ---------------------------------------------------------------------------


async def test_fast_path_bypasses_ultron_and_db_failures_and_preserves_order(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify fast path never calls ULTRON/send_command, dispatches before DB log, and survives DB errors."""
    (
        test_app,
        clock,
        agent,
        simulator,
        gateway,
        fast_path,
        _mission_svc,
        vehicle_svc,
    ) = _build_m4_app(isolated_engine, session_factory)

    async with session_factory() as session:
        await vehicle_svc.connect_vehicle(session, "SCOUT01")
        await vehicle_svc.connect_vehicle(session, "HAWK01")

    # 5. Poison ULTRON planning, World Model update hooks, and ordinary send_command
    # so that if emergency stop touches any of them, the test would fail immediately.
    async def _poison_ultron(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Emergency stop fast path must never invoke ULTRON agent methods!")

    async def _poison_send_command(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Emergency stop fast path must use trigger_emergency_stop, not send_command!")

    setattr(agent, "create_simulated_mission", _poison_ultron)
    setattr(agent, "update_vehicle_state", _poison_ultron)
    setattr(simulator.scout, "send_command", _poison_send_command)
    setattr(simulator.hawk, "send_command", _poison_send_command)

    # 7. Record deterministic execution trace to prove dispatch completes before DB logging starts
    trace_order: list[str] = []
    fast_path.set_order_trace_hook(trace_order.append)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post("/emergency-stop", json={"vehicle_id": "ALL"})
        assert res.status_code == 200
        assert res.json()["overall_outcome"] == "ALL_CONFIRMED_STOPPED"

    await fast_path.wait_for_pending_logs()
    fast_path.set_order_trace_hook(None)

    assert trace_order[:2] == ["dispatch:SCOUT01", "dispatch:HAWK01"]
    assert "db_log_start" in trace_order
    assert trace_order.index("dispatch:HAWK01") < trace_order.index("db_log_start")

    # 6. Inject a broken session_factory that always raises RuntimeError on DB access;
    # dispatch must still succeed without error!
    @asynccontextmanager
    async def _broken_session_factory() -> AsyncIterator[AsyncSession]:
        raise RuntimeError("Simulated SQLite storage failure")
        yield  # pragma: no cover

    test_app.state.session_factory = _broken_session_factory
    trace_broken_db: list[str] = []
    fast_path.set_order_trace_hook(trace_broken_db.append)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res_db_fail = await client.post("/emergency-stop", json={"vehicle_id": "ALL"})
        assert res_db_fail.status_code == 200
        body_db_fail = res_db_fail.json()
        assert body_db_fail["overall_outcome"] == "ALL_CONFIRMED_STOPPED"
        assert body_db_fail["all_confirmed_stopped"] is True

    await fast_path.wait_for_pending_logs()
    fast_path.set_order_trace_hook(None)
    assert "db_log_error" in trace_broken_db

    # 10. Verify LockedRealVehicleAdapter explicitly reports real_hardware_available=False
    locked_adapter = LockedRealVehicleAdapter("SCOUT01", "ground", clock=clock)
    real_outcome = await locked_adapter.trigger_emergency_stop()
    assert real_outcome.mode == "real"
    assert real_outcome.is_simulated is False
    assert real_outcome.real_hardware_available is False
    assert real_outcome.dispatch_attempted is False
    assert real_outcome.dispatch_succeeded is False
    assert real_outcome.vehicle_acknowledged is False
    assert real_outcome.vehicle_confirmed_stopped is False
    assert "physical vehicle kill switch" in real_outcome.reason.lower()

    # 12. Measure simulated backend fast-path latency across 10 runs (< 100ms target)
    test_app.state.session_factory = session_factory
    dispatch_durations_ms: list[float] = []
    http_roundtrip_ms: list[float] = []
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(10):
            t0 = time.perf_counter()
            r = await client.post("/emergency-stop", json={"vehicle_id": "ALL"})
            t1 = time.perf_counter()
            assert r.status_code == 200
            dispatch_durations_ms.append(float(r.json()["dispatch_elapsed_ms"]))
            http_roundtrip_ms.append((t1 - t0) * 1000.0)

    await fast_path.wait_for_pending_logs()
    assert max(dispatch_durations_ms) < 100.0
    assert max(http_roundtrip_ms) < 100.0

    await fast_path.shutdown()
    await gateway.close_all()
    await vehicle_svc.stop()
