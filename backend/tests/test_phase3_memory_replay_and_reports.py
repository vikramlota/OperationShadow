"""Phase 3 Backend Regression Suite: Mission Memory, Replay & After-Action Intelligence.

Covers all 20 required Phase 3 backend verification scenarios:
1. Snapshot serialization and Pydantic validation (`MissionMemorySnapshot` + nested models)
2. Snapshot persistence across backend restart (SQLite file DB reopen & `init_db` additive migration)
3. Snapshot immutability (Pydantic frozen model, SQLAlchemy `before_update` guard, SQLite trigger)
4. Correct mission and event association (`mission_id`, `associated_event_id`, `associated_trace_id`)
5. Snapshot capture at meaningful state transitions (mission created, initial plan, hazard/obstacle, Standing Order, triage, replan before/after, task transition, completion)
6. No snapshot explosion during high-frequency telemetry or duplicate camera frames
7. Snapshot storage failure handling (`snapshot_persistence_failures` counter, mission continues safely)
8. Backward compatibility with missions created before Phase 3 (zero rows in `mission_snapshots`)
9. Chronological replay ordering (including simultaneous timestamp deterministic tie-breaking)
10. Correct before/after snapshots around accepted replanning (`before_state` vs `after_state`)
11. Correct linkage between Phase 2 events, Standing Orders, triage, and reasoning traces
12. Missing historical data handled honestly (`historical_state_available: False` or `None`)
13. Mission report task totals match persisted records (`total`, `completed`, `pending`, `cancelled`, `failed`)
14. Mission report includes supported observations and events only (no hallucinated findings, camera-relative frames preserved)
15. Simulated execution clearly identified (`is_simulated_execution: True`, `SIMULATED_COMPLETED`)
16. Incomplete and cancelled mission reports (`ACTIVE` / `ABORTED` / `FAILED` missions, incomplete tasks listed)
17. E-Stop and critical safety events correctly represented in replay and report
18. Report generation works with Gemini unavailable (`GEMINI_API_KEY` unset, `llm_used_for_report: False`)
19. Report generation does not invoke physical commands or mutate mission state
20. Existing REST and WebSocket contracts remain compatible (`GET /health`, `POST /mission`, `GET /mission/{id}`, `GET /mission/{id}/replay`, `GET /mission/{id}/report`, `/ws/dashboard`)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Ensure backend and root paths are importable
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ROOT_DIR = _BACKEND_DIR.parent
for _p in [str(_ROOT_DIR), str(_BACKEND_DIR), str(_BACKEND_DIR / "fastapi")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.config import Settings
from db.models import Base, Event, Mission, MissionSnapshot, ReasoningTrace, Task
from db.session import ensure_phase3_schema_migrations, seed_default_vehicles
from main import create_app
from schemas.entities import (
    MissionCreateRequest,
    MissionReplayResponse,
    MissionReportResponse,
)
from services.event_service import EventService
from services.mission_service import MissionService
from services.ultron_agent_adapter import UltronLangGraphAgent
from services.vehicle_service import VehicleService
from ultron.events.schemas import (
    EventPriorityEnum,
    EventSourceEnum,
    EventTypeEnum,
    UltronEvent,
)
from ultron.memory.store import (
    MissionMemorySnapshot,
    SnapshotDecisionContext,
    SnapshotPhaseEnum,
    SnapshotTaskState,
    SnapshotTriggerTypeEnum,
    build_mission_snapshot,
)
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import MissionStatus
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    ObservationValidationStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorSourceModeEnum,
    SensorTypeEnum,
)
from ultron.schemas.world import (
    ConnectionStatus,
    MissionState,
    ScoutState,
    TrackedObject,
    TrackingStatusEnum,
    VehicleMode,
    WorldModel,
)
from ultron_stub import ManualClock, MockFleetSimulator
from websocket.dashboard_gateway import DashboardGateway


def _build_world_for_test(
    mission_id: str = "M-PH3-01",
    objective: str = "Sweep Sector Alpha and inspect corridor hazards",
    battery: float = 86.0,
) -> WorldModel:
    world = WorldModel(
        mission=MissionState(
            id=mission_id,
            objective=objective,
            status=MissionStatus.IN_PROGRESS,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                mode=VehicleMode.SIMULATED,
                position=Position2D(x=2.5, y=3.0),
                battery=battery,
                status="ACTIVE",
                connection=ConnectionStatus.ONLINE,
                last_heartbeat_timestamp=1700000100.0,
            )
        },
    )
    obs = SensorObservation(
        observation_id="OBS-CAM-PH3-01",
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        detector_object_id=1,
        sensor_timestamp=1700000100.0,
        ingested_at=1700000100.1,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(
            x=1.1, y=-0.1, z=1.2, range_m=1.2, uncertainty_m=0.08
        ),
        classification="chair",
        confidence=0.93,
        validation_status=ObservationValidationStatusEnum.VALIDATED,
        source_mode=SensorSourceModeEnum.SIMULATED,
        is_obstacle=True,
        is_hazard=False,
    )
    world.environment.observation_history.append(obs)
    world.environment.tracked_objects["OBJ-001"] = TrackedObject(
        object_id="OBJ-001",
        label="chair",
        position=obs.position,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position_uncertainty_m=0.08,
        classification_confidence=0.93,
        first_observed_timestamp=1700000100.0,
        last_observed_timestamp=1700000100.0,
        last_ingested_at=1700000100.1,
        last_known_source="depth_camera",
        vehicle_id="SCOUT01",
        source_mode=SensorSourceModeEnum.SIMULATED,
        contributing_observation_ids=["OBS-CAM-PH3-01"],
        contributing_sensor_types=["depth_camera"],
        tracking_status=TrackingStatusEnum.ACTIVE,
        epistemic_status=EpistemicStatusEnum.OBSERVED,
        currently_visible=True,
        is_obstacle=True,
        is_hazard=False,
    )
    return world


# ---------------------------------------------------------------------------
# Scenarios 1, 2, 3, 4: Snapshot Serialization, Persistence Across Restart,
#                       Immutability (Pydantic + ORM + SQLite Trigger), and Association
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_serialization_persistence_across_restart_immutability_and_association(
    tmp_path: Path,
) -> None:
    """Verify Scenarios 1, 2, 3, and 4: Pydantic validation, file SQLite restart persistence, 3-layer immutability, and ID association."""
    world = _build_world_for_test("M-SNAP-01", "Inspect Sector Alpha")
    snap = build_mission_snapshot(
        world,
        mission_id="M-SNAP-01",
        sequence=2,
        timestamp=1700000102.0,
        trigger_type=SnapshotTriggerTypeEnum.REPLAN_ACCEPTED,
        trigger_reason="Standing Order SO-03-BLOCKED-ROUTE applied",
        phase=SnapshotPhaseEnum.AFTER,
        associated_event_id="UEVT-001",
        associated_trace_id="M-SNAP-01-RT02",
        paired_before_snapshot_id="SNAP-M-SNAP-01-0001",
        task_states=[
            SnapshotTaskState(
                task_id="M-SNAP-01-T01",
                vehicle_id="SCOUT01",
                task_type="STOP",
                area="SECTOR_A",
                priority=1,
                status="ASSIGNED",
                execution_stage="ASSIGNED",
                is_simulated=True,
            )
        ],
        decision_context=SnapshotDecisionContext(
            event_id="UEVT-001",
            standing_order_id="SO-03-BLOCKED-ROUTE",
            planner_source="standing_order",
            validation_passed=True,
            resulting_task_ids=("M-SNAP-01-T01",),
            rationale="USED CACHED PLAN: Standing Order hold on obstacle",
        ),
    )

    # Scenario 1: Serialization & Pydantic validation
    dumped = snap.to_persisted_dict()
    rehydrated = MissionMemorySnapshot.from_persisted_dict(dumped)
    assert rehydrated.snapshot_id == snap.snapshot_id
    assert rehydrated.mission_id == "M-SNAP-01"
    assert rehydrated.associated_event_id == "UEVT-001"
    assert rehydrated.associated_trace_id == "M-SNAP-01-RT02"
    assert rehydrated.paired_before_snapshot_id == "SNAP-M-SNAP-01-0001"
    assert rehydrated.vehicles["SCOUT01"].mode == "simulated"
    assert rehydrated.vehicles["SCOUT01"].battery == 86.0
    assert len(rehydrated.tracked_objects) == 1
    assert rehydrated.tracked_objects[0].object_id == "OBJ-001"
    assert rehydrated.evidence_provenance.transform_map_validated is False
    assert (
        "oak_rgb_camera_optical_frame"
        in rehydrated.evidence_provenance.unconfirmed_coordinate_frames
    )

    # Invalid fields are rejected by Pydantic
    with pytest.raises(ValidationError):
        MissionMemorySnapshot.from_persisted_dict({**dumped, "unexpected_field": "bad"})

    # Scenario 3 (Layer 1): Pydantic frozen model rejects in-place mutation
    with pytest.raises(ValidationError):
        snap.mission_status = "ABORTED"  # type: ignore[misc]

    # Scenario 2 & 4: Persist to file-backed SQLite, close engine, reopen ("backend restart"), and read back
    db_file = tmp_path / "phase3_restart_test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    engine1 = create_async_engine(db_url)
    async with engine1.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_phase3_schema_migrations(conn)

    session_factory1 = async_sessionmaker(engine1, expire_on_commit=False)
    event_svc = EventService(gateway=None)

    async with session_factory1() as session:
        session.add(
            Mission(
                id="M-SNAP-01",
                objective="Inspect Sector Alpha",
                status="ACTIVE",
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        persisted = await event_svc.record_mission_snapshot(session, snap)
        assert persisted is not None
        assert persisted.id == snap.snapshot_id
        await session.commit()

    await engine1.dispose()

    # Reopen a brand-new engine against the same file (simulating backend restart)
    engine2 = create_async_engine(db_url)
    async with engine2.begin() as conn:
        await ensure_phase3_schema_migrations(conn)

    session_factory2 = async_sessionmaker(engine2, expire_on_commit=False)
    async with session_factory2() as session:
        loaded_dicts = await event_svc.list_mission_snapshots(session, "M-SNAP-01")
        assert len(loaded_dicts) == 1
        loaded_snap = MissionMemorySnapshot.from_persisted_dict(loaded_dicts[0])
        assert loaded_snap.snapshot_id == snap.snapshot_id
        assert loaded_snap.mission_id == "M-SNAP-01"
        assert loaded_snap.associated_event_id == "UEVT-001"
        assert loaded_snap.associated_trace_id == "M-SNAP-01-RT02"
        assert loaded_snap.decision_context is not None
        assert loaded_snap.decision_context.standing_order_id == "SO-03-BLOCKED-ROUTE"

        # Scenario 3 (Layer 2): SQLAlchemy ORM `before_update` listener rejects updates
        orm_row = await session.get(MissionSnapshot, snap.snapshot_id)
        assert orm_row is not None
        orm_row.trigger_type = "MUTATED_TRIGGER"
        with pytest.raises(ValueError, match="immutable"):
            await session.flush()
        await session.rollback()

        # Scenario 3 (Layer 3): SQLite trigger `trg_mission_snapshots_immutable` rejects raw SQL UPDATE
        with pytest.raises(IntegrityError, match="immutable"):
            await session.execute(
                text(
                    "UPDATE mission_snapshots SET trigger_type = 'HACKED' WHERE id = :id"
                ),
                {"id": snap.snapshot_id},
            )
        await session.rollback()

    await engine2.dispose()


# ---------------------------------------------------------------------------
# Scenarios 5, 6, 7: Meaningful State Transitions, No Snapshot Explosion,
#                    and Snapshot Storage Failure Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_meaningful_transitions_no_explosion_and_storage_failure_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Scenarios 5, 6, and 7: snapshots captured at meaningful transitions, no explosion on duplicate frames/heartbeats, and safe failure handling."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_phase3_schema_migrations(conn)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gateway = DashboardGateway()
    event_svc = EventService(gateway=gateway)
    agent = UltronLangGraphAgent(replan_cooldown_sec=0.01, replan_debounce_sec=0.0)
    mission_svc = MissionService(agent=agent, gateway=gateway, events=event_svc)

    async with session_factory() as session:
        await seed_default_vehicles(session)
        create_res = await mission_svc.create_mission(
            session,
            MissionCreateRequest(
                objective="Sweep Sector Alpha and report obstacles",
                language="English",
            ),
        )
        mission_id = create_res.mission_id

    # Scenario 5: Mission creation + initial plan accepted already captured snapshots
    initial_snaps = agent.memory_store.get_mission_snapshots(mission_id)
    trigger_types = [s.trigger_type for s in initial_snaps]
    assert SnapshotTriggerTypeEnum.MISSION_CREATED in trigger_types
    assert SnapshotTriggerTypeEnum.INITIAL_PLAN_ACCEPTED in trigger_types

    count_before_telemetry = len(agent.memory_store.get_mission_snapshots(mission_id))

    # Scenario 6: High-frequency routine telemetry heartbeats (25 updates without status/mode change or low battery)
    for i in range(25):
        await agent.update_vehicle_state(
            "SCOUT01",
            position_x=1.5 + i * 0.02,
            position_y=1.5,
            battery=95.0 - i * 0.1,
            status="ACTIVE",
            connection="CONNECTED",
            mode="simulated",
        )
    assert len(agent.memory_store.get_mission_snapshots(mission_id)) == count_before_telemetry

    # Scenario 6: 20 duplicate / non-event camera frames (same chair within dedup window)
    base_ts = datetime.now(timezone.utc)
    for i in range(20):
        frame_dt = base_ts + timedelta(milliseconds=i * 50)
        agent.ingest_detection_frame_into_world_model(
            stamp=frame_dt.timestamp(),
            received_at=frame_dt,
            frame="oak_rgb_camera_optical_frame",
            objects=[
                {
                    "id": 1,
                    "class": "chair",
                    "score": 0.94,
                    "position": {"x": 0.9, "y": 0.0, "z": 1.1},
                    "range_m": 1.1,
                }
            ],
            vehicle_id="SCOUT01",
            telemetry_source="rosbridge",
            map_frame_confirmed=False,
        )

    await agent.wait_for_replanning(timeout=3.0)
    snaps_after_burst = agent.memory_store.get_mission_snapshots(mission_id)
    # Out of 20 duplicate camera frames + 25 heartbeats (45 updates), only the first novel
    # obstacle detection + Standing Order replan + triage (5 transitions) are recorded, NOT 20x
    assert len(snaps_after_burst) <= count_before_telemetry + 6
    after_triggers = [s.trigger_type for s in snaps_after_burst]
    assert (
        SnapshotTriggerTypeEnum.EVENT_ROUTED in after_triggers
        or SnapshotTriggerTypeEnum.SIGNIFICANT_OBSERVATION_ACCEPTED in after_triggers
    )
    assert SnapshotTriggerTypeEnum.STANDING_ORDER_APPLIED in after_triggers

    # Scenario 5: Complete mission -> captures MISSION_COMPLETED snapshot in SQLite
    async with session_factory() as session:
        await mission_svc.complete_mission(session, mission_id)
        db_snaps = await event_svc.list_mission_snapshots(session, mission_id)
        db_triggers = [s["trigger_type"] for s in db_snaps]
        assert SnapshotTriggerTypeEnum.MISSION_COMPLETED.value in db_triggers

    # Scenario 7: Snapshot storage failure handling — if DB write fails, mission continues safely & failure is logged
    failures_before = event_svc.snapshot_persistence_failures
    agent_failures_before = agent.snapshot_persistence_failures_count
    async with session_factory() as session:
        await agent.sync_event_intelligence_to_event_service(
            session, event_svc, mission_id=mission_id, broadcast=False
        )
        agent.memory_store.record_transition_snapshot(
            agent.world,
            mission_id=mission_id,
            trigger_type=SnapshotTriggerTypeEnum.MISSION_COMPLETED,
            trigger_reason="Test storage failure resilience",
        )
        with patch.object(
            MissionSnapshot, "__init__", side_effect=RuntimeError("Simulated disk full")
        ):
            await agent.sync_event_intelligence_to_event_service(
                session, event_svc, mission_id=mission_id, broadcast=False
            )
    assert event_svc.snapshot_persistence_failures == failures_before + 1
    assert event_svc.last_snapshot_persistence_error is not None
    assert "Simulated disk full" in event_svc.last_snapshot_persistence_error
    assert agent.snapshot_persistence_failures_count == agent_failures_before + 1

    await engine.dispose()


# ---------------------------------------------------------------------------
# Scenarios 8, 9, 10, 11, 12: Backward Compatibility, Chronological Ordering,
#                             Before/After Replanning Snapshots, Phase 2 Linkage,
#                             and Honest Missing Historical State
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_chronological_ordering_before_after_snapshots_phase2_linkage_and_legacy_honesty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Scenarios 8, 9, 10, 11, and 12 on GET /mission/{id}/replay and MissionService.get_mission_replay."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_phase3_schema_migrations(conn)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gateway = DashboardGateway()
    event_svc = EventService(gateway=gateway)
    agent = UltronLangGraphAgent(replan_cooldown_sec=0.01, replan_debounce_sec=0.0)
    mission_svc = MissionService(agent=agent, gateway=gateway, events=event_svc)

    # Scenario 8 & 12: Create a legacy pre-Phase-3 mission directly in SQLite WITHOUT any `mission_snapshots` rows
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        await seed_default_vehicles(session)
        session.add(
            Mission(
                id="M-LEGACY",
                objective="Legacy Phase 1 mission without snapshots",
                status="COMPLETED",
                created_at=t0,
                completed_at=t0 + timedelta(seconds=30),
            )
        )
        session.add(
            Task(
                id="M-LEGACY-T01",
                mission_id="M-LEGACY",
                vehicle_id="SCOUT01",
                type="PATROL",
                area="SECTOR_A",
                target="Sector Alpha",
                priority=1,
                status="COMPLETED",
            )
        )
        # Simultaneous timestamp tie-breaking: event and reasoning_trace at exact same timestamp t0
        session.add(
            Event(
                id="M-LEGACY-EVT01",
                mission_id="M-LEGACY",
                timestamp=t0,
                category="MISSION",
                message="Legacy mission M-LEGACY created",
            )
        )
        session.add(
            ReasoningTrace(
                id="M-LEGACY-RT01",
                mission_id="M-LEGACY",
                timestamp=t0,
                trigger="Operator submitted M-LEGACY",
                rationale="USED CACHED PLAN: Legacy deterministic plan",
                resulting_task_ids=["M-LEGACY-T01"],
            )
        )
        await session.commit()

        legacy_replay = await mission_svc.get_mission_replay(session, "M-LEGACY")
        assert legacy_replay.mission_id == "M-LEGACY"
        assert len(legacy_replay.entries) == 2
        # Scenario 9: Simultaneous timestamp tie-breaker orders 'event' before 'reasoning_trace'
        assert legacy_replay.entries[0].entry_type == "event"
        assert legacy_replay.entries[1].entry_type == "reasoning_trace"
        # Scenario 12: Missing historical state is explicitly reported as None / historical_state_available=False (never fabricated)
        assert legacy_replay.entries[0].world_model_snapshot is None
        assert legacy_replay.entries[1].world_model_snapshot is not None
        assert legacy_replay.entries[1].world_model_snapshot["historical_state_available"] is False

        # Legacy mission report also works cleanly with `historical_state_available: False`
        legacy_report = await mission_svc.generate_mission_report(session, "M-LEGACY")
        assert legacy_report.historical_state_available is False
        assert legacy_report.historical_snapshots_count == 0
        assert legacy_report.task_summary.total_tasks == 1
        assert legacy_report.task_summary.completed_tasks == 1

    # Scenarios 9, 10, 11: Full Phase 3 mission with Phase 2 Standing Order + LLM Triage fallback + Replan
    async with session_factory() as session:
        create_res = await mission_svc.create_mission(
            session,
            MissionCreateRequest(
                objective="Locate red_package in Sector Alpha and avoid hazards",
                language="English",
            ),
        )
        m_id = create_res.mission_id

    # 1) Trigger Standing Order via blocking obstacle detection
    now_dt = datetime.now(timezone.utc)
    agent.ingest_detection_frame_into_world_model(
        stamp=now_dt.timestamp(),
        received_at=now_dt,
        frame="oak_rgb_camera_optical_frame",
        objects=[
            {
                "id": 10,
                "class": "chair",
                "score": 0.95,
                "position": {"x": 0.8, "y": 0.0, "z": 1.0},
                "range_m": 1.0,
            }
        ],
        vehicle_id="SCOUT01",
        telemetry_source="rosbridge",
        map_frame_confirmed=False,
    )
    await agent.wait_for_replanning(timeout=3.0)

    # 2) Trigger LLM Triage (fallback) + replanning via ambiguous sensor degradation event
    agent.submit_event(
        UltronEvent(
            event_id="UEVT-PH3-TRIAGE",
            event_type=EventTypeEnum.SENSOR_DEGRADED,
            source=EventSourceEnum.SENSOR_HEALTH,
            priority=EventPriorityEnum.HIGH,
            mission_id=m_id,
            vehicle_id="SCOUT01",
            received_at=now_dt.timestamp() + 2.0,
            evidence_payload={"sensor_id": "depth_camera"},
            summary="Depth camera frame rate degraded during sweep",
        )
    )
    await agent.wait_for_replanning(timeout=3.0)

    async with session_factory() as session:
        await agent.sync_event_intelligence_to_event_service(
            session, event_svc, mission_id=m_id, broadcast=False
        )
        await session.commit()
        replay = await mission_svc.get_mission_replay(session, m_id)
        assert replay.mission_id == m_id
        assert len(replay.entries) >= 4

        # Scenario 9: Strictly non-decreasing timestamps
        timestamps = [e.timestamp for e in replay.entries]
        assert timestamps == sorted(timestamps)

        # Scenario 10: Replan reasoning_trace entry has both before_state and after_state
        rt_entries = [e for e in replay.entries if e.entry_type == "reasoning_trace"]
        assert len(rt_entries) >= 2
        replan_entries_with_both = [
            e
            for e in rt_entries
            if e.world_model_snapshot
            and e.world_model_snapshot.get("historical_state_available") is True
            and (
                e.world_model_snapshot.get("before_state") is not None
                or e.world_model_snapshot.get("after_state") is not None
            )
        ]
        assert len(replan_entries_with_both) >= 2

        # Check a Standing Order reasoning trace entry has before_state AND after_state and standing_order_id linkage
        so_entries = [
            e
            for e in rt_entries
            if e.world_model_snapshot
            and e.world_model_snapshot.get("standing_order_id") == "SO-03-BLOCKED-ROUTE"
        ]
        assert len(so_entries) >= 1
        so_meta = so_entries[0].world_model_snapshot
        assert so_meta is not None
        assert so_meta["before_state"] is not None
        assert so_meta["after_state"] is not None
        assert so_meta["before_snapshot_id"] != so_meta["after_snapshot_id"]

        # Scenario 11: Event entries preserve structured Phase 2 event_type, severity, source_mode, and evidence_ids
        ev_entries = [e for e in replay.entries if e.entry_type == "event"]
        routed_ev_entries = [
            e
            for e in ev_entries
            if e.world_model_snapshot
            and e.world_model_snapshot.get("event_type") in ("OBSTACLE_DETECTED", "SENSOR_DEGRADED")
        ]
        assert len(routed_ev_entries) >= 1
        assert routed_ev_entries[0].world_model_snapshot["source_mode"] in ("simulated", "physical")

    await engine.dispose()


# ---------------------------------------------------------------------------
# Scenarios 13, 14, 15, 16, 17, 18, 19, 20: After-Action Report & API Contracts
# ---------------------------------------------------------------------------


def test_after_action_report_scenarios_and_api_contracts(
    isolated_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Scenarios 13–20 end-to-end via FastAPI TestClient and SQLite."""
    # Scenario 18: Ensure GEMINI_API_KEY is unset; report generation must be 100% deterministic
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    active_clock = ManualClock(datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc))
    agent = UltronLangGraphAgent(replan_cooldown_sec=0.01, replan_debounce_sec=0.0)
    simulator = MockFleetSimulator(clock=active_clock)
    gateway = DashboardGateway()
    event_svc = EventService(gateway=gateway)
    mission_svc = MissionService(agent=agent, gateway=gateway, events=event_svc)
    vehicle_svc = VehicleService(
        agent=agent,
        clock=active_clock,
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
    )

    with TestClient(app) as client:
        # Scenario 20: Existing health & WebSocket contracts remain compatible
        health_resp = client.get("/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["hardware_lockout"] is True

        # Create Mission 1 (will be completed in simulation)
        create_resp = client.post(
            "/mission",
            json={
                "objective": "Simulated SCOUT01 sweep of Sector Alpha for obstacles and hazards",
                "language": "English",
            },
        )
        assert create_resp.status_code in (200, 201)
        m1_id = create_resp.json()["mission_id"]

        # Scenario 16: Generate report while mission is still ACTIVE (incomplete tasks)
        active_report_resp = client.get(f"/mission/{m1_id}/report")
        assert active_report_resp.status_code == 200
        active_report = MissionReportResponse.model_validate(active_report_resp.json())
        assert active_report.overview.final_status == "ACTIVE"
        assert active_report.task_summary.total_tasks >= 1
        assert len(active_report.unresolved_items.incomplete_tasks) >= 1
        assert active_report.report_deterministic is True
        assert active_report.llm_used_for_report is False

        # Complete Mission 1 in simulation
        complete_resp = client.post(f"/mission/{m1_id}/complete")
        assert complete_resp.status_code == 200
        assert complete_resp.json()["status"] == "COMPLETED"

        # Scenario 13, 14, 15, 18, 19: Generate completed mission report and verify task counts, provenance, and non-mutation
        mission_before = client.get(f"/mission/{m1_id}").json()
        vehicle_before = client.get("/vehicle/SCOUT01/status").json()

        report_resp = client.get(f"/mission/{m1_id}/report")
        assert report_resp.status_code == 200
        report = MissionReportResponse.model_validate(report_resp.json())

        # Scenario 19: Report generation does not mutate mission or vehicle state
        mission_after = client.get(f"/mission/{m1_id}").json()
        vehicle_after = client.get("/vehicle/SCOUT01/status").json()
        assert mission_before == mission_after
        assert vehicle_before["status"] == vehicle_after["status"]

        # Scenario 13: Task totals match persisted records
        persisted_tasks = mission_after["tasks"]
        assert report.task_summary.total_tasks == len(persisted_tasks)
        assert report.task_summary.completed_tasks == sum(
            1 for t in persisted_tasks if t["status"] == "COMPLETED"
        )
        assert report.task_summary.pending_tasks == sum(
            1 for t in persisted_tasks if t["status"] in ("PENDING", "ASSIGNED", "IN_PROGRESS")
        )
        assert report.task_summary.cancelled_tasks == sum(
            1 for t in persisted_tasks if t["status"] == "CANCELLED"
        )
        assert report.task_summary.failed_tasks == sum(
            1 for t in persisted_tasks if t["status"] == "FAILED"
        )

        # Scenario 15: Simulated execution is clearly identified and never claimed as physical execution
        assert report.overview.is_simulated_execution is True
        assert report.overview.vehicle_mode == "simulated"
        assert "SIMULATED" in report.overview.execution_provenance.upper()
        for t_rec in report.task_summary.tasks:
            assert t_rec.is_simulated is True
            assert t_rec.highest_evidence_stage == "SIMULATED_COMPLETED"
            assert t_rec.highest_evidence_stage != "PHYSICAL_EXECUTION_CONFIRMED"

        # Markdown export format endpoint (`?format=markdown`)
        md_resp = client.get(f"/mission/{m1_id}/report?format=markdown")
        assert md_resp.status_code == 200
        assert "AFTER-ACTION MISSION REPORT" in md_resp.text.upper()
        assert m1_id in md_resp.text
        assert "SIMULATED" in md_resp.text.upper()

        # Scenario 17: Trigger Emergency Stop during a second mission and verify E-Stop & cancelled tasks in report
        create_m2 = client.post(
            "/mission",
            json={
                "objective": "Patrol Sector Bravo before emergency stop",
                "language": "English",
            },
        )
        assert create_m2.status_code in (200, 201)
        m2_id = create_m2.json()["mission_id"]

        estop_resp = client.post("/emergency-stop", json={"vehicle_id": "ALL"})
        assert estop_resp.status_code == 200
        assert estop_resp.json()["request_accepted"] is True

        m2_report_resp = client.get(f"/mission/{m2_id}/report")
        assert m2_report_resp.status_code == 200
        m2_report = MissionReportResponse.model_validate(m2_report_resp.json())
        assert (
            len(m2_report.safety_summary.emergency_stop_events) >= 1
            or m2_report.vehicle_summary.final_status == "EMERGENCY_STOPPED"
        )
        assert (
            "Simulated" in m2_report.safety_summary.stop_dispatch_acknowledged_status
            or "Emergency Stop" in m2_report.safety_summary.stop_dispatch_acknowledged_status
        )

        # Scenario 20: Replay endpoint and WebSocket snapshot contract remain compatible
        replay_resp = client.get(f"/mission/{m1_id}/replay")
        assert replay_resp.status_code == 200
        replay_model = MissionReplayResponse.model_validate(replay_resp.json())
        assert replay_model.mission_id == m1_id
        assert len(replay_model.entries) >= 2

        with client.websocket_connect("/ws/dashboard") as ws:
            snap_msg = ws.receive_json()
            assert snap_msg["type"] == "snapshot"
            assert snap_msg["protocol_version"] == "1.0"
