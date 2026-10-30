"""Structured, read-only World Model retrieval tools for ULTRON (`ultron/world_model/tools.py` — Section 6).

Provides deterministic, read-only query functions over `WorldModel`:
- `get_environment_snapshot(world, current_time=None)`
- `get_detected_objects(world, include_stale=True, label_filter=None, current_time=None)`
- `get_object_details(world, object_id, current_time=None)`
- `get_nearby_obstacles(world, reference_position, radius, coordinate_frame="map", current_time=None)`
- `get_free_space(world, reference_position=None, coordinate_frame="map", current_time=None)`
- `get_sensor_health(world, sensor_id=None, current_time=None)`
- `get_observation_history(world, object_id=None, limit=50)`
- `get_vehicle_pose(world, vehicle_id="SCOUT01")`
- `get_environment_changes(world, since_timestamp, current_time=None)`

Guarantees:
- Never mutates the `WorldModel` (operates on a deep copy / read-only view).
- Never fabricates defaults that look like real observations.
- Never claims an area is clear/free unless active, fresh sensor coverage supports that conclusion.
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from ultron.config import config as ultron_config
from ultron.schemas.commands import Position2D
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    Position3DMeasurement,
    SensorObservation,
)
from ultron.schemas.world import (
    SensorHealthReport,
    SensorHealthStatusEnum,
    TrackedObject,
    TrackingStatusEnum,
    WorldModel,
)
from ultron.world_model.fusion import euclidean_distance_3d


class VehiclePoseResult(BaseModel):
    """Structured vehicle pose and validity readout."""

    vehicle_id: str
    found: bool
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    position_alt: Optional[float] = None
    heading: Optional[float] = None
    coordinate_frame: str = "map"
    pose_valid: bool = False
    connection: str = "UNKNOWN"
    status: str = "UNKNOWN"
    battery: Optional[float] = None
    mode: str = "simulated"
    epistemic_status: EpistemicStatusEnum = EpistemicStatusEnum.UNKNOWN
    last_heartbeat_timestamp: Optional[float] = None
    warning: Optional[str] = None


class ObjectDetailsResult(BaseModel):
    """Structured details for a single tracked object."""

    found: bool
    object_id: str
    object: Optional[TrackedObject] = None
    observations: List[SensorObservation] = Field(default_factory=list)
    is_stale: bool = False
    age_seconds: Optional[float] = None
    epistemic_status: EpistemicStatusEnum = EpistemicStatusEnum.UNKNOWN
    warning: Optional[str] = None


class DetectedObjectsResult(BaseModel):
    """Structured list of tracked objects in the World Model."""

    timestamp: float
    total_count: int
    active_count: int
    stale_count: int
    conflicted_count: int
    corroborated_count: int
    objects: List[TrackedObject] = Field(default_factory=list)
    sensor_coverage_available: bool = False
    warnings: List[str] = Field(default_factory=list)


class NearbyObstaclesResult(BaseModel):
    """Structured query result for obstacles within `radius` of `reference_position`."""

    reference_position: Position3DMeasurement
    coordinate_frame: str
    radius_m: float
    sensor_coverage_sufficient: bool
    epistemic_status: EpistemicStatusEnum
    obstacles: List[TrackedObject] = Field(default_factory=list)
    legacy_map_obstacles: List[Position2D] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class FreeSpaceResult(BaseModel):
    """Structured free-space assessment around a reference position.

    Never claims space is clear (`is_verified_clear=True`) unless active, fresh
    geometric sensor coverage (such as LiDAR) explicitly supports that conclusion.
    """

    reference_position: Position3DMeasurement
    coordinate_frame: str
    is_verified_clear: bool = False
    free_space_radius_m: Optional[float] = None
    epistemic_status: EpistemicStatusEnum = EpistemicStatusEnum.UNKNOWN
    supporting_sensors: List[str] = Field(default_factory=list)
    reason: str
    warnings: List[str] = Field(default_factory=list)


class SensorHealthQueryResult(BaseModel):
    """Structured sensor health report across all or a specific sensor."""

    timestamp: float
    all_healthy: bool
    any_sensor_available: bool
    lidar_available: bool
    camera_available: bool
    sensors: Dict[str, SensorHealthReport] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


class ObservationHistoryResult(BaseModel):
    """Structured chronological observation history."""

    object_id: Optional[str] = None
    count: int
    observations: List[SensorObservation] = Field(default_factory=list)


class EnvironmentChangesResult(BaseModel):
    """Structured summary of environmental updates since `since_timestamp`."""

    since_timestamp: float
    current_timestamp: float
    has_changes: bool
    new_observations: List[SensorObservation] = Field(default_factory=list)
    updated_objects: List[TrackedObject] = Field(default_factory=list)
    new_hazards_count: int = 0
    sensor_warnings: List[str] = Field(default_factory=list)


class EnvironmentSnapshotResult(BaseModel):
    """Comprehensive read-only World Model snapshot for ULTRON planning & reasoning."""

    timestamp: float
    mission_id: str
    mission_objective: str
    mission_status: str
    can_assess_from_live_sensors: bool
    transform_map_validated: bool
    extrinsics_camera_lidar_validated: bool
    vehicles: Dict[str, VehiclePoseResult] = Field(default_factory=dict)
    sensor_health: SensorHealthQueryResult
    detected_objects: DetectedObjectsResult
    free_space: FreeSpaceResult
    explored_areas: List[str] = Field(default_factory=list)
    known_obstacles_count: int = 0
    unhandled_hazards_count: int = 0
    warnings: List[str] = Field(default_factory=list)


def _resolve_now(world: WorldModel, current_time: Optional[float] = None) -> float:
    """Resolve evaluation timestamp, using world.environment.last_updated_timestamp for synthetic test epochs."""
    if current_time is not None:
        return current_time
    if (
        world.environment.last_updated_timestamp is not None
        and world.environment.last_updated_timestamp < 1_000_000_000.0
    ):
        return world.environment.last_updated_timestamp
    return time.time()


def _evaluate_tracked_copy(
    obj: TrackedObject,
    now: float,
    stale_after_sec: float,
) -> TrackedObject:
    copy_obj = obj.model_copy(deep=True)
    age = now - copy_obj.last_observed_timestamp
    if age > stale_after_sec:
        copy_obj.currently_visible = False
        if copy_obj.tracking_status != TrackingStatusEnum.CONFLICTED:
            copy_obj.tracking_status = TrackingStatusEnum.STALE
        if copy_obj.epistemic_status != EpistemicStatusEnum.CONFLICTED:
            copy_obj.epistemic_status = EpistemicStatusEnum.STALE
    return copy_obj


def get_sensor_health(
    world: WorldModel,
    sensor_id: Optional[str] = None,
    *,
    current_time: Optional[float] = None,
) -> SensorHealthQueryResult:
    """Retrieve structured health, freshness, and availability for sensors (§6)."""
    now = _resolve_now(world, current_time)
    sensors_out: Dict[str, SensorHealthReport] = {}
    warnings: List[str] = []

    target_items = (
        [(sensor_id, world.environment.sensor_health[sensor_id])]
        if (sensor_id and sensor_id in world.environment.sensor_health)
        else list(world.environment.sensor_health.items())
    )

    if sensor_id and sensor_id not in world.environment.sensor_health:
        warnings.append(f"Requested sensor '{sensor_id}' is not registered in the World Model.")

    for sid, report in target_items:
        rep_copy = report.model_copy(deep=True)
        if not rep_copy.enabled:
            if rep_copy.sensor_id == "lidar" or not rep_copy.topic:
                rep_copy.status = SensorHealthStatusEnum.UNAVAILABLE
            elif rep_copy.last_ingested_at is None:
                rep_copy.status = SensorHealthStatusEnum.DISCONNECTED
        elif rep_copy.last_ingested_at is not None:
            age = now - rep_copy.last_ingested_at
            if age > rep_copy.stale_after_sec:
                rep_copy.is_stale = True
                if rep_copy.status not in (
                    SensorHealthStatusEnum.DISCONNECTED,
                    SensorHealthStatusEnum.ERROR,
                    SensorHealthStatusEnum.UNAVAILABLE,
                ):
                    rep_copy.status = SensorHealthStatusEnum.STALE

        if rep_copy.status == SensorHealthStatusEnum.UNAVAILABLE:
            warnings.append(
                f"Sensor '{sid}' ({rep_copy.sensor_type.value}) is UNAVAILABLE: {rep_copy.notes or 'not configured'}."
            )
        elif rep_copy.status == SensorHealthStatusEnum.DISCONNECTED:
            warnings.append(
                f"Sensor '{sid}' ({rep_copy.sensor_type.value}) is DISCONNECTED; no active data stream."
            )
        elif rep_copy.status == SensorHealthStatusEnum.STALE or rep_copy.is_stale:
            warnings.append(
                f"Sensor '{sid}' ({rep_copy.sensor_type.value}) data is STALE "
                f"(last message at {rep_copy.last_message_timestamp})."
            )
        elif rep_copy.status == SensorHealthStatusEnum.ERROR:
            warnings.append(
                f"Sensor '{sid}' ({rep_copy.sensor_type.value}) is in ERROR state: {rep_copy.error_state}."
            )

        sensors_out[sid] = rep_copy

    cam = sensors_out.get("depth_camera")
    lidar = sensors_out.get("lidar")
    camera_available = bool(
        cam
        and cam.status in (SensorHealthStatusEnum.CONNECTED, SensorHealthStatusEnum.SIMULATED)
        and not cam.is_stale
    )
    lidar_available = bool(
        lidar
        and lidar.status in (SensorHealthStatusEnum.CONNECTED, SensorHealthStatusEnum.SIMULATED)
        and not lidar.is_stale
    )
    any_available = camera_available or lidar_available
    all_healthy = bool(sensors_out) and all(
        s.status in (SensorHealthStatusEnum.CONNECTED, SensorHealthStatusEnum.SIMULATED)
        and not s.is_stale
        for s in sensors_out.values()
    )

    return SensorHealthQueryResult(
        timestamp=now,
        all_healthy=all_healthy,
        any_sensor_available=any_available,
        lidar_available=lidar_available,
        camera_available=camera_available,
        sensors=sensors_out,
        warnings=warnings,
    )


def get_detected_objects(
    world: WorldModel,
    *,
    include_stale: bool = True,
    label_filter: Optional[str] = None,
    current_time: Optional[float] = None,
) -> DetectedObjectsResult:
    """Retrieve all tracked objects in the World Model with freshness and provenance (§6)."""
    now = _resolve_now(world, current_time)
    stale_thresh = ultron_config.camera_stale_after_sec
    health = get_sensor_health(world, current_time=now)

    evaluated: List[TrackedObject] = []
    active_count = 0
    stale_count = 0
    conflicted_count = 0
    corroborated_count = 0

    for obj in world.environment.tracked_objects.values():
        copy_obj = _evaluate_tracked_copy(obj, now, stale_thresh)
        if label_filter and copy_obj.label.lower() != label_filter.strip().lower():
            continue
        is_stale = copy_obj.epistemic_status == EpistemicStatusEnum.STALE or copy_obj.tracking_status == TrackingStatusEnum.STALE
        if is_stale and not include_stale:
            continue

        if copy_obj.epistemic_status == EpistemicStatusEnum.CONFLICTED:
            conflicted_count += 1
        elif is_stale:
            stale_count += 1
        else:
            active_count += 1

        if copy_obj.corroborated_by_multiple_sensors:
            corroborated_count += 1

        evaluated.append(copy_obj)

    evaluated.sort(key=lambda item: item.object_id)
    warnings = list(health.warnings)
    if not health.lidar_available:
        warnings.append(
            "LiDAR corroboration is unavailable; camera detections are single-sensor observations."
        )

    return DetectedObjectsResult(
        timestamp=now,
        total_count=len(evaluated),
        active_count=active_count,
        stale_count=stale_count,
        conflicted_count=conflicted_count,
        corroborated_count=corroborated_count,
        objects=evaluated,
        sensor_coverage_available=health.any_sensor_available,
        warnings=warnings,
    )


def get_object_details(
    world: WorldModel,
    object_id: str,
    *,
    current_time: Optional[float] = None,
) -> ObjectDetailsResult:
    """Retrieve full provenance, observations, and epistemic status for a specific tracked object (§6)."""
    now = _resolve_now(world, current_time)
    stale_thresh = ultron_config.camera_stale_after_sec

    raw_obj = world.environment.tracked_objects.get(object_id)
    if raw_obj is None:
        for candidate in world.environment.tracked_objects.values():
            if candidate.label.lower() == object_id.strip().lower():
                raw_obj = candidate
                break

    if raw_obj is None:
        return ObjectDetailsResult(
            found=False,
            object_id=object_id,
            object=None,
            observations=[],
            is_stale=False,
            age_seconds=None,
            epistemic_status=EpistemicStatusEnum.UNKNOWN,
            warning=f"Object '{object_id}' does not exist in the World Model.",
        )

    copy_obj = _evaluate_tracked_copy(raw_obj, now, stale_thresh)
    age = round(max(0.0, now - copy_obj.last_observed_timestamp), 3)
    is_stale = copy_obj.epistemic_status == EpistemicStatusEnum.STALE

    obs_set = set(copy_obj.contributing_observation_ids)
    related_obs = [
        o.model_copy(deep=True)
        for o in world.environment.observation_history
        if o.observation_id in obs_set
    ]

    warning: Optional[str] = None
    if copy_obj.epistemic_status == EpistemicStatusEnum.CONFLICTED:
        warning = copy_obj.conflict_details or "Conflicting sensor observations detected."
    elif is_stale:
        warning = (
            f"Object '{copy_obj.object_id}' ({copy_obj.label}) observation is STALE "
            f"(age {age:.2f}s > {stale_thresh:.1f}s); no longer confirmed at last position."
        )

    return ObjectDetailsResult(
        found=True,
        object_id=copy_obj.object_id,
        object=copy_obj,
        observations=related_obs,
        is_stale=is_stale,
        age_seconds=age,
        epistemic_status=copy_obj.epistemic_status,
        warning=warning,
    )


def get_nearby_obstacles(
    world: WorldModel,
    reference_position: Position2D | Position3DMeasurement,
    radius: float,
    *,
    coordinate_frame: str = "map",
    current_time: Optional[float] = None,
) -> NearbyObstaclesResult:
    """Retrieve obstacles within `radius` meters of `reference_position` in `coordinate_frame` (§6)."""
    now = _resolve_now(world, current_time)
    stale_thresh = ultron_config.camera_stale_after_sec
    ref_3d = (
        reference_position
        if isinstance(reference_position, Position3DMeasurement)
        else Position3DMeasurement(x=reference_position.x, y=reference_position.y, z=0.0)
    )

    health = get_sensor_health(world, current_time=now)
    warnings: List[str] = []
    matched: List[TrackedObject] = []

    for obj in world.environment.tracked_objects.values():
        copy_obj = _evaluate_tracked_copy(obj, now, stale_thresh)
        if not (copy_obj.is_obstacle or copy_obj.is_hazard or copy_obj.label.lower() in {"chair", "box", "bench", "obstacle", "wall", "table"}):
            continue
        if copy_obj.coordinate_frame != coordinate_frame:
            warnings.append(
                f"Obstacle '{copy_obj.object_id}' ({copy_obj.label}) is in frame "
                f"'{copy_obj.coordinate_frame}', which cannot be directly compared to '{coordinate_frame}' without a validated transform."
            )
            continue
        dist = euclidean_distance_3d(ref_3d, copy_obj.position)
        if dist <= (radius + copy_obj.position_uncertainty_m):
            matched.append(copy_obj)

    legacy_matched: List[Position2D] = []
    if coordinate_frame == "map":
        for pt in world.environment.known_obstacles:
            if math.hypot(pt.x - ref_3d.x, pt.y - ref_3d.y) <= radius:
                legacy_matched.append(pt.model_copy(deep=True))

    if not health.any_sensor_available and not matched and not legacy_matched:
        warnings.append(
            "No active perception sensors are online; absence of obstacle records does NOT guarantee clear space."
        )
        epistemic = EpistemicStatusEnum.UNKNOWN
    elif any(m.epistemic_status == EpistemicStatusEnum.CONFLICTED for m in matched):
        epistemic = EpistemicStatusEnum.CONFLICTED
    elif any(m.epistemic_status == EpistemicStatusEnum.CORROBORATED for m in matched):
        epistemic = EpistemicStatusEnum.CORROBORATED
    elif matched or legacy_matched:
        if all(m.epistemic_status == EpistemicStatusEnum.STALE for m in matched) and not legacy_matched:
            epistemic = EpistemicStatusEnum.STALE
        else:
            epistemic = EpistemicStatusEnum.OBSERVED
    else:
        epistemic = (
            EpistemicStatusEnum.OBSERVED
            if health.lidar_available
            else EpistemicStatusEnum.UNKNOWN
        )

    return NearbyObstaclesResult(
        reference_position=ref_3d,
        coordinate_frame=coordinate_frame,
        radius_m=radius,
        sensor_coverage_sufficient=health.lidar_available,
        epistemic_status=epistemic,
        obstacles=matched,
        legacy_map_obstacles=legacy_matched,
        warnings=warnings,
    )


def get_free_space(
    world: WorldModel,
    reference_position: Optional[Position2D | Position3DMeasurement] = None,
    *,
    coordinate_frame: str = "map",
    current_time: Optional[float] = None,
) -> FreeSpaceResult:
    """Assess verified free space around `reference_position` (§4, §6, §7)."""
    now = _resolve_now(world, current_time)
    if reference_position is None:
        scout = world.vehicles.get("SCOUT01")
        ref_3d = (
            Position3DMeasurement(x=scout.position.x, y=scout.position.y, z=0.0)
            if scout is not None
            else Position3DMeasurement(x=0.0, y=0.0, z=0.0)
        )
    elif isinstance(reference_position, Position2D):
        ref_3d = Position3DMeasurement(x=reference_position.x, y=reference_position.y, z=0.0)
    else:
        ref_3d = reference_position

    health = get_sensor_health(world, current_time=now)
    warnings = list(health.warnings)

    if not health.lidar_available or world.environment.free_space_coverage_m is None:
        return FreeSpaceResult(
            reference_position=ref_3d,
            coordinate_frame=coordinate_frame,
            is_verified_clear=False,
            free_space_radius_m=None,
            epistemic_status=EpistemicStatusEnum.UNKNOWN,
            supporting_sensors=(["depth_camera"] if health.camera_available else []),
            reason=(
                "Cannot verify free space: 360° LiDAR geometric coverage is UNAVAILABLE or unconfirmed. "
                "Absence of camera detections does not prove an area is clear."
            ),
            warnings=warnings,
        )

    nearby = get_nearby_obstacles(
        world,
        ref_3d,
        radius=world.environment.free_space_coverage_m,
        coordinate_frame=coordinate_frame,
        current_time=now,
    )
    if nearby.obstacles or nearby.legacy_map_obstacles:
        return FreeSpaceResult(
            reference_position=ref_3d,
            coordinate_frame=coordinate_frame,
            is_verified_clear=False,
            free_space_radius_m=world.environment.free_space_coverage_m,
            epistemic_status=EpistemicStatusEnum.OBSERVED,
            supporting_sensors=["lidar"] + (["depth_camera"] if health.camera_available else []),
            reason=(
                f"Obstacles detected within {world.environment.free_space_coverage_m:.2f}m "
                f"in frame '{coordinate_frame}'."
            ),
            warnings=warnings + nearby.warnings,
        )

    return FreeSpaceResult(
        reference_position=ref_3d,
        coordinate_frame=coordinate_frame,
        is_verified_clear=True,
        free_space_radius_m=world.environment.free_space_coverage_m,
        epistemic_status=EpistemicStatusEnum.OBSERVED,
        supporting_sensors=["lidar"] + (["depth_camera"] if health.camera_available else []),
        reason=(
            f"Verified free space up to {world.environment.free_space_coverage_m:.2f}m "
            f"supported by active LiDAR scan in frame '{coordinate_frame}'."
        ),
        warnings=warnings,
    )


def get_observation_history(
    world: WorldModel,
    object_id: Optional[str] = None,
    *,
    limit: int = 50,
) -> ObservationHistoryResult:
    """Retrieve chronological `SensorObservation` records, optionally filtered by `object_id` (§6)."""
    if object_id is not None:
        tracked = world.environment.tracked_objects.get(object_id)
        if tracked is None:
            return ObservationHistoryResult(
                object_id=object_id,
                count=0,
                observations=[],
            )
        obs_ids = set(tracked.contributing_observation_ids)
        filtered = [
            o.model_copy(deep=True)
            for o in world.environment.observation_history
            if o.observation_id in obs_ids
        ]
    else:
        filtered = [
            o.model_copy(deep=True)
            for o in world.environment.observation_history
        ]

    sliced = filtered[-max(1, limit):]
    return ObservationHistoryResult(
        object_id=object_id,
        count=len(sliced),
        observations=sliced,
    )


def get_vehicle_pose(
    world: WorldModel,
    vehicle_id: str = "SCOUT01",
) -> VehiclePoseResult:
    """Retrieve vehicle pose, connection state, and pose validity (§6)."""
    v = world.vehicles.get(vehicle_id)
    if v is None:
        return VehiclePoseResult(
            vehicle_id=vehicle_id,
            found=False,
            pose_valid=False,
            epistemic_status=EpistemicStatusEnum.UNKNOWN,
            warning=f"Vehicle '{vehicle_id}' is not present in the World Model.",
        )

    conn_str = v.connection.value if hasattr(v.connection, "value") else str(v.connection)
    mode_str = v.mode.value if hasattr(v.mode, "value") else str(v.mode)
    pose_valid = getattr(v, "pose_valid", True) and conn_str == "ONLINE"
    alt = getattr(v.position, "alt", 0.0)
    heading = getattr(v, "heading", 0.0)

    warning: Optional[str] = None
    if not pose_valid:
        warning = (
            f"Vehicle '{vehicle_id}' connection is {conn_str}; pose cannot be verified from live telemetry."
        )

    return VehiclePoseResult(
        vehicle_id=vehicle_id,
        found=True,
        position_x=v.position.x,
        position_y=v.position.y,
        position_alt=alt,
        heading=heading,
        coordinate_frame=getattr(v, "coordinate_frame", "map"),
        pose_valid=pose_valid,
        connection=conn_str,
        status=v.status,
        battery=v.battery,
        mode=mode_str,
        epistemic_status=(
            EpistemicStatusEnum.OBSERVED if pose_valid else EpistemicStatusEnum.UNKNOWN
        ),
        last_heartbeat_timestamp=getattr(v, "last_heartbeat_timestamp", None),
        warning=warning,
    )


def get_environment_changes(
    world: WorldModel,
    since_timestamp: float,
    *,
    current_time: Optional[float] = None,
) -> EnvironmentChangesResult:
    """Retrieve all observations and tracked object changes since `since_timestamp` (§6)."""
    now = _resolve_now(world, current_time)
    stale_thresh = ultron_config.camera_stale_after_sec

    new_obs = [
        o.model_copy(deep=True)
        for o in world.environment.observation_history
        if o.sensor_timestamp > since_timestamp or o.ingested_at > since_timestamp
    ]
    updated_objs = [
        _evaluate_tracked_copy(obj, now, stale_thresh)
        for obj in world.environment.tracked_objects.values()
        if obj.last_observed_timestamp > since_timestamp or obj.last_ingested_at > since_timestamp
    ]
    new_hazards = sum(
        1
        for h in world.environment.hazards
        if (h.timestamp is not None and h.timestamp > since_timestamp)
        or not getattr(h, "handled", False)
    )
    health = get_sensor_health(world, current_time=now)

    return EnvironmentChangesResult(
        since_timestamp=since_timestamp,
        current_timestamp=now,
        has_changes=bool(new_obs or updated_objs or new_hazards > 0),
        new_observations=new_obs,
        updated_objects=updated_objs,
        new_hazards_count=new_hazards,
        sensor_warnings=health.warnings,
    )


def get_environment_snapshot(
    world: WorldModel,
    *,
    current_time: Optional[float] = None,
) -> EnvironmentSnapshotResult:
    """Retrieve a complete, structured, read-only environmental intelligence snapshot (§6)."""
    now = _resolve_now(world, current_time)
    health = get_sensor_health(world, current_time=now)
    detected = get_detected_objects(world, include_stale=True, current_time=now)
    free_space = get_free_space(world, current_time=now)
    vehicles_out = {
        vid: get_vehicle_pose(world, vid)
        for vid in world.vehicles
    }

    unhandled_hazards = sum(
        1 for h in world.environment.hazards if not getattr(h, "handled", False)
    )

    warnings = list(health.warnings)
    if not world.environment.transform_map_validated:
        warnings.append(
            "Global map transform (map -> odom -> oak_parent_frame) is NOT confirmed; camera detections remain in camera-relative frame."
        )
    if not world.environment.extrinsics_camera_lidar_validated:
        warnings.append(
            "Camera-to-LiDAR extrinsics are NOT confirmed; cross-sensor fusion is gated."
        )

    return EnvironmentSnapshotResult(
        timestamp=now,
        mission_id=world.mission.id,
        mission_objective=world.mission.objective,
        mission_status=world.mission.status.value if hasattr(world.mission.status, "value") else str(world.mission.status),
        can_assess_from_live_sensors=health.any_sensor_available or detected.total_count > 0,
        transform_map_validated=world.environment.transform_map_validated,
        extrinsics_camera_lidar_validated=world.environment.extrinsics_camera_lidar_validated,
        vehicles=vehicles_out,
        sensor_health=health,
        detected_objects=detected,
        free_space=free_space,
        explored_areas=list(world.environment.explored_areas),
        known_obstacles_count=len(world.environment.known_obstacles),
        unhandled_hazards_count=unhandled_hazards,
        warnings=warnings,
    )


class WorldModelTools:
    """Bound read-only toolset over a `WorldModel` instance for ULTRON planner/replanner nodes."""

    def __init__(self, world: WorldModel, *, current_time: Optional[float] = None) -> None:
        self._world = world
        self._current_time = _resolve_now(world, current_time)

    def get_environment_snapshot(self) -> EnvironmentSnapshotResult:
        return get_environment_snapshot(self._world, current_time=self._current_time)

    def get_detected_objects(
        self,
        *,
        include_stale: bool = True,
        label_filter: Optional[str] = None,
    ) -> DetectedObjectsResult:
        return get_detected_objects(
            self._world,
            include_stale=include_stale,
            label_filter=label_filter,
            current_time=self._current_time,
        )

    def get_object_details(self, object_id: str) -> ObjectDetailsResult:
        return get_object_details(
            self._world,
            object_id,
            current_time=self._current_time,
        )

    def get_nearby_obstacles(
        self,
        reference_position: Position2D | Position3DMeasurement,
        radius: float,
        coordinate_frame: str = "map",
    ) -> NearbyObstaclesResult:
        return get_nearby_obstacles(
            self._world,
            reference_position,
            radius,
            coordinate_frame=coordinate_frame,
            current_time=self._current_time,
        )

    def get_free_space(
        self,
        reference_position: Optional[Position2D | Position3DMeasurement] = None,
        coordinate_frame: str = "map",
    ) -> FreeSpaceResult:
        return get_free_space(
            self._world,
            reference_position,
            coordinate_frame=coordinate_frame,
            current_time=self._current_time,
        )

    def get_sensor_health(
        self,
        sensor_id: Optional[str] = None,
    ) -> SensorHealthQueryResult:
        return get_sensor_health(
            self._world,
            sensor_id,
            current_time=self._current_time,
        )

    def get_observation_history(
        self,
        object_id: Optional[str] = None,
        limit: int = 50,
    ) -> ObservationHistoryResult:
        return get_observation_history(
            self._world,
            object_id,
            limit=limit,
        )

    def get_vehicle_pose(self, vehicle_id: str = "SCOUT01") -> VehiclePoseResult:
        return get_vehicle_pose(self._world, vehicle_id)

    def get_environment_changes(
        self,
        since_timestamp: float,
    ) -> EnvironmentChangesResult:
        return get_environment_changes(
            self._world,
            since_timestamp,
            current_time=self._current_time,
        )
