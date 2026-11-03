"""Phase 3 ULTRON Mission Memory & Snapshot Unit Tests (`tests/test_phase3_mission_memory.py`).

Verifies `MissionMemorySnapshot`, `MemoryStore` bounds, coordinate-frame provenance honesty,
and `UltronLangGraphAgent` snapshot capture without hardware actuation.
"""

from __future__ import annotations

from pathlib import Path
import sys

from pydantic import ValidationError
import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in [str(_ROOT), str(_ROOT / "backend"), str(_ROOT / "backend" / "fastapi")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ultron.memory.store import (
    MemoryStore,
    MissionMemorySnapshot,
    SnapshotDecisionContext,
    SnapshotPhaseEnum,
    SnapshotTriggerTypeEnum,
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


def test_memory_store_bounds_and_camera_frame_provenance() -> None:
    """Verify MemoryStore enforces per-mission snapshot caps and preserves camera-relative coordinate provenance."""
    store = MemoryStore(max_snapshots_per_mission=10)
    world = WorldModel(
        mission=MissionState(
            id="M-MEM-BOUND",
            objective="Test memory store bounds and provenance",
            status=MissionStatus.IN_PROGRESS,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                mode=VehicleMode.SIMULATED,
                position=Position2D(x=1.0, y=2.0),
                battery=91.5,
                status="ACTIVE",
                connection=ConnectionStatus.ONLINE,
            )
        },
    )
    obs = SensorObservation(
        observation_id="OBS-OPTICAL-01",
        sensor_id="depth_camera",
        sensor_type=SensorTypeEnum.DEPTH_CAMERA,
        vehicle_id="SCOUT01",
        sensor_timestamp=1700000100.0,
        ingested_at=1700000100.1,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position=Position3DMeasurement(
            x=0.5, y=-0.1, z=1.4, range_m=1.4, uncertainty_m=0.1
        ),
        classification="person",
        confidence=0.96,
        validation_status=ObservationValidationStatusEnum.VALIDATED,
        source_mode=SensorSourceModeEnum.SIMULATED,
    )
    world.environment.observation_history.append(obs)
    world.environment.tracked_objects["OBJ-PERSON-1"] = TrackedObject(
        object_id="OBJ-PERSON-1",
        label="person",
        position=obs.position,
        coordinate_frame="oak_rgb_camera_optical_frame",
        position_uncertainty_m=0.1,
        classification_confidence=0.96,
        first_observed_timestamp=1700000100.0,
        last_observed_timestamp=1700000100.0,
        last_ingested_at=1700000100.1,
        last_known_source="depth_camera",
        vehicle_id="SCOUT01",
        source_mode=SensorSourceModeEnum.SIMULATED,
        contributing_observation_ids=["OBS-OPTICAL-01"],
        contributing_sensor_types=["depth_camera"],
        tracking_status=TrackingStatusEnum.ACTIVE,
        epistemic_status=EpistemicStatusEnum.OBSERVED,
        currently_visible=True,
    )

    for idx in range(14):
        snap = store.record_transition_snapshot(
            world,
            mission_id="M-MEM-BOUND",
            timestamp=1700000100.2 + idx * 0.1,
            trigger_type=SnapshotTriggerTypeEnum.REPLAN_ACCEPTED,
            trigger_reason=f"Decision {idx}",
            phase=SnapshotPhaseEnum.AFTER,
            associated_trace_id=f"RT-{idx:02d}",
            decision_context=SnapshotDecisionContext(
                resulting_task_ids=("T-01",),
                rationale=f"Decision {idx}",
            ),
        )
        assert snap.evidence_provenance.transform_map_validated is False
        assert "oak_rgb_camera_optical_frame" in snap.evidence_provenance.unconfirmed_coordinate_frames
        assert snap.tracked_objects[0].coordinate_frame == "oak_rgb_camera_optical_frame"
        assert snap.observation_refs[0].map_projection_allowed is False

    snaps = store.get_mission_snapshots("M-MEM-BOUND")
    assert len(snaps) == 10
    assert store.overflow_evicted_count == 4
    assert [s.associated_trace_id for s in snaps] == [
        f"RT-{idx:02d}" for idx in range(4, 14)
    ]

    # Round-trip JSON persistence & frozen immutability check
    persisted = snaps[0].to_persisted_dict()
    rehydrated = MissionMemorySnapshot.from_persisted_dict(persisted)
    assert rehydrated.snapshot_id == snaps[0].snapshot_id
    with pytest.raises(ValidationError):
        snaps[0].mission_objective = "Mutated"  # type: ignore[misc]
