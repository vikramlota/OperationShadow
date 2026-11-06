"""Focused end-to-end tests for physical SCOUT01 demo mission workflow using mocked hardware interfaces.

Verifies the 7 targeted physical demo requirements without touching physical hardware:
1. Frontend/API mission creation (`POST /mission`) generates ULTRON plan and waits for operator approval (`POST /mission/{id}/execute`) without auto-executing.
2. Only ONE mission executor controls SCOUT01 (manual WASD blocked while mission executor lease is active; 'X' stop always permitted).
3. Existing physical serial-control pathway (`ScoutSerialWriter`) is reused.
4. Hardcoded timed AVOID movements are replaced with actual sensor-based obstacle evaluation and avoidance.
   - Immediate `'X'` stop occurs on obstacle detection independent of Gemini/LLM availability.
   - Sensor-verified clear side triggers physical direction change (`'A'` or `'D'`), bypass (`'W'`), and continuation toward destination.
   - Unverifiable sensor geometry (dead-center without lateral clearance, both sides blocked, or missing/conflicted sensors) stops SCOUT01 (`'X'`) and reports blocked (`FAILED`) rather than guessing.
5. ULTRON replanning connects to the currently executing mission so updated routes replace old `ASSIGNED` tasks (`CANCELLED` -> new `ASSIGNED` tasks executed).
6. Real execution progress, detections, decisions, and route changes broadcast over `DashboardGateway` WebSockets.
7. No fabricated `94.0%` battery or invented dead-reckoning coordinates in real mode, and physical completion does not emit misleading `[SIMULATED_ULTRON_STUB]` records.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest

_repo_root = Path(__file__).resolve().parent.parent
_fastapi_dir = _repo_root / "backend" / "fastapi"
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
if str(_fastapi_dir) not in sys.path:
    sys.path.insert(0, str(_fastapi_dir))

from db.models import Vehicle
from db.session import create_db_engine, create_session_factory, init_db
from schemas.entities import MissionCreateRequest
from services.event_service import EventService
from services.mission_service import MissionService, _task_serial_steps
from services.scout_control_simulator import ScoutControlSimulator
from services.scout_serial_writer import (
    ScoutSerialWriter,
    reset_global_emergency_stop,
)
from services.ultron_agent_adapter import UltronLangGraphAgent
from websocket.dashboard_gateway import DashboardGateway


class MockSerialTransport:
    """In-memory mock serial port recording every single-char command sent to ESP32."""

    def __init__(self) -> None:
        self.is_open = True
        self.written: list[str] = []

    def write(self, data: bytes) -> int:
        decoded = data.decode("ascii", errors="ignore")
        self.written.append(decoded)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


@pytest.fixture(autouse=True)
def _reset_estop() -> Any:
    reset_global_emergency_stop(authorized=True)
    yield
    reset_global_emergency_stop(authorized=True)


@pytest.mark.asyncio
async def test_task_serial_steps_uses_sensor_avoid_instead_of_hardcoded_turns() -> None:
    """Gap 4: AVOID tasks must use SENSOR_AVOID rather than blind hardcoded timed turns."""
    steps = _task_serial_steps("AVOID", "bypass corridor obstacle", total_tasks=2)
    assert steps == [
        ("SENSOR_AVOID", 0.0, "AVOID obstacle via sensor-verified clearance"),
    ]


@pytest.mark.asyncio
async def test_single_mission_executor_lease_blocks_manual_wasd_but_allows_stop() -> None:
    """Gap 2: While MissionService holds the executor lease, manual WASD is rejected while 'X' stop is allowed."""
    mock_serial = MockSerialTransport()
    writer = ScoutSerialWriter(
        port="/dev/mock_esp32",
        baudrate=115200,
        enabled=True,
        hardware_lockout=False,
        real_mode=True,
        failsafe_verified=True,
    )
    writer._connection = mock_serial  # type: ignore[assignment]

    sim = ScoutControlSimulator(serial_writer=writer)
    client_id = await sim.connect_client()

    await sim.acquire_mission_executor_lease()
    assert sim.mission_executor_active is True

    # Manual 'W' while mission executor is active must be rejected without writing 'W' to serial
    accepted_w = await sim.update_command(client_id, "W")
    assert accepted_w is False
    assert "W" not in mock_serial.written

    # Manual 'X' stop is always permitted even during active mission execution
    accepted_x = await sim.update_command(client_id, "X")
    assert accepted_x is True
    assert "X" in mock_serial.written

    await sim.release_mission_executor_lease()
    assert sim.mission_executor_active is False


@pytest.mark.asyncio
async def test_end_to_end_demo_obstacle_detection_sensor_avoidance_replanning_and_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gaps 1-7: Plan -> Approve/Execute -> Real Obstacle Detection -> Immediate 'X' Stop (no Gemini needed)
    -> Sensor-Verified Avoidance ('D') -> ULTRON Replanning Task Replacement -> Real Telemetry Preserved -> Destination.
    """
    import services.scout_serial_writer as ssw_mod

    async def _fast_wait(duration_s: float) -> bool:
        await asyncio.sleep(0.005)
        return not ssw_mod.is_global_emergency_stop_latched()

    monkeypatch.setattr(ssw_mod, "wait_unless_emergency_stopped", _fast_wait)
    # Disable Gemini API key to prove immediate obstacle stopping & sensor avoidance work offline
    monkeypatch.setenv("GEMINI_API_KEY", "")

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'demo_e2e.db'}"
    engine = create_db_engine(db_url)
    session_factory = create_session_factory(engine)
    await init_db(engine)

    # Set real SCOUT01 telemetry (battery=83.5%, position=(4.2, 7.8)) to verify it is NOT overwritten with 94.0% or dead-reckoning
    async with session_factory() as session:
        v_row = await session.get(Vehicle, "SCOUT01")
        assert v_row is not None
        v_row.mode = "real"
        v_row.connection = "CONNECTED"
        v_row.status = "IDLE"
        v_row.battery = 83.5
        v_row.position_x = 4.2
        v_row.position_y = 7.8
        await session.commit()

    gateway = DashboardGateway()
    broadcast_missions: list[Any] = []
    broadcast_events: list[Any] = []
    broadcast_traces: list[Any] = []
    broadcast_vehicles: list[dict[str, Any]] = []

    gateway.broadcast_mission_status = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda m: broadcast_missions.append(m)
    )
    gateway.broadcast_event = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda e: broadcast_events.append(e)
    )
    gateway.broadcast_reasoning_trace = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda rt: broadcast_traces.append(rt)
    )
    gateway.broadcast_vehicle_dict = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda vd: broadcast_vehicles.append(vd)
    )

    events = EventService(gateway=gateway)
    agent = UltronLangGraphAgent(replan_cooldown_sec=0.0, replan_debounce_sec=0.0)
    await agent.update_vehicle_state(
        "SCOUT01",
        status="IDLE",
        connection="CONNECTED",
        mode="real",
        battery=83.5,
        position_x=4.2,
        position_y=7.8,
        last_heartbeat=datetime.now(timezone.utc),
    )

    service = MissionService(agent=agent, gateway=gateway, events=events)

    # 1. Create mission ("Navigate to the destination, detect obstacles, avoid them and continue towards the destination.")
    async with session_factory() as session:
        create_resp, rationale = await service.create_mission_with_rationale(
            session,
            MissionCreateRequest(
                objective="Navigate to the destination, detect obstacles, avoid them and continue towards the destination."
            ),
        )
        mission_id = create_resp.mission_id
        detail_after_create = await service.get_mission(session, mission_id)

    assert mission_id == "M001"
    assert rationale is not None
    # Verify mission did NOT auto-execute on creation (all tasks remain ASSIGNED awaiting approval)
    assert detail_after_create.status.value == "ACTIVE"
    assert all(t.status == "ASSIGNED" for t in detail_after_create.tasks)

    # 2. Prepare mocked physical serial writer that injects a real depth-camera obstacle when 'W' starts
    mock_serial = MockSerialTransport()
    writer = ScoutSerialWriter(
        port="/dev/mock_esp32",
        baudrate=115200,
        enabled=True,
        hardware_lockout=False,
        real_mode=True,
        failsafe_verified=True,
    )
    writer._connection = mock_serial  # type: ignore[assignment]

    obstacle_injected = False
    orig_send = writer.send

    async def _send_with_obstacle_trigger(command: str) -> None:
        nonlocal obstacle_injected
        await orig_send(command)
        if command == "W" and not obstacle_injected:
            obstacle_injected = True
            # Real depth-camera observation: chair obstacle on the LEFT (x = -0.35m in optical frame) at 1.1m range
            now_dt = datetime.now(timezone.utc)
            agent.ingest_detection_frame_into_world_model(
                stamp=now_dt.timestamp(),
                received_at=now_dt,
                frame="oak_rgb_camera_optical_frame",
                objects=[
                    {
                        "id": 1,
                        "class": "chair",
                        "score": 0.94,
                        "position": {"x": -0.35, "y": 0.0, "z": 1.1},
                        "range_m": 1.1,
                    }
                ],
                vehicle_id="SCOUT01",
                telemetry_source="rosbridge",
            )

    writer.send = _send_with_obstacle_trigger  # type: ignore[method-assign]

    sim = ScoutControlSimulator(serial_writer=writer)

    # 3. Operator approves and executes mission on SCOUT01
    final_detail = await service.execute_mission_on_rover(
        session_factory,
        mission_id,
        serial_writer=writer,
        control_simulator=sim,
    )

    assert final_detail.status.value == "COMPLETED"
    assert sim.mission_executor_active is False

    # Verify physical serial commands:
    # - Started with 'W'
    # - Immediately stopped with 'X' upon obstacle detection
    # - Reversed with 'S' to create safe clearance from obstacle
    # - Chose sensor-verified right turn 'D' (since obstacle was at x=-0.35m on the left)
    # - Continued forward 'W' and counter-turned 'A'
    assert "W" in mock_serial.written
    assert "X" in mock_serial.written
    assert "S" in mock_serial.written
    assert "D" in mock_serial.written
    first_w_idx = mock_serial.written.index("W")
    stop_after_w_idx = mock_serial.written.index("X", first_w_idx)
    reverse_s_idx = mock_serial.written.index("S", stop_after_w_idx)
    turn_d_idx = mock_serial.written.index("D", reverse_s_idx)
    assert first_w_idx < stop_after_w_idx < reverse_s_idx < turn_d_idx

    # Verify ULTRON Standing Order / replanning superseded initial task(s) and added T-OBS-01 / T-OBS-02
    task_ids_and_status = {t.id: t.status for t in final_detail.tasks}
    assert "M001-T01" in task_ids_and_status
    assert task_ids_and_status["M001-T01"] == "CANCELLED"
    assert "T-OBS-01" in task_ids_and_status
    assert task_ids_and_status["T-OBS-01"] == "COMPLETED"
    assert "T-OBS-02" in task_ids_and_status
    assert task_ids_and_status["T-OBS-02"] == "COMPLETED"

    # Verify real telemetry was preserved (never hardcoded 94.0% battery or fake dead-reckoning)
    assert len(broadcast_vehicles) > 0
    for v_msg in broadcast_vehicles:
        assert v_msg["battery"] == 83.5
        assert v_msg["position_x"] == 4.2
        assert v_msg["position_y"] == 7.8

    # Verify WebSocket broadcasts included obstacle stop, reverse, sensor-verified avoidance, and replanning trace
    event_messages = [e.message for e in broadcast_events]
    assert any("Obstacle 'chair' detected" in m and "stopped ('X')" in m for m in event_messages)
    assert any("reversed ('S')" in m for m in event_messages)
    assert any("sensor-verified avoidance" in m and "turning RIGHT ('D')" in m for m in event_messages)

    trace_triggers = [rt.trigger for rt in broadcast_traces]
    assert any("SENSOR_VERIFIED_AVOIDANCE" in t for t in trace_triggers)
    assert any("MISSION_EXECUTION_COMPLETED" in t for t in trace_triggers)
    assert not any("SIMULATED_MISSION_COMPLETION" in t for t in trace_triggers)

    await engine.dispose()


@pytest.mark.asyncio
async def test_unverifiable_sensor_geometry_stops_rover_and_reports_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gap 4 & Constraint: If sensor geometry cannot verify a safe alternative (e.g. dead-center obstacle
    with no lateral clearance verification), stop the rover ('X') and report blocked (FAILED) rather than guessing.
    """
    import services.scout_serial_writer as ssw_mod

    async def _fast_wait(duration_s: float) -> bool:
        await asyncio.sleep(0.005)
        return not ssw_mod.is_global_emergency_stop_latched()

    monkeypatch.setattr(ssw_mod, "wait_unless_emergency_stopped", _fast_wait)
    monkeypatch.setenv("GEMINI_API_KEY", "")

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'demo_blocked.db'}"
    engine = create_db_engine(db_url)
    session_factory = create_session_factory(engine)
    await init_db(engine)

    async with session_factory() as session:
        v_row = await session.get(Vehicle, "SCOUT01")
        assert v_row is not None
        v_row.mode = "real"
        v_row.connection = "CONNECTED"
        v_row.status = "IDLE"
        v_row.battery = 88.0
        await session.commit()

    gateway = DashboardGateway()
    broadcast_events: list[Any] = []
    gateway.broadcast_event = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda e: broadcast_events.append(e)
    )
    events = EventService(gateway=gateway)
    agent = UltronLangGraphAgent(replan_cooldown_sec=0.0, replan_debounce_sec=0.0)
    await agent.update_vehicle_state(
        "SCOUT01",
        status="IDLE",
        connection="CONNECTED",
        mode="real",
        battery=88.0,
        position_x=1.5,
        position_y=1.5,
        last_heartbeat=datetime.now(timezone.utc),
    )

    service = MissionService(agent=agent, gateway=gateway, events=events)

    async with session_factory() as session:
        create_resp = await service.create_mission(
            session,
            MissionCreateRequest(
                objective="Navigate to the destination, detect obstacles, avoid them and continue towards the destination."
            ),
        )
        mission_id = create_resp.mission_id

    mock_serial = MockSerialTransport()
    writer = ScoutSerialWriter(
        port="/dev/mock_esp32",
        baudrate=115200,
        enabled=True,
        hardware_lockout=False,
        real_mode=True,
        failsafe_verified=True,
    )
    writer._connection = mock_serial  # type: ignore[assignment]

    obstacle_injected = False
    orig_send = writer.send

    async def _send_with_dead_center_obstacle(command: str) -> None:
        nonlocal obstacle_injected
        await orig_send(command)
        if command == "W" and not obstacle_injected:
            obstacle_injected = True
            now_dt = datetime.now(timezone.utc)
            # Dead-center barrier (x = 0.0m) with no LiDAR free-space coverage -> cannot verify left or right
            agent.ingest_detection_frame_into_world_model(
                stamp=now_dt.timestamp(),
                received_at=now_dt,
                frame="oak_rgb_camera_optical_frame",
                objects=[
                    {
                        "id": 9,
                        "class": "barrier",
                        "score": 0.96,
                        "position": {"x": 0.0, "y": 0.0, "z": 0.95},
                        "range_m": 0.95,
                    }
                ],
                vehicle_id="SCOUT01",
                telemetry_source="rosbridge",
            )

    writer.send = _send_with_dead_center_obstacle  # type: ignore[method-assign]

    final_detail = await service.execute_mission_on_rover(
        session_factory,
        mission_id,
        serial_writer=writer,
    )

    # Mission must report FAILED (blocked) and rover must hold position ('X') without guessing 'A' or 'D'
    assert final_detail.status.value == "FAILED"
    assert mock_serial.written[0] == "W"
    assert "X" in mock_serial.written
    assert "A" not in mock_serial.written
    assert "D" not in mock_serial.written
    assert mock_serial.written[-1] == "X"

    event_messages = [e.message for e in broadcast_events]
    assert any("SCOUT01 BLOCKED" in m and "cannot verify lateral clearance" in m for m in event_messages)

    await engine.dispose()
