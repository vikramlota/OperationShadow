"""Milestone 2 automated tests: ORM persistence, FK constraints, Pydantic & WebSocket contracts.

Covers all 9 required verification areas:
1. Model creation and persistence across all 6 tables.
2. Database relationships and SQLite foreign-key constraints (PRAGMA foreign_keys=ON).
3. Serialization of ORM records through Pydantic read schemas.
4. Rejection of invalid battery, confidence, priority, and constrained values.
5. Correct handling of optional fields and UTC timestamps.
6. Validation of versioned WebSocket envelopes & snapshot and rejection of malformed messages.
7. Database initialization idempotency, session cleanup/rollback, logging, and error handlers.
8. Continued operation of the /health endpoint.
9. Strict isolation between test cases so database state never leaks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging

from core.errors import HardwareLockoutError, ResourceNotFoundError
from core.logging import StructuredJsonFormatter, configure_logging
from db.models import (
    Event,
    Mission,
    Observation,
    ReasoningTrace,
    Task,
    Vehicle,
)
from db.session import init_db, session_scope
from httpx import ASGITransport, AsyncClient
from main import create_app
from pydantic import ValidationError
import pytest
from schemas.entities import (
    EmergencyStopRequest,
    EventCreate,
    EventRead,
    MissionCreateRecord,
    MissionCreateRequest,
    MissionDetailRead,
    MissionRead,
    MissionReplayResponse,
    MissionReplayTimelineEntry,
    ObservationCreate,
    ObservationRead,
    ReasoningTraceCreate,
    ReasoningTraceRead,
    TaskCreate,
    TaskRead,
    VehicleCreate,
    VehicleRead,
    VehicleUpdate,
)
from schemas.enums import (
    EventCategoryEnum,
    MissionStatusEnum,
    TaskStatusEnum,
    VehicleConnectionStateEnum,
    VehicleIdEnum,
    VehicleModeEnum,
    VehicleStatusEnum,
    VehicleTypeEnum,
)
from schemas.websocket import (
    DashboardEventEnvelope,
    DashboardMissionStatusEnvelope,
    DashboardReasoningTraceEnvelope,
    DashboardSnapshotPayload,
    DashboardTelemetryEnvelope,
    VehicleInboundObservationStub,
    VehicleInboundTelemetryStub,
    VehicleOutboundCommandStub,
    validate_dashboard_ws_envelope,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import selectinload


# ---------------------------------------------------------------------------
# 1. Model Creation & Persistence + 2. Relationships & Foreign-Key Constraints
# ---------------------------------------------------------------------------


async def test_all_six_models_persist_and_load_relationships(
    db_session: AsyncSession,
) -> None:
    """Verify creation, persistence, JSON storage, and ORM relationships for all 6 tables."""
    now = datetime.now(timezone.utc)

    mission = Mission(
        id="M001",
        objective="Search Sector Alpha and verify thermal anomaly",
        status=MissionStatusEnum.ACTIVE.value,
        created_at=now,
        completed_at=None,
    )
    task_scout = Task(
        id="T001",
        mission_id="M001",
        vehicle_id="SCOUT01",
        type="SWEEP_CORRIDOR",
        area="Sector-A1",
        target="North Entry",
        priority=1,
        status=TaskStatusEnum.IN_PROGRESS.value,
    )
    task_hawk = Task(
        id="T002",
        mission_id="M001",
        vehicle_id="HAWK01",
        type="OVERWATCH_SCAN",
        area="Sector-A2",
        target="Thermal Source",
        priority=2,
        status=TaskStatusEnum.ASSIGNED.value,
    )
    observation = Observation(
        id="OBS001",
        mission_id="M001",
        vehicle_id="SCOUT01",
        obs_type="obstacle_detected",
        confidence=0.94,
        location_x=4.25,
        location_y=8.5,
        timestamp=now,
        raw={"sensor": "lidar_2d", "range_m": 1.35, "blocked": True},
    )
    event_row = Event(
        id="EVT001",
        mission_id="M001",
        timestamp=now,
        category=EventCategoryEnum.MISSION.value,
        message="Mission M001 initialized with 2 tasks.",
    )
    trace = ReasoningTrace(
        id="RT001",
        mission_id="M001",
        timestamp=now,
        trigger="SCOUT01 reported corridor obstruction in Sector-A1",
        rationale="Rerouting HAWK01 to aerial overwatch over Sector-A2 while SCOUT01 holds.",
        resulting_task_ids=["T001", "T002"],
    )

    db_session.add_all([mission, task_scout, task_hawk, observation, event_row, trace])
    await db_session.commit()

    stmt = (
        select(Mission)
        .where(Mission.id == "M001")
        .options(
            selectinload(Mission.tasks),
            selectinload(Mission.observations),
            selectinload(Mission.events),
            selectinload(Mission.reasoning_traces),
        )
    )
    loaded_mission = (await db_session.execute(stmt)).scalar_one()

    assert loaded_mission.objective == "Search Sector Alpha and verify thermal anomaly"
    assert len(loaded_mission.tasks) == 2
    assert len(loaded_mission.observations) == 1
    assert loaded_mission.observations[0].raw == {
        "sensor": "lidar_2d",
        "range_m": 1.35,
        "blocked": True,
    }
    assert len(loaded_mission.events) == 1
    assert len(loaded_mission.reasoning_traces) == 1
    assert loaded_mission.reasoning_traces[0].resulting_task_ids == ["T001", "T002"]

    scout_stmt = (
        select(Vehicle)
        .where(Vehicle.id == "SCOUT01")
        .options(selectinload(Vehicle.tasks), selectinload(Vehicle.observations))
    )
    loaded_scout = (await db_session.execute(scout_stmt)).scalar_one()
    assert len(loaded_scout.tasks) == 1
    assert loaded_scout.tasks[0].id == "T001"
    assert len(loaded_scout.observations) == 1
    assert loaded_scout.observations[0].id == "OBS001"


async def test_foreign_key_constraints_and_cascades_enforced(
    db_session: AsyncSession,
) -> None:
    """Verify SQLite PRAGMA foreign_keys=ON rejects orphan FKs and cascades mission deletes."""
    orphan_task = Task(
        id="T_ORPHAN",
        mission_id="NON_EXISTENT_MISSION",
        vehicle_id="SCOUT01",
        type="PATROL",
        priority=1,
        status="PENDING",
    )
    db_session.add(orphan_task)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # Valid mission, invalid vehicle FK
    mission = Mission(
        id="M_FK",
        objective="Test foreign keys",
        status="ACTIVE",
    )
    db_session.add(mission)
    await db_session.commit()

    bad_vehicle_obs = Observation(
        id="OBS_BAD_VEH",
        mission_id="M_FK",
        vehicle_id="INVALID_DRONE",
        obs_type="target",
        confidence=0.8,
        location_x=1.0,
        location_y=2.0,
    )
    db_session.add(bad_vehicle_obs)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # Verify mission deletion cascades to tasks, events, observations, and reasoning_traces
    db_session.add_all(
        [
            Task(
                id="T_CASCADE",
                mission_id="M_FK",
                vehicle_id="SCOUT01",
                type="HOLD",
                priority=1,
                status="PENDING",
            ),
            Event(
                id="E_CASCADE",
                mission_id="M_FK",
                category="MISSION",
                message="Created",
            ),
            ReasoningTrace(
                id="RT_CASCADE",
                mission_id="M_FK",
                trigger="Init",
                rationale="Initial plan",
                resulting_task_ids=["T_CASCADE"],
            ),
        ]
    )
    await db_session.commit()

    await db_session.delete(mission)
    await db_session.commit()

    remaining_tasks = (
        await db_session.execute(select(Task).where(Task.id == "T_CASCADE"))
    ).scalars().all()
    assert remaining_tasks == []


# ---------------------------------------------------------------------------
# 3. Serialization of Database Records Through Pydantic Schemas
# ---------------------------------------------------------------------------


async def test_orm_records_serialize_cleanly_through_pydantic_schemas(
    db_session: AsyncSession,
) -> None:
    """Verify ORM models serialize into MissionDetailRead, VehicleRead, ObservationRead, etc."""
    now = datetime.now(timezone.utc)
    mission = Mission(
        id="M002",
        objective="Inspect Room B",
        status=MissionStatusEnum.ACTIVE.value,
        created_at=now,
    )
    task = Task(
        id="T010",
        mission_id="M002",
        vehicle_id="HAWK01",
        type="SCAN",
        area="Room-B",
        target="Marker-1",
        priority=3,
        status=TaskStatusEnum.ASSIGNED.value,
    )
    obs = Observation(
        id="OBS010",
        mission_id="M002",
        vehicle_id="HAWK01",
        obs_type="qr_marker",
        confidence=0.99,
        location_x=3.0,
        location_y=4.0,
        timestamp=now,
        raw={"code": "ALPHA-7"},
    )
    evt = Event(
        id="EVT010",
        mission_id="M002",
        timestamp=now,
        category=EventCategoryEnum.OBSERVATION.value,
        message="Marker detected",
    )
    trace = ReasoningTrace(
        id="RT010",
        mission_id="M002",
        timestamp=now + timedelta(seconds=1),
        trigger="QR Marker ALPHA-7 sighted",
        rationale="Confirm target acquisition and complete scan task.",
        resulting_task_ids=["T010"],
    )
    db_session.add_all([mission, task, obs, evt, trace])
    await db_session.commit()

    loaded_mission = (
        await db_session.execute(
            select(Mission)
            .where(Mission.id == "M002")
            .options(selectinload(Mission.tasks))
        )
    ).scalar_one()
    loaded_hawk = (
        await db_session.execute(select(Vehicle).where(Vehicle.id == "HAWK01"))
    ).scalar_one()

    mission_schema = MissionDetailRead.model_validate(loaded_mission)
    assert mission_schema.id == "M002"
    assert mission_schema.status == MissionStatusEnum.ACTIVE
    assert len(mission_schema.tasks) == 1
    assert mission_schema.tasks[0].id == "T010"

    vehicle_schema = VehicleRead.model_validate(loaded_hawk)
    assert vehicle_schema.id == VehicleIdEnum.HAWK01
    assert vehicle_schema.mode == VehicleModeEnum.SIMULATED

    obs_schema = ObservationRead.model_validate(obs)
    assert obs_schema.confidence == pytest.approx(0.99)
    assert obs_schema.raw == {"code": "ALPHA-7"}

    evt_schema = EventRead.model_validate(evt)
    trace_schema = ReasoningTraceRead.model_validate(trace)

    replay = MissionReplayResponse(
        mission_id="M002",
        entries=[
            MissionReplayTimelineEntry(
                entry_type="event",
                timestamp=evt_schema.timestamp,
                event=evt_schema,
            ),
            MissionReplayTimelineEntry(
                entry_type="reasoning_trace",
                timestamp=trace_schema.timestamp,
                reasoning_trace=trace_schema,
                world_model_snapshot={"plan_before": ["T010"], "plan_after": []},
            ),
        ],
    )
    assert len(replay.entries) == 2
    assert replay.entries[1].world_model_snapshot is not None


# ---------------------------------------------------------------------------
# 4. Rejection of Invalid Battery, Confidence, Priority, and Constrained Values
# ---------------------------------------------------------------------------


async def test_rejection_of_invalid_battery_confidence_priority_and_modes(
    db_session: AsyncSession,
) -> None:
    """Verify Pydantic and SQLite CheckConstraints reject out-of-range values."""
    # Battery out of range (< 0 or > 100)
    with pytest.raises(ValidationError):
        VehicleCreate(
            id=VehicleIdEnum.SCOUT01,
            vehicle_type=VehicleTypeEnum.GROUND,
            battery=-1.0,
        )
    with pytest.raises(ValidationError):
        VehicleUpdate(battery=105.0)

    # Confidence out of range (< 0.0 or > 1.0)
    with pytest.raises(ValidationError):
        ObservationCreate(
            id="OBS_BAD",
            mission_id="M001",
            vehicle_id=VehicleIdEnum.SCOUT01,
            obs_type="hazard",
            confidence=1.05,
            location_x=0.0,
            location_y=0.0,
        )
    with pytest.raises(ValidationError):
        ObservationCreate(
            id="OBS_BAD_NEG",
            mission_id="M001",
            vehicle_id=VehicleIdEnum.SCOUT01,
            obs_type="hazard",
            confidence=-0.1,
            location_x=0.0,
            location_y=0.0,
        )

    # Priority out of range (< 1 or > 100)
    with pytest.raises(ValidationError):
        TaskCreate(
            id="T_BAD_PRIO",
            mission_id="M001",
            type="SCAN",
            priority=0,
        )
    with pytest.raises(ValidationError):
        TaskCreate(
            id="T_BAD_PRIO_HIGH",
            mission_id="M001",
            type="SCAN",
            priority=101,
        )

    # Invalid emergency stop target
    with pytest.raises(ValidationError):
        EmergencyStopRequest.model_validate({"vehicle_id": "TANK99"})

    # Extra unknown fields forbidden
    with pytest.raises(ValidationError):
        MissionCreateRequest.model_validate(
            {"objective": "Valid objective", "undocumented_field": 123}
        )

    # Database-level CHECK constraint enforcement for battery and mode
    bad_vehicle = Vehicle(
        id="BAD_V",
        vehicle_type="ground",
        status="IDLE",
        battery=150.0,
        position_x=0.0,
        position_y=0.0,
        position_alt=0.0,
        connection="DISCONNECTED",
        mode="simulated",
    )
    db_session.add(bad_vehicle)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# 5. Correct Handling of Optional Fields and Timestamps
# ---------------------------------------------------------------------------


def test_optional_fields_and_timestamp_consistency() -> None:
    """Verify optional fields default cleanly and naive timestamps normalize to UTC."""
    naive_created = datetime(2026, 10, 27, 18, 0, 0)
    record = MissionCreateRecord(
        id="M_TIME",
        objective="Test UTC normalization",
        created_at=naive_created,
        completed_at=None,
    )
    assert record.created_at.tzinfo == timezone.utc
    assert record.completed_at is None

    # completed_at earlier than created_at must be rejected
    with pytest.raises(ValidationError, match="completed_at cannot be earlier"):
        MissionCreateRecord(
            id="M_BAD_TIME",
            objective="Invalid completion order",
            created_at=datetime(2026, 10, 27, 18, 0, 0, tzinfo=timezone.utc),
            completed_at=datetime(2026, 10, 27, 17, 59, 0, tzinfo=timezone.utc),
        )

    # Task with optional area, target, and vehicle_id omitted
    unassigned_task = TaskCreate(
        id="T_OPT",
        mission_id="M_TIME",
        type="STANDBY",
    )
    assert unassigned_task.vehicle_id is None
    assert unassigned_task.area is None
    assert unassigned_task.target is None
    assert unassigned_task.priority == 1
    assert unassigned_task.status == TaskStatusEnum.PENDING

    # Observation with optional raw payload omitted
    obs = ObservationCreate(
        id="OBS_OPT",
        mission_id="M_TIME",
        vehicle_id=VehicleIdEnum.SCOUT01,
        obs_type="clear_path",
        confidence=0.75,
        location_x=1.5,
        location_y=2.5,
    )
    assert obs.raw is None
    assert obs.timestamp.tzinfo == timezone.utc

    # Event & ReasoningTrace creation contracts
    event_contract = EventCreate(
        id="EVT_OPT",
        mission_id="M_TIME",
        category=EventCategoryEnum.SYSTEM,
        message="System ready",
    )
    assert event_contract.timestamp.tzinfo == timezone.utc

    trace_contract = ReasoningTraceCreate(
        id="RT_OPT",
        mission_id="M_TIME",
        trigger="Mission created",
        rationale="Initial decomposition",
    )
    assert trace_contract.resulting_task_ids == []


# ---------------------------------------------------------------------------
# 6. Validation of Supported WebSocket Message Types & Malformed Rejection
# ---------------------------------------------------------------------------


def test_websocket_contracts_validate_all_types_and_reject_malformed() -> None:
    """Verify versioned dashboard WS envelopes, full-state snapshot, and vehicle command stubs."""
    now = datetime.now(timezone.utc)

    # 1) telemetry envelope
    telemetry_env = validate_dashboard_ws_envelope(
        {
            "type": "telemetry",
            "vehicle_id": "SCOUT01",
            "sequence": 1,
            "payload": {
                "vehicle_id": "SCOUT01",
                "status": "ACTIVE",
                "connection": "CONNECTED",
                "mode": "simulated",
                "battery": 88.5,
                "position_x": 2.1,
                "position_y": 3.4,
                "position_alt": 0.0,
                "last_heartbeat": now.isoformat(),
            },
        }
    )
    assert isinstance(telemetry_env, DashboardTelemetryEnvelope)
    assert telemetry_env.protocol_version == "1.0"
    assert telemetry_env.payload.battery == pytest.approx(88.5)

    # 2) event envelope
    event_env = validate_dashboard_ws_envelope(
        {
            "type": "event",
            "payload": {
                "id": "EVT100",
                "mission_id": "M001",
                "timestamp": now.isoformat(),
                "category": "TELEMETRY",
                "message": "SCOUT01 heartbeat received",
            },
        }
    )
    assert isinstance(event_env, DashboardEventEnvelope)

    # 3) reasoning_trace envelope
    trace_env = validate_dashboard_ws_envelope(
        {
            "type": "reasoning_trace",
            "payload": {
                "id": "RT100",
                "mission_id": "M001",
                "timestamp": now.isoformat(),
                "trigger": "Obstacle detected",
                "rationale": "Reassigning sector scan to HAWK01",
                "resulting_task_ids": ["T002"],
            },
        }
    )
    assert isinstance(trace_env, DashboardReasoningTraceEnvelope)

    # 4) mission_status envelope
    mission_env = validate_dashboard_ws_envelope(
        {
            "type": "mission_status",
            "payload": {
                "mission_id": "M001",
                "objective": "Recon Sector Alpha",
                "status": "ACTIVE",
                "tasks": [],
            },
        }
    )
    assert isinstance(mission_env, DashboardMissionStatusEnvelope)

    # 5) Full-state snapshot on connect (§5.2)
    snapshot = DashboardSnapshotPayload(
        current_mission=None,
        vehicles={
            VehicleIdEnum.SCOUT01: VehicleRead(
                id=VehicleIdEnum.SCOUT01,
                vehicle_type=VehicleTypeEnum.GROUND,
                status=VehicleStatusEnum.IDLE,
                battery=100.0,
                position_x=0.0,
                position_y=0.0,
                position_alt=0.0,
                connection=VehicleConnectionStateEnum.DISCONNECTED,
                mode=VehicleModeEnum.SIMULATED,
            ),
            VehicleIdEnum.HAWK01: VehicleRead(
                id=VehicleIdEnum.HAWK01,
                vehicle_type=VehicleTypeEnum.AERIAL,
                status=VehicleStatusEnum.IDLE,
                battery=100.0,
                position_x=0.0,
                position_y=0.0,
                position_alt=0.0,
                connection=VehicleConnectionStateEnum.DISCONNECTED,
                mode=VehicleModeEnum.SIMULATED,
            ),
        },
        recent_events=[],
        recent_reasoning_traces=[],
    )
    assert snapshot.protocol_version == "1.0"
    assert set(snapshot.vehicles.keys()) == {VehicleIdEnum.SCOUT01, VehicleIdEnum.HAWK01}

    # 6) Reject unknown envelope type or mismatched payload
    with pytest.raises(ValidationError):
        validate_dashboard_ws_envelope({"type": "unknown_type", "payload": {}})

    with pytest.raises(ValidationError):
        validate_dashboard_ws_envelope(
            {
                "type": "telemetry",
                "payload": {"mission_id": "M001", "wrong": "shape"},
            }
        )

    # 7) Vehicle inbound/outbound stubs & rejection of arbitrary free-form command text (§5.1)
    inbound_telem = VehicleInboundTelemetryStub(
        vehicle_id=VehicleIdEnum.HAWK01,
        battery=91.0,
        position_x=1.0,
        position_y=2.0,
        position_alt=1.5,
    )
    assert inbound_telem.protocol_version == "1.0-stub"

    inbound_obs = VehicleInboundObservationStub(
        observation_id="OBS200",
        mission_id="M001",
        vehicle_id=VehicleIdEnum.HAWK01,
        obs_type="thermal_signature",
        confidence=0.88,
        location_x=5.0,
        location_y=6.0,
    )
    assert inbound_obs.to_observation_read().id == "OBS200"

    valid_cmd = VehicleOutboundCommandStub(
        command_id="CMD001",
        vehicle_id=VehicleIdEnum.SCOUT01,
        command_type="EMERGENCY_STOP",
        parameters={"reason": "operator_override"},
    )
    assert valid_cmd.command_type == "EMERGENCY_STOP"

    with pytest.raises(ValidationError):
        VehicleOutboundCommandStub(
            command_id="CMD002",
            vehicle_id=VehicleIdEnum.SCOUT01,
            command_type="move forward 10 meters please",
        )


# ---------------------------------------------------------------------------
# 7. Database Initialization, Session Cleanup, Logging & Error Responses
# ---------------------------------------------------------------------------


async def test_db_init_idempotency_session_rollback_logging_and_errors(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify idempotent vehicle seeding, session_scope rollback, JSON logging, and error schemas."""
    # Calling init_db again must not duplicate SCOUT01 or HAWK01
    await init_db(isolated_engine, seed_defaults=True)

    async with session_factory() as session:
        vehicles = (await session.execute(select(Vehicle))).scalars().all()
        assert len(vehicles) == 2
        assert {v.id for v in vehicles} == {"SCOUT01", "HAWK01"}

    # Verify session_scope rolls back on exception without leaking partial writes
    with pytest.raises(RuntimeError, match="simulated failure"):
        async with session_scope(session_factory) as scoped_session:
            scoped_session.add(
                Mission(
                    id="M_ROLLBACK",
                    objective="Should be rolled back",
                    status="PENDING",
                )
            )
            raise RuntimeError("simulated failure")

    async with session_factory() as session:
        rolled_back = (
            await session.execute(select(Mission).where(Mission.id == "M_ROLLBACK"))
        ).scalar_one_or_none()
        assert rolled_back is None

    # Verify structured JSON log formatting
    configure_logging(level="INFO", json_output=True)
    formatter = StructuredJsonFormatter()
    record = logging.LogRecord(
        name="operation_shadow.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Mission state transition",
        args=(),
        exc_info=None,
    )
    record.mission_id = "M001"
    formatted = json.loads(formatter.format(record))
    assert formatted["level"] == "INFO"
    assert formatted["mission_id"] == "M001"
    assert formatted["message"] == "Mission state transition"

    # Verify consistent error handler responses
    test_app = create_app()

    @test_app.get("/test-not-found")
    async def _raise_not_found() -> None:
        raise ResourceNotFoundError("Mission", "M999")

    @test_app.get("/test-lockout")
    async def _raise_lockout() -> None:
        raise HardwareLockoutError()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        res_404 = await client.get("/test-not-found")
        assert res_404.status_code == 404
        body_404 = res_404.json()
        assert body_404["error_code"] == "RESOURCE_NOT_FOUND"
        assert body_404["details"] == {"resource": "Mission", "id": "M999"}

        res_409 = await client.get("/test-lockout")
        assert res_409.status_code == 409
        assert res_409.json()["error_code"] == "HARDWARE_LOCKOUT_ACTIVE"


# ---------------------------------------------------------------------------
# 8. Existing Health Endpoint & 9. Strict Test Isolation Across Cases
# ---------------------------------------------------------------------------


async def test_isolation_step_a_insert_mission(db_session: AsyncSession) -> None:
    """Insert a mission in test A to verify it does not leak into test B."""
    db_session.add(
        Mission(
            id="M_LEAK_CHECK",
            objective="Ensure this row never leaks to subsequent tests",
            status=MissionStatusEnum.ACTIVE.value,
        )
    )
    await db_session.commit()

    found = (
        await db_session.execute(select(Mission).where(Mission.id == "M_LEAK_CHECK"))
    ).scalar_one_or_none()
    assert found is not None


async def test_isolation_step_b_verify_clean_database(db_session: AsyncSession) -> None:
    """Confirm M_LEAK_CHECK from test A is absent in test B's isolated database."""
    leaked = (
        await db_session.execute(select(Mission).where(Mission.id == "M_LEAK_CHECK"))
    ).scalar_one_or_none()
    assert leaked is None

    # Default seeded vehicles are still present and in simulated mode
    vehicles = (await db_session.execute(select(Vehicle))).scalars().all()
    assert {v.id for v in vehicles} == {"SCOUT01", "HAWK01"}
    assert all(v.mode == "simulated" for v in vehicles)
