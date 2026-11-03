"""Persistent & In-Memory Mission Memory Store (`ultron/memory/store.py` — Phase 3).

Provides:
1. Immutable `MissionMemorySnapshot` schemas derived from the authoritative `WorldModel`
   at meaningful mission transitions (initial planning, accepted plans, task transitions,
   significant observations, Event Router classifications, Standing Order executions,
   LLM Event Triage evaluations, before/after replans, vehicle state changes, and
   mission completion/cancellation/failure).
2. Explicit evidence provenance tracking distinguishing:
   - `REAL` (`physical`) observations
   - `SIMULATED` (`simulated`) observations
   - `UNKNOWN` data & sensor gaps
   - `STALE` observations
   - Unconfirmed coordinate frames (`oak_rgb_camera_optical_frame`, `oak_parent_frame`, unconfirmed `map`)
   - Inferences vs. direct observations
3. Bounded payload sizes (`MAX_SNAPSHOT_OBSERVATION_REFS`, `MAX_SNAPSHOT_TRACKED_OBJECTS`,
   `MAX_SNAPSHOTS_PER_MISSION`) to prevent unbounded RAM or SQLite growth during
   high-frequency telemetry or camera streams.
4. Backward-compatible `MemoryStore` interface (`record_snapshot`, `get_latest`,
   `get_timeline`, `clear`) alongside Phase 3 structured transition snapshots.
"""

from __future__ import annotations

from collections import OrderedDict
import copy
from datetime import datetime, timezone
from enum import Enum
import time
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple
import uuid

from pydantic import BaseModel, ConfigDict, Field

from ultron.config import config as ultron_config
from ultron.schemas.commands import Position2D
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    SensorObservation,
    SensorSourceModeEnum,
)
from ultron.schemas.world import (
    HazardEntity,
    SensorHealthReport,
    SensorHealthStatusEnum,
    TrackedObject,
    TrackingStatusEnum,
    WorldModel,
)
from ultron.world_model.tools import get_sensor_health

MAX_SNAPSHOT_OBSERVATION_REFS = 25
MAX_SNAPSHOT_TRACKED_OBJECTS = 50
MAX_SNAPSHOT_HAZARDS = 50
MAX_SNAPSHOT_OBSTACLES = 50
MAX_SNAPSHOTS_PER_MISSION = 250


class SnapshotTriggerTypeEnum(str, Enum):
    """Meaningful mission transition triggers that produce a persistent snapshot."""

    MISSION_CREATED = "MISSION_CREATED"
    INITIAL_PLAN_ACCEPTED = "INITIAL_PLAN_ACCEPTED"
    TASK_STATE_TRANSITION = "TASK_STATE_TRANSITION"
    SIGNIFICANT_OBSERVATION_ACCEPTED = "SIGNIFICANT_OBSERVATION_ACCEPTED"
    EVENT_ROUTED = "EVENT_ROUTED"
    STANDING_ORDER_APPLIED = "STANDING_ORDER_APPLIED"
    LLM_TRIAGE_COMPLETED = "LLM_TRIAGE_COMPLETED"
    REPLAN_BEFORE = "REPLAN_BEFORE"
    REPLAN_ACCEPTED = "REPLAN_ACCEPTED"
    REPLAN_DEGRADED = "REPLAN_DEGRADED"
    VEHICLE_STATE_TRANSITION = "VEHICLE_STATE_TRANSITION"
    EMERGENCY_STOP_TRIGGERED = "EMERGENCY_STOP_TRIGGERED"
    MISSION_COMPLETED = "MISSION_COMPLETED"
    MISSION_FAILED = "MISSION_FAILED"
    MISSION_CANCELLED = "MISSION_CANCELLED"


class SnapshotPhaseEnum(str, Enum):
    """Role of a snapshot relative to a decision or state transition."""

    BEFORE = "BEFORE"
    AFTER = "AFTER"
    STATE_TRANSITION = "STATE_TRANSITION"


class SnapshotTaskState(BaseModel):
    """Immutable snapshot of a single mission task's state and execution provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    vehicle_id: str = "SCOUT01"
    task_type: str
    area: Optional[str] = None
    target: Optional[str] = None
    priority: int = Field(default=1, ge=1, le=100)
    status: str = "ASSIGNED"
    execution_stage: Literal[
        "PLANNED",
        "ASSIGNED",
        "COMMAND_ACCEPTED",
        "COMMAND_DISPATCHED",
        "VEHICLE_ACKNOWLEDGED",
        "PHYSICAL_EXECUTION_CONFIRMED",
        "SIMULATED_COMPLETED",
        "IN_PROGRESS",
        "CANCELLED",
        "FAILED",
    ] = "ASSIGNED"
    is_simulated: bool = True


class SnapshotVehicleState(BaseModel):
    """Immutable snapshot of a vehicle's pose, battery, connection, and mode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vehicle_id: str = "SCOUT01"
    vehicle_type: str = "ground"
    mode: str = "simulated"
    status: str = "IDLE"
    connection: str = "ONLINE"
    pose_valid: bool = True
    coordinate_frame: str = "map"
    position_x: float = 0.0
    position_y: float = 0.0
    position_alt: float = 0.0
    heading: float = 0.0
    battery: float = Field(default=100.0, ge=0.0, le=100.0)
    last_heartbeat_timestamp: Optional[float] = None
    epistemic_status: EpistemicStatusEnum = EpistemicStatusEnum.OBSERVED


class SnapshotObservationRef(BaseModel):
    """Compact, bounded reference to a validated observation in the World Model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str
    sensor_id: str
    sensor_type: str
    vehicle_id: Optional[str] = None
    sensor_timestamp: float
    ingested_at: float
    coordinate_frame: str
    classification: Optional[str] = None
    confidence: Optional[float] = None
    position_x: float
    position_y: float
    position_z: float = 0.0
    range_m: Optional[float] = None
    epistemic_status: EpistemicStatusEnum = EpistemicStatusEnum.OBSERVED
    source_mode: SensorSourceModeEnum = SensorSourceModeEnum.SIMULATED
    is_obstacle: bool = False
    is_hazard: bool = False
    is_stale: bool = False
    map_projection_allowed: bool = False


class SnapshotEvidenceProvenance(BaseModel):
    """Explicit epistemic and provenance breakdown for a mission snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    real_observation_ids: Tuple[str, ...] = ()
    simulated_observation_ids: Tuple[str, ...] = ()
    stale_observation_ids: Tuple[str, ...] = ()
    conflicted_object_ids: Tuple[str, ...] = ()
    corroborated_object_ids: Tuple[str, ...] = ()
    unconfirmed_coordinate_frames: Tuple[str, ...] = ()
    unknowns: Tuple[str, ...] = ()
    inferences: Tuple[str, ...] = ()
    transform_map_validated: bool = False
    extrinsics_camera_lidar_validated: bool = False
    free_space_verified_clear: bool = False


class SnapshotDecisionContext(BaseModel):
    """Structured context linking a snapshot to an event, Standing Order, triage, or replan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: Optional[str] = None
    event_type: Optional[str] = None
    event_priority: Optional[str] = None
    event_source: Optional[str] = None
    event_source_mode: Optional[str] = None
    evidence_ids: Tuple[str, ...] = ()
    standing_order_id: Optional[str] = None
    triage_disposition: Optional[str] = None
    triage_severity: Optional[str] = None
    triage_source: Optional[str] = None
    planner_source: Optional[str] = None
    validation_passed: Optional[bool] = None
    validation_errors: Tuple[str, ...] = ()
    resulting_task_ids: Tuple[str, ...] = ()
    rationale: Optional[str] = None


class MissionMemorySnapshot(BaseModel):
    """Immutable historical snapshot of the ULTRON World Model at a meaningful transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str
    mission_id: str
    sequence: int = Field(ge=1)
    timestamp: float = Field(gt=0.0)
    timestamp_iso: str
    trigger_type: SnapshotTriggerTypeEnum
    trigger_reason: str
    phase: SnapshotPhaseEnum = SnapshotPhaseEnum.STATE_TRANSITION
    associated_event_id: Optional[str] = None
    associated_trace_id: Optional[str] = None
    paired_before_snapshot_id: Optional[str] = None
    mission_status: str
    mission_objective: str
    task_states: Tuple[SnapshotTaskState, ...] = ()
    active_task_ids: Dict[str, Optional[str]] = Field(default_factory=dict)
    completed_task_ids: Tuple[str, ...] = ()
    pending_task_ids: Tuple[str, ...] = ()
    vehicles: Dict[str, SnapshotVehicleState] = Field(default_factory=dict)
    observation_refs: Tuple[SnapshotObservationRef, ...] = ()
    total_observation_count: int = Field(default=0, ge=0)
    tracked_objects: Tuple[TrackedObject, ...] = ()
    hazards: Tuple[HazardEntity, ...] = ()
    known_obstacles: Tuple[Position2D, ...] = ()
    explored_areas: Tuple[str, ...] = ()
    sensor_health: Dict[str, SensorHealthReport] = Field(default_factory=dict)
    evidence_provenance: SnapshotEvidenceProvenance = Field(
        default_factory=SnapshotEvidenceProvenance
    )
    decision_context: Optional[SnapshotDecisionContext] = None

    def to_persisted_dict(self) -> dict[str, Any]:
        """Return a deep-copied JSON-serializable dict for SQLite storage or API responses."""
        return copy.deepcopy(self.model_dump(mode="json"))

    @classmethod
    def from_persisted_dict(cls, payload: dict[str, Any]) -> MissionMemorySnapshot:
        """Reconstruct and validate an immutable `MissionMemorySnapshot` from persisted JSON."""
        return cls.model_validate(copy.deepcopy(payload))


def build_mission_snapshot(
    world: WorldModel,
    *,
    mission_id: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    sequence: int = 1,
    timestamp: Optional[float] = None,
    trigger_type: SnapshotTriggerTypeEnum = SnapshotTriggerTypeEnum.MISSION_CREATED,
    trigger_reason: str = "Mission state transition",
    phase: SnapshotPhaseEnum = SnapshotPhaseEnum.STATE_TRANSITION,
    associated_event_id: Optional[str] = None,
    associated_trace_id: Optional[str] = None,
    paired_before_snapshot_id: Optional[str] = None,
    task_states: Optional[Sequence[SnapshotTaskState | dict[str, Any]]] = None,
    decision_context: Optional[SnapshotDecisionContext | dict[str, Any]] = None,
    max_observation_refs: int = MAX_SNAPSHOT_OBSERVATION_REFS,
    max_tracked_objects: int = MAX_SNAPSHOT_TRACKED_OBJECTS,
) -> MissionMemorySnapshot:
    """Construct an immutable, bounded `MissionMemorySnapshot` from `world` without mutating `world`."""
    world_copy = world.model_copy(deep=True)
    resolved_mission_id = (mission_id or world_copy.mission.id or "M001").strip()
    now_ts = (
        float(timestamp)
        if (timestamp is not None and timestamp > 0.0)
        else time.time()
    )
    ts_iso = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
    resolved_snap_id = (
        snapshot_id
        or f"SNAP-{resolved_mission_id}-{sequence:04d}-{uuid.uuid4().hex[:6].upper()}"
    )

    stale_thresh = ultron_config.camera_stale_after_sec

    # 1. Vehicle state snapshots
    vehicles_out: Dict[str, SnapshotVehicleState] = {}
    for vid, v in world_copy.vehicles.items():
        conn_str = v.connection.value if hasattr(v.connection, "value") else str(v.connection)
        mode_str = v.mode.value if hasattr(v.mode, "value") else str(v.mode)
        pose_valid = bool(getattr(v, "pose_valid", True)) and conn_str in ("ONLINE", "CONNECTED")
        vehicles_out[vid] = SnapshotVehicleState(
            vehicle_id=vid,
            vehicle_type=getattr(v, "vehicle_type", "ground"),
            mode=mode_str,
            status=str(v.status),
            connection=conn_str,
            pose_valid=pose_valid,
            coordinate_frame=getattr(v, "coordinate_frame", "map"),
            position_x=float(v.position.x),
            position_y=float(v.position.y),
            position_alt=float(getattr(v.position, "alt", 0.0)),
            heading=float(getattr(v, "heading", 0.0)),
            battery=float(v.battery),
            last_heartbeat_timestamp=getattr(v, "last_heartbeat_timestamp", None),
            epistemic_status=(
                EpistemicStatusEnum.OBSERVED if pose_valid else EpistemicStatusEnum.UNKNOWN
            ),
        )

    # 2. Sensor health evaluation at `now_ts`
    health_query = get_sensor_health(world_copy, current_time=now_ts)
    sensor_health_out = {
        sid: rep.model_copy(deep=True) for sid, rep in health_query.sensors.items()
    }

    # 3. Bounded observation references & provenance classification
    all_obs: List[SensorObservation] = list(world_copy.environment.observation_history)
    total_obs_count = len(all_obs)
    sliced_obs = all_obs[-max(1, max_observation_refs):] if all_obs else []

    real_obs_ids: List[str] = []
    sim_obs_ids: List[str] = []
    stale_obs_ids: List[str] = []
    unconfirmed_frames_set: set[str] = set()
    obs_refs: List[SnapshotObservationRef] = []

    for obs in all_obs:
        is_stale_obs = (now_ts - obs.sensor_timestamp) > stale_thresh
        if obs.source_mode == SensorSourceModeEnum.PHYSICAL:
            real_obs_ids.append(obs.observation_id)
        else:
            sim_obs_ids.append(obs.observation_id)
        if is_stale_obs:
            stale_obs_ids.append(obs.observation_id)
        if obs.coordinate_frame != "map" or not world_copy.environment.transform_map_validated:
            unconfirmed_frames_set.add(obs.coordinate_frame)

    for obs in sliced_obs:
        is_stale_obs = (now_ts - obs.sensor_timestamp) > stale_thresh
        ep_status = (
            EpistemicStatusEnum.STALE
            if is_stale_obs
            else EpistemicStatusEnum.OBSERVED
        )
        map_allowed = (
            obs.coordinate_frame == "map"
            and world_copy.environment.transform_map_validated
        )
        obs_refs.append(
            SnapshotObservationRef(
                observation_id=obs.observation_id,
                sensor_id=obs.sensor_id,
                sensor_type=(
                    obs.sensor_type.value
                    if hasattr(obs.sensor_type, "value")
                    else str(obs.sensor_type)
                ),
                vehicle_id=obs.vehicle_id,
                sensor_timestamp=obs.sensor_timestamp,
                ingested_at=obs.ingested_at,
                coordinate_frame=obs.coordinate_frame,
                classification=obs.classification,
                confidence=obs.confidence,
                position_x=obs.position.x,
                position_y=obs.position.y,
                position_z=obs.position.z,
                range_m=obs.position.range_m,
                epistemic_status=ep_status,
                source_mode=obs.source_mode,
                is_obstacle=obs.is_obstacle,
                is_hazard=obs.is_hazard,
                is_stale=is_stale_obs,
                map_projection_allowed=map_allowed,
            )
        )

    # 4. Bounded tracked objects evaluated for freshness at `now_ts`
    tracked_sorted = sorted(
        world_copy.environment.tracked_objects.values(),
        key=lambda item: item.object_id,
    )[: max(1, max_tracked_objects)]
    evaluated_tracked: List[TrackedObject] = []
    conflicted_ids: List[str] = []
    corroborated_ids: List[str] = []

    for tobj in tracked_sorted:
        t_copy = tobj.model_copy(deep=True)
        if (now_ts - t_copy.last_observed_timestamp) > stale_thresh:
            t_copy.currently_visible = False
            if t_copy.tracking_status != TrackingStatusEnum.CONFLICTED:
                t_copy.tracking_status = TrackingStatusEnum.STALE
            if t_copy.epistemic_status != EpistemicStatusEnum.CONFLICTED:
                t_copy.epistemic_status = EpistemicStatusEnum.STALE
        if t_copy.epistemic_status == EpistemicStatusEnum.CONFLICTED:
            conflicted_ids.append(t_copy.object_id)
        if t_copy.corroborated_by_multiple_sensors:
            corroborated_ids.append(t_copy.object_id)
        if t_copy.coordinate_frame != "map" or not world_copy.environment.transform_map_validated:
            unconfirmed_frames_set.add(t_copy.coordinate_frame)
        evaluated_tracked.append(t_copy)

    # 5. Task states & active/completed/pending trackers
    active_map: Dict[str, Optional[str]] = {
        (k.value if hasattr(k, "value") else str(k)): v
        for k, v in world_copy.mission.tasks.active.items()
    }
    completed_ids = tuple(world_copy.mission.tasks.completed)
    pending_ids = tuple(world_copy.mission.tasks.pending)

    resolved_tasks: List[SnapshotTaskState] = []
    if task_states is not None:
        for item in task_states:
            if isinstance(item, SnapshotTaskState):
                resolved_tasks.append(item)
            elif isinstance(item, dict):
                resolved_tasks.append(SnapshotTaskState.model_validate(item))
    else:
        for vid_key, tid in active_map.items():
            if tid:
                resolved_tasks.append(
                    SnapshotTaskState(
                        task_id=tid,
                        vehicle_id=vid_key,
                        task_type="ACTIVE_TASK",
                        priority=1,
                        status="ASSIGNED",
                        execution_stage="ASSIGNED",
                        is_simulated=True,
                    )
                )

    # 6. Collect unknowns and inferences from latest decision traces & sensor health
    unknowns_list: List[str] = list(health_query.warnings)
    if not world_copy.environment.transform_map_validated:
        unknowns_list.append(
            "Global map transform (map -> odom -> oak_parent_frame) is NOT confirmed; camera coordinates remain camera-relative."
        )
    if not world_copy.environment.extrinsics_camera_lidar_validated:
        unknowns_list.append(
            "Camera-to-LiDAR extrinsics are NOT confirmed; cross-sensor fusion is gated."
        )

    inferences_list: List[str] = []
    if world_copy.environment.decision_traces:
        latest_dt = world_copy.environment.decision_traces[-1]
        for inf in latest_dt.inferences_made:
            if inf not in inferences_list:
                inferences_list.append(inf)
        for unk in latest_dt.unknowns_and_uncertainties:
            if unk not in unknowns_list:
                unknowns_list.append(unk)

    provenance = SnapshotEvidenceProvenance(
        real_observation_ids=tuple(real_obs_ids[-max_observation_refs:]),
        simulated_observation_ids=tuple(sim_obs_ids[-max_observation_refs:]),
        stale_observation_ids=tuple(stale_obs_ids[-max_observation_refs:]),
        conflicted_object_ids=tuple(conflicted_ids),
        corroborated_object_ids=tuple(corroborated_ids),
        unconfirmed_coordinate_frames=tuple(sorted(unconfirmed_frames_set)),
        unknowns=tuple(unknowns_list[:20]),
        inferences=tuple(inferences_list[:20]),
        transform_map_validated=world_copy.environment.transform_map_validated,
        extrinsics_camera_lidar_validated=world_copy.environment.extrinsics_camera_lidar_validated,
        free_space_verified_clear=bool(
            health_query.lidar_available
            and world_copy.environment.free_space_coverage_m is not None
        ),
    )

    resolved_decision_ctx: Optional[SnapshotDecisionContext] = None
    if isinstance(decision_context, SnapshotDecisionContext):
        resolved_decision_ctx = decision_context
    elif isinstance(decision_context, dict):
        resolved_decision_ctx = SnapshotDecisionContext.model_validate(decision_context)

    mission_status_str = (
        world_copy.mission.status.value
        if hasattr(world_copy.mission.status, "value")
        else str(world_copy.mission.status)
    )

    return MissionMemorySnapshot(
        snapshot_id=resolved_snap_id,
        mission_id=resolved_mission_id,
        sequence=max(1, int(sequence)),
        timestamp=now_ts,
        timestamp_iso=ts_iso,
        trigger_type=trigger_type,
        trigger_reason=trigger_reason,
        phase=phase,
        associated_event_id=associated_event_id,
        associated_trace_id=associated_trace_id,
        paired_before_snapshot_id=paired_before_snapshot_id,
        mission_status=mission_status_str,
        mission_objective=world_copy.mission.objective,
        task_states=tuple(resolved_tasks),
        active_task_ids=active_map,
        completed_task_ids=completed_ids,
        pending_task_ids=pending_ids,
        vehicles=vehicles_out,
        observation_refs=tuple(obs_refs),
        total_observation_count=total_obs_count,
        tracked_objects=tuple(evaluated_tracked),
        hazards=tuple(
            h.model_copy(deep=True)
            for h in world_copy.environment.hazards[-MAX_SNAPSHOT_HAZARDS:]
        ),
        known_obstacles=tuple(
            pt.model_copy(deep=True)
            for pt in world_copy.environment.known_obstacles[-MAX_SNAPSHOT_OBSTACLES:]
        ),
        explored_areas=tuple(world_copy.environment.explored_areas),
        sensor_health=sensor_health_out,
        evidence_provenance=provenance,
        decision_context=resolved_decision_ctx,
    )


class MemoryStore:
    """Episodic mission memory store capturing immutable World Model snapshots at meaningful transitions.

    Maintains both the legacy `snapshots: List[WorldModel]` interface and Phase 3
    structured `MissionMemorySnapshot` records with bounded per-mission storage.
    """

    def __init__(
        self,
        *,
        max_snapshots_per_mission: int = MAX_SNAPSHOTS_PER_MISSION,
    ) -> None:
        self.snapshots: List[WorldModel] = []
        self._max_snapshots_per_mission = max(10, int(max_snapshots_per_mission))
        self._mission_snapshots: Dict[str, OrderedDict[str, MissionMemorySnapshot]] = {}
        self._snapshots_by_id: Dict[str, MissionMemorySnapshot] = {}
        self._sequence_by_mission: Dict[str, int] = {}
        self._synced_snapshot_ids: set[str] = set()
        self._event_before_snapshot_id: Dict[str, str] = {}
        self._overflow_evicted_count: int = 0

    @property
    def overflow_evicted_count(self) -> int:
        return self._overflow_evicted_count

    def record_snapshot(self, world: WorldModel) -> None:
        """Deep copies and records the current World Model state (legacy interface)."""
        self.snapshots.append(world.model_copy(deep=True))
        if len(self.snapshots) > self._max_snapshots_per_mission:
            del self.snapshots[: len(self.snapshots) - self._max_snapshots_per_mission]

    def get_latest(self) -> Optional[WorldModel]:
        """Returns a deep copy of the most recent World Model snapshot."""
        if not self.snapshots:
            return None
        return self.snapshots[-1].model_copy(deep=True)

    def get_timeline(self) -> List[WorldModel]:
        """Returns the chronological sequence of World Model snapshots."""
        return [s.model_copy(deep=True) for s in self.snapshots]

    def clear(self) -> None:
        """Clears memory for a fresh mission run."""
        self.snapshots.clear()
        self._mission_snapshots.clear()
        self._snapshots_by_id.clear()
        self._sequence_by_mission.clear()
        self._synced_snapshot_ids.clear()
        self._event_before_snapshot_id.clear()

    def record_transition_snapshot(
        self,
        world: WorldModel,
        *,
        mission_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        trigger_type: SnapshotTriggerTypeEnum = SnapshotTriggerTypeEnum.MISSION_CREATED,
        trigger_reason: str = "Mission state transition",
        phase: SnapshotPhaseEnum = SnapshotPhaseEnum.STATE_TRANSITION,
        associated_event_id: Optional[str] = None,
        associated_trace_id: Optional[str] = None,
        paired_before_snapshot_id: Optional[str] = None,
        task_states: Optional[Sequence[SnapshotTaskState | dict[str, Any]]] = None,
        decision_context: Optional[SnapshotDecisionContext | dict[str, Any]] = None,
    ) -> MissionMemorySnapshot:
        """Build and store an immutable `MissionMemorySnapshot` for a meaningful mission transition."""
        resolved_mission_id = (mission_id or world.mission.id or "M001").strip()
        next_seq = self._sequence_by_mission.get(resolved_mission_id, 0) + 1
        self._sequence_by_mission[resolved_mission_id] = next_seq

        if (
            paired_before_snapshot_id is None
            and phase == SnapshotPhaseEnum.AFTER
            and associated_event_id is not None
        ):
            paired_before_snapshot_id = self._event_before_snapshot_id.get(
                associated_event_id
            )

        snap = build_mission_snapshot(
            world,
            mission_id=resolved_mission_id,
            snapshot_id=snapshot_id,
            sequence=next_seq,
            timestamp=timestamp,
            trigger_type=trigger_type,
            trigger_reason=trigger_reason,
            phase=phase,
            associated_event_id=associated_event_id,
            associated_trace_id=associated_trace_id,
            paired_before_snapshot_id=paired_before_snapshot_id,
            task_states=task_states,
            decision_context=decision_context,
        )

        mission_bucket = self._mission_snapshots.setdefault(
            resolved_mission_id, OrderedDict()
        )
        if len(mission_bucket) >= self._max_snapshots_per_mission:
            oldest_id, _ = mission_bucket.popitem(last=False)
            self._snapshots_by_id.pop(oldest_id, None)
            self._synced_snapshot_ids.discard(oldest_id)
            self._overflow_evicted_count += 1

        mission_bucket[snap.snapshot_id] = snap
        self._snapshots_by_id[snap.snapshot_id] = snap

        if phase == SnapshotPhaseEnum.BEFORE and associated_event_id is not None:
            self._event_before_snapshot_id[associated_event_id] = snap.snapshot_id

        # Keep legacy `self.snapshots` updated as well
        self.record_snapshot(world)
        return snap

    def get_before_snapshot_id_for_event(self, event_id: Optional[str]) -> Optional[str]:
        """Return the snapshot_id captured when `event_id` was routed (`BEFORE` state)."""
        if not event_id:
            return None
        return self._event_before_snapshot_id.get(event_id)

    def get_snapshot_by_id(self, snapshot_id: Optional[str]) -> Optional[MissionMemorySnapshot]:
        """Retrieve an immutable `MissionMemorySnapshot` by ID."""
        if not snapshot_id:
            return None
        snap = self._snapshots_by_id.get(snapshot_id)
        return snap.model_copy(deep=True) if snap is not None else None

    def get_mission_snapshots(
        self,
        mission_id: Optional[str] = None,
    ) -> List[MissionMemorySnapshot]:
        """Return chronological immutable `MissionMemorySnapshot` records for `mission_id`."""
        if mission_id is not None:
            bucket = self._mission_snapshots.get(mission_id)
            if not bucket:
                return []
            return [s.model_copy(deep=True) for s in bucket.values()]

        all_snaps: List[MissionMemorySnapshot] = []
        for bucket in self._mission_snapshots.values():
            all_snaps.extend(s.model_copy(deep=True) for s in bucket.values())
        all_snaps.sort(key=lambda item: (item.timestamp, item.sequence, item.snapshot_id))
        return all_snaps

    def get_unsynced_snapshots(
        self,
        mission_id: Optional[str] = None,
    ) -> List[MissionMemorySnapshot]:
        """Return snapshots not yet persisted to SQLite."""
        candidates = self.get_mission_snapshots(mission_id)
        return [
            s for s in candidates if s.snapshot_id not in self._synced_snapshot_ids
        ]

    def mark_snapshot_synced(self, snapshot_id: str) -> None:
        """Mark a snapshot ID as persisted to SQLite."""
        self._synced_snapshot_ids.add(snapshot_id)
