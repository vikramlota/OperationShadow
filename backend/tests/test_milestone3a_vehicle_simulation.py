"""Milestone 3A automated tests: Vehicle abstraction, deterministic simulation, heartbeat timeout, and DB sync.

Covers:
1. VehicleAdapterPort protocol compliance and LockedRealVehicleAdapter safety lockout.
2. Deterministic SCOUT01 (ground) and HAWK01 (aerial) mock instances, starting positions, and battery drain.
3. Step-driven movement, telemetry generation, and ground-altitude validation.
4. Healthy heartbeats, expired heartbeat timeout -> UNKNOWN transition, disconnection, reconnection, and recovery.
5. Stale / out-of-order telemetry rejection (preventing false recovery from UNKNOWN).
6. Mock task-state transitions, structured observations, and command acknowledgements.
7. Configurable fault injection and invalid input rejection.
8. Sensible SQLite persistence boundary (throttled routine ticks vs. immediate state-transition writes).
9. Clean startup, periodic background task execution, and shutdown with zero leaked asyncio tasks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.config import Settings
from core.errors import HardwareLockoutError
from db.models import Mission, Observation, Vehicle
import pytest
from services.vehicle_service import VehicleService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ultron_stub import (
    LockedRealVehicleAdapter,
    ManualClock,
    MockFleetSimulator,
    MockVehicleAdapter,
    MockVehicleConfig,
    RealHardwareLockedError,
    StubUltronAgent,
    TelemetryPayloadStub,
    VehicleAdapterPort,
    VehicleCommandEnvelopeStub,
)


# ---------------------------------------------------------------------------
# 1. Vehicle Adapter Interface & Real-Hardware Safety Lockout
# ---------------------------------------------------------------------------


async def test_vehicle_adapter_protocol_and_real_hardware_lockout(
    db_session: AsyncSession,
) -> None:
    """Verify mock and locked real adapters implement VehicleAdapterPort and block real control."""
    clock = ManualClock()
    simulator = MockFleetSimulator(clock=clock)

    assert isinstance(simulator.scout, VehicleAdapterPort)
    assert isinstance(simulator.hawk, VehicleAdapterPort)

    locked_real = LockedRealVehicleAdapter("SCOUT01", "ground")
    assert isinstance(locked_real, VehicleAdapterPort)
    assert locked_real.mode == "real"
    assert locked_real.get_connection_state() == "DISCONNECTED"

    with pytest.raises(RealHardwareLockedError, match="physical hardware control is locked"):
        await locked_real.start()

    with pytest.raises(RealHardwareLockedError, match="physical hardware control is locked"):
        await locked_real.connect()

    cmd = VehicleCommandEnvelopeStub(
        command_id="CMD_REAL_1",
        vehicle_id="SCOUT01",
        command_type="MOVE_TO",
        parameters={"x": 1.0, "y": 1.0},
    )
    with pytest.raises(RealHardwareLockedError, match="physical hardware control is locked"):
        await locked_real.send_command(cmd)

    # VehicleService must also reject switching to 'real' when SHADOW_HARDWARE_LOCKOUT=true
    service = VehicleService(
        clock=clock,
        simulator=simulator,
        app_settings=Settings(hardware_lockout=True, default_vehicle_mode="simulated"),
    )
    with pytest.raises(HardwareLockoutError):
        await service.set_vehicle_mode(db_session, "SCOUT01", "real")


# ---------------------------------------------------------------------------
# 2. Both Mock Vehicles: Configurable Positions, Movement & Battery Drain
# ---------------------------------------------------------------------------


async def test_mock_vehicles_deterministic_movement_and_battery_drain() -> None:
    """Verify SCOUT01 and HAWK01 configurable start positions, step movement, and battery levels."""
    clock = ManualClock(datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc))
    scout_cfg = MockVehicleConfig(
        vehicle_id="SCOUT01",
        vehicle_type="ground",
        initial_x=1.0,
        initial_y=2.0,
        initial_alt=0.0,
        initial_battery=95.0,
        battery_drain_per_tick=0.5,
        default_step_distance=1.0,
    )
    hawk_cfg = MockVehicleConfig(
        vehicle_id="HAWK01",
        vehicle_type="aerial",
        initial_x=5.0,
        initial_y=5.0,
        initial_alt=2.0,
        initial_battery=90.0,
        battery_drain_per_tick=1.0,
        default_step_distance=1.0,
    )
    fleet = MockFleetSimulator(clock=clock, scout_config=scout_cfg, hawk_config=hawk_cfg)
    await fleet.start_all()

    scout_state = fleet.scout.get_state()
    hawk_state = fleet.hawk.get_state()
    assert scout_state.position_x == 1.0
    assert scout_state.position_y == 2.0
    assert scout_state.position_alt == 0.0
    assert scout_state.battery == 95.0
    assert hawk_state.position_alt == 2.0
    assert hawk_state.battery == 90.0

    # Ground vehicle SCOUT01 must reject non-zero altitude
    with pytest.raises(ValueError, match="Ground vehicle SCOUT01"):
        fleet.scout.set_target_position(3.0, 2.0, alt=1.5)
    with pytest.raises(ValueError, match="Ground vehicle SCOUT01"):
        fleet.scout.move_by(dalt=0.5)

    # Set valid targets: SCOUT01 moves 2.0m along X; HAWK01 moves 1.0m along Y and +1.0m alt
    fleet.scout.set_target_position(3.0, 2.0)
    fleet.hawk.set_target_position(5.0, 6.0, alt=3.0)

    clock.advance(milliseconds=250)
    tick1 = await fleet.step_all()
    assert tick1["SCOUT01"] is not None
    assert tick1["SCOUT01"].position_x == pytest.approx(2.0)
    assert tick1["SCOUT01"].battery == pytest.approx(94.5)
    assert tick1["HAWK01"] is not None
    assert tick1["HAWK01"].position_y == pytest.approx(6.0)
    assert tick1["HAWK01"].position_alt == pytest.approx(3.0)
    assert tick1["HAWK01"].battery == pytest.approx(89.0)

    clock.advance(milliseconds=250)
    tick2 = await fleet.step_all()
    assert tick2["SCOUT01"] is not None
    assert tick2["SCOUT01"].position_x == pytest.approx(3.0)
    assert tick2["SCOUT01"].status == "IDLE"
    assert tick2["SCOUT01"].battery == pytest.approx(94.0)

    await fleet.stop_all()


# ---------------------------------------------------------------------------
# 3. Heartbeat Timeout -> UNKNOWN, Disconnection, Reconnection & Stale Telemetry
# ---------------------------------------------------------------------------


async def test_heartbeat_timeout_unknown_recovery_and_stale_telemetry_rejection(
    db_session: AsyncSession,
) -> None:
    """Verify healthy heartbeats, timeout to UNKNOWN, stale rejection, and recovery."""
    clock = ManualClock(datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc))
    agent = StubUltronAgent()
    simulator = MockFleetSimulator(clock=clock)
    service = VehicleService(
        agent=agent,
        clock=clock,
        simulator=simulator,
        heartbeat_interval_ms=250,
        heartbeat_timeout_ms=1000,
        persist_every_n_heartbeats=3,
    )

    # 1. Connect both vehicles at t = 0ms
    scout_read = await service.connect_vehicle(db_session, "SCOUT01")
    hawk_read = await service.connect_vehicle(db_session, "HAWK01")
    assert scout_read.connection.value == "CONNECTED"
    assert hawk_read.connection.value == "CONNECTED"

    # 2. Advance 400ms (within 1000ms timeout) and emit healthy heartbeats
    clock.advance(milliseconds=400)
    telem_frames = await simulator.step_all()
    assert telem_frames["SCOUT01"] is not None
    assert telem_frames["HAWK01"] is not None

    res_scout = await service.ingest_telemetry(db_session, telem_frames["SCOUT01"])
    res_hawk = await service.ingest_telemetry(db_session, telem_frames["HAWK01"])
    assert res_scout.accepted is True
    assert res_hawk.accepted is True
    assert await service.evaluate_heartbeats(db_session) == []

    # 3. Suppress SCOUT01 heartbeats and advance clock by 1100ms (> 1000ms threshold)
    simulator.scout.configure_faults(suppress_heartbeats=True)
    clock.advance(milliseconds=600)
    # HAWK01 sends a heartbeat at t = 1000ms; SCOUT01 does not
    hawk_beat = await simulator.hawk.step()
    assert hawk_beat is not None
    await service.ingest_telemetry(db_session, hawk_beat, force_persist=True)

    clock.advance(milliseconds=500)  # Now t = 1500ms; SCOUT01 last_heartbeat was at t = 400ms (age = 1100ms)
    hawk_beat_2 = await simulator.hawk.step()
    assert hawk_beat_2 is not None
    await service.ingest_telemetry(db_session, hawk_beat_2, force_persist=True)

    timed_out = await service.evaluate_heartbeats(db_session)
    assert timed_out == ["SCOUT01"]
    assert simulator.scout.get_connection_state() == "UNKNOWN"
    assert simulator.scout.get_state().status == "UNKNOWN"
    assert len(agent.unknown_transitions) == 1
    assert agent.unknown_transitions[0][0] == "SCOUT01"

    db_scout = (
        await db_session.execute(select(Vehicle).where(Vehicle.id == "SCOUT01"))
    ).scalar_one()
    assert db_scout.connection == "UNKNOWN"
    assert db_scout.status == "UNKNOWN"

    # 4. Stale telemetry must NOT recover SCOUT01 from UNKNOWN or overwrite state
    stale_older_than_last = TelemetryPayloadStub(
        vehicle_id="SCOUT01",
        battery=50.0,
        position_x=99.0,
        position_y=99.0,
        position_alt=0.0,
        status="ACTIVE",
        timestamp=datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc),  # t = 0ms < 400ms
    )
    stale_res_1 = await service.ingest_telemetry(db_session, stale_older_than_last)
    assert stale_res_1.accepted is False
    assert stale_res_1.reason == "stale_older_than_last_heartbeat"

    stale_expired_window = TelemetryPayloadStub(
        vehicle_id="SCOUT01",
        battery=50.0,
        position_x=88.0,
        position_y=88.0,
        position_alt=0.0,
        status="ACTIVE",
        timestamp=datetime(2026, 10, 28, 12, 0, 0, 450000, tzinfo=timezone.utc),  # t = 450ms > 400ms, age 1050ms > 1000ms timeout
    )
    stale_res_2 = await service.ingest_telemetry(db_session, stale_expired_window)
    assert stale_res_2.accepted is False
    assert stale_res_2.reason == "stale_exceeded_timeout_window"

    # Confirm SCOUT01 is still UNKNOWN in DB
    await db_session.refresh(db_scout)
    assert db_scout.connection == "UNKNOWN"

    # 5. Reconnect & recover SCOUT01 with a fresh heartbeat
    recovered = await service.reconnect_vehicle(db_session, "SCOUT01")
    assert recovered.connection.value == "CONNECTED"
    assert recovered.status.value == "IDLE"

    fresh_beat = await simulator.scout.step()
    assert fresh_beat is not None
    recovery_ingest = await service.ingest_telemetry(db_session, fresh_beat)
    assert recovery_ingest.accepted is True
    assert recovery_ingest.connection == "CONNECTED"

    # 6. Explicit disconnect and reconnect
    disconnected = await service.disconnect_vehicle(db_session, "HAWK01")
    assert disconnected.connection.value == "DISCONNECTED"
    assert await simulator.hawk.step() is None

    reconnected = await service.reconnect_vehicle(db_session, "HAWK01")
    assert reconnected.connection.value == "CONNECTED"


# ---------------------------------------------------------------------------
# 4. Task Transitions, Observations, Command Acknowledgements & Fault Injection
# ---------------------------------------------------------------------------


async def test_task_transitions_observations_commands_and_fault_injection(
    db_session: AsyncSession,
) -> None:
    """Verify mock task transitions, observations persisted to DB, command acks, and faults."""
    clock = ManualClock()
    agent = StubUltronAgent()
    simulator = MockFleetSimulator(clock=clock)
    service = VehicleService(agent=agent, clock=clock, simulator=simulator)

    await service.connect_vehicle(db_session, "SCOUT01")
    await service.connect_vehicle(db_session, "HAWK01")

    # Task state transitions on SCOUT01
    state_assigned = simulator.scout.transition_task_state("T100", "ASSIGNED")
    assert state_assigned.status == "ACTIVE"
    assert state_assigned.active_task_id == "T100"

    state_exec = simulator.scout.transition_task_state("T100", "IN_PROGRESS")
    assert state_exec.status == "EXECUTING"

    state_done = simulator.scout.transition_task_state("T100", "COMPLETED")
    assert state_done.status == "IDLE"
    assert state_done.active_task_id is None

    # Create a mission row so observation FK to missions.id is satisfied
    mission = Mission(
        id="M_SIM_01",
        objective="Verify simulated observation persistence",
        status="ACTIVE",
    )
    db_session.add(mission)
    await db_session.commit()

    obs_payload = await simulator.hawk.emit_observation(
        observation_id="OBS_SIM_01",
        mission_id="M_SIM_01",
        obs_type="thermal_hotspot",
        confidence=0.92,
        location_x=4.5,
        location_y=7.2,
        raw={"temp_c": 41.8},
    )
    obs_read = await service.record_observation(db_session, obs_payload)
    assert obs_read.id == "OBS_SIM_01"
    assert obs_read.confidence == pytest.approx(0.92)

    persisted_obs = (
        await db_session.execute(
            select(Observation).where(Observation.id == "OBS_SIM_01")
        )
    ).scalar_one()
    assert persisted_obs.raw == {"temp_c": 41.8}

    snapshot = await agent.get_world_model_snapshot()
    assert len(snapshot.observations) == 1
    assert snapshot.observations[0]["id"] == "OBS_SIM_01"

    # Valid command acknowledgement
    move_cmd = VehicleCommandEnvelopeStub(
        command_id="CMD_SIM_01",
        vehicle_id="SCOUT01",
        command_type="GOTO_WAYPOINT",
        parameters={"x": 3.0, "y": 4.0},
    )
    ack_ok = await service.dispatch_command(db_session, move_cmd)
    assert ack_ok.status == "ACCEPTED"
    assert ack_ok.is_simulated is True

    # Fault injection: command rejection
    simulator.scout.configure_faults(reject_commands=True)
    ack_rejected = await service.dispatch_command(db_session, move_cmd)
    assert ack_rejected.status == "REJECTED"
    simulator.scout.configure_faults(reject_commands=False)

    # Emergency stop command transitions vehicle to EMERGENCY_STOPPED and syncs to DB
    estop_cmd = VehicleCommandEnvelopeStub(
        command_id="CMD_ESTOP_01",
        vehicle_id="SCOUT01",
        command_type="EMERGENCY_STOP",
    )
    ack_estop = await service.dispatch_command(db_session, estop_cmd)
    assert ack_estop.status == "ACCEPTED"
    db_scout = (
        await db_session.execute(select(Vehicle).where(Vehicle.id == "SCOUT01"))
    ).scalar_one()
    assert db_scout.status == "EMERGENCY_STOPPED"

    # Fault injection: disconnect on command returns FAILED
    simulator.hawk.configure_faults(disconnect_on_command=True)
    hawk_cmd = VehicleCommandEnvelopeStub(
        command_id="CMD_HAWK_01",
        vehicle_id="HAWK01",
        command_type="HOVER",
    )
    ack_failed = await service.dispatch_command(db_session, hawk_cmd)
    assert ack_failed.status == "FAILED"
    assert simulator.hawk.get_connection_state() == "DISCONNECTED"

    # Reject arbitrary free-form text in VehicleCommandEnvelopeStub (§5.1)
    with pytest.raises(ValueError, match="arbitrary free-form text is prohibited"):
        VehicleCommandEnvelopeStub(
            command_id="CMD_BAD_TEXT",
            vehicle_id="SCOUT01",
            command_type="fly north quickly",
        )


# ---------------------------------------------------------------------------
# 5. SQLite Persistence Boundary & Background Task Startup/Shutdown Cleanup
# ---------------------------------------------------------------------------


async def test_persistence_boundary_throttling_and_background_task_cleanup(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify routine heartbeats throttle DB writes while keeping World Model live, and tasks clean up."""
    clock = ManualClock()
    agent = StubUltronAgent()
    simulator = MockFleetSimulator(clock=clock)
    service = VehicleService(
        agent=agent,
        clock=clock,
        simulator=simulator,
        persist_every_n_heartbeats=3,
    )

    await service.connect_vehicle(db_session, "SCOUT01")
    simulator.scout.set_target_position(10.0, 0.0)

    # Tick 1: status transitions from IDLE -> ACTIVE, so it MUST persist immediately
    clock.advance(milliseconds=250)
    beat1 = await simulator.scout.step()
    assert beat1 is not None
    res1 = await service.ingest_telemetry(db_session, beat1)
    assert res1.persisted_to_db is True

    # Tick 2: status remains ACTIVE (unpersisted count = 1 < 3) -> World Model updates, DB write skipped
    clock.advance(milliseconds=250)
    beat2 = await simulator.scout.step()
    assert beat2 is not None
    res2 = await service.ingest_telemetry(db_session, beat2)
    assert res2.accepted is True
    assert res2.persisted_to_db is False

    # World Model has the latest position from beat2 immediately
    wm_snapshot = await agent.get_world_model_snapshot()
    assert wm_snapshot.vehicles["SCOUT01"]["position_x"] == pytest.approx(beat2.position_x)

    # Tick 3 (count = 2 < 3) -> skipped; Tick 4 (count = 3 == 3) -> persisted checkpoint
    clock.advance(milliseconds=250)
    beat3 = await simulator.scout.step()
    assert beat3 is not None
    res3 = await service.ingest_telemetry(db_session, beat3)
    assert res3.persisted_to_db is False

    clock.advance(milliseconds=250)
    beat4 = await simulator.scout.step()
    assert beat4 is not None
    res4 = await service.ingest_telemetry(db_session, beat4)
    assert res4.persisted_to_db is True

    db_scout = (
        await db_session.execute(select(Vehicle).where(Vehicle.id == "SCOUT01"))
    ).scalar_one()
    assert db_scout.position_x == pytest.approx(beat4.position_x)

    # Verify background periodic telemetry and heartbeat monitor start and stop without leaked tasks
    await simulator.scout.start_periodic_telemetry(interval_ms=20)
    await service.start_heartbeat_monitor(session_factory, check_interval_ms=20)
    assert simulator.scout.has_background_task is True
    assert service.has_monitor_task is True

    await service.stop()
    assert simulator.scout.has_background_task is False
    assert simulator.hawk.has_background_task is False
    assert service.has_monitor_task is False
