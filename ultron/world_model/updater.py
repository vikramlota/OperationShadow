"""Deterministic World Model updater, persistent object tracker, and multi-sensor fusion integration (`ultron/world_model/updater.py` — Sections 4, 5).

Responsibilities:
1. Ingest both `SensorObservation` and legacy `StructuredObservation` objects into
   the shared `WorldModel`.
2. Maintain persistent `TrackedObject` identities (`OBJ-001`, `OBJ-002`, ...) across
   frames without confusing a detector's frame-local `detector_object_id` with a
   global real-world identity.
3. Associate new observations with existing `TrackedObject` records in the same
   coordinate frame (or across frames when a validated transform exists in
   `FrameTransformRegistry`).
4. Run `evaluate_camera_lidar_fusion` when both camera and LiDAR observations are
   present:
   - Only marks `CORROBORATED` when timestamp alignment, frame transform, and
     camera-to-LiDAR extrinsics are validated and spatial uncertainty gates pass.
   - Preserves conflicting measurements and marks the object `CONFLICTED` without
     averaging incompatible positions.
5. Refresh object and sensor staleness (`refresh_world_model_freshness`) so old
   observations are explicitly marked `STALE` and `currently_visible=False`.
"""

from __future__ import annotations

import math
import time
from typing import Optional
import uuid

from ultron.config import config as ultron_config
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import VehicleID
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    ObservationValidationStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorSourceModeEnum,
    SensorTypeEnum,
    StructuredObservation,
)
from ultron.schemas.world import (
    HazardEntity,
    ObjectEntity,
    SensorHealthStatusEnum,
    TrackedObject,
    TrackingStatusEnum,
    WorldModel,
)
from ultron.world_model.fusion import (
    FrameTransformRegistry,
    angular_separation_rad,
    euclidean_distance_3d,
    evaluate_camera_lidar_fusion,
)


def _next_tracked_object_id(world: WorldModel) -> str:
    idx = len(world.environment.tracked_objects) + 1
    while f"OBJ-{idx:03d}" in world.environment.tracked_objects:
        idx += 1
    return f"OBJ-{idx:03d}"


def structured_to_sensor_observation(obs: StructuredObservation) -> SensorObservation:
    """Convert a `StructuredObservation` into a rich `SensorObservation`."""
    now = time.time()
    stamp = obs.timestamp if (obs.timestamp is not None and obs.timestamp > 0) else now
    ingested = obs.ingested_at if (obs.ingested_at is not None and obs.ingested_at > 0) else now
    obs_id = obs.observation_id or f"OBS-{uuid.uuid4().hex[:8].upper()}"
    vid_str = obs.source_vehicle.value if hasattr(obs.source_vehicle, "value") else str(obs.source_vehicle)

    ev = obs.event.lower()
    if ev.startswith("detection:"):
        label = ev.split(":", 1)[1].strip() or (obs.visual_evidence or "unknown_object")
    elif ev == "possible_hazard" or "hazard" in ev:
        label = obs.visual_evidence or "possible_hazard"
    elif ev == "obstacle_blocked" or "obstacle" in ev:
        label = obs.visual_evidence or "obstacle"
    else:
        label = obs.visual_evidence or obs.event

    is_hazard = ev == "possible_hazard" or "hazard" in ev or label.lower() in {
        "fire",
        "smoke",
        "hazard",
        "thermal_anomaly",
    }
    is_obstacle = ev == "obstacle_blocked" or "obstacle" in ev or label.lower() in {
        "chair",
        "bench",
        "box",
        "obstacle",
        "lidar_obstacle",
        "wall",
        "barrier",
        "table",
        "dining table",
        "diningtable",
        "couch",
        "sofa",
        "potted plant",
        "pottedplant",
        "bed",
        "tv",
        "tvmonitor",
        "laptop",
        "backpack",
        "suitcase",
        "handbag",
        "bottle",
        "cup",
        "trash can",
        "cone",
        "pillar",
        "column",
        "door",
        "cabinet",
        "refrigerator",
        "person",
        "bicycle",
        "dog",
        "cat",
        "cart",
    }

    return SensorObservation(
        observation_id=obs_id,
        sensor_id=obs.sensor_id,
        sensor_type=obs.sensor_type,
        vehicle_id=vid_str,
        detector_object_id=obs.detector_object_id,
        sensor_timestamp=stamp,
        ingested_at=ingested,
        coordinate_frame=obs.coordinate_frame or "map",
        position=Position3DMeasurement(
            x=obs.location.x,
            y=obs.location.y,
            z=obs.position_z,
            range_m=obs.range_m,
            uncertainty_m=0.15,
        ),
        classification=label,
        confidence=obs.confidence,
        raw_reference=obs.raw_reference,
        validation_status=ObservationValidationStatusEnum.VALIDATED,
        source_mode=obs.source_mode,
        is_obstacle=is_obstacle,
        is_hazard=is_hazard,
    )


def ingest_sensor_observation(
    world: WorldModel,
    obs: SensorObservation,
    *,
    transform_registry: Optional[FrameTransformRegistry] = None,
    association_distance_m: Optional[float] = None,
    fusion_max_time_diff_sec: Optional[float] = None,
    fusion_association_distance_m: Optional[float] = None,
    fusion_conflict_distance_m: Optional[float] = None,
) -> WorldModel:
    """Ingest a validated `SensorObservation`, update sensor health, associate/track objects, and run fusion."""
    if obs.validation_status != ObservationValidationStatusEnum.VALIDATED:
        return world

    # Deduplicate by observation_id
    if any(existing.observation_id == obs.observation_id for existing in world.environment.observation_history):
        return world

    registry = transform_registry or FrameTransformRegistry(
        map_frame_confirmed=world.environment.transform_map_validated,
        camera_lidar_extrinsics_confirmed=world.environment.extrinsics_camera_lidar_validated,
    )
    assoc_gate = (
        association_distance_m
        if association_distance_m is not None
        else ultron_config.tracking_association_distance_m
    )
    max_dt = (
        fusion_max_time_diff_sec
        if fusion_max_time_diff_sec is not None
        else ultron_config.fusion_max_time_diff_sec
    )
    fusion_gate = (
        fusion_association_distance_m
        if fusion_association_distance_m is not None
        else ultron_config.fusion_association_distance_m
    )
    conflict_gate = (
        fusion_conflict_distance_m
        if fusion_conflict_distance_m is not None
        else ultron_config.fusion_conflict_distance_m
    )

    # 1. Record in observation history (with bounded retention to prevent unbounded RAM growth)
    world.environment.observation_history.append(obs)
    if len(world.environment.observation_history) > 500:
        world.environment.observation_history = world.environment.observation_history[-500:]
    world.environment.last_updated_timestamp = max(
        world.environment.last_updated_timestamp or 0.0,
        obs.ingested_at,
    )

    # 2. Update sensor health entry
    health_key = (
        "depth_camera"
        if obs.sensor_type == SensorTypeEnum.DEPTH_CAMERA
        else ("lidar" if obs.sensor_type == SensorTypeEnum.LIDAR else obs.sensor_id)
    )
    if health_key in world.environment.sensor_health:
        sh = world.environment.sensor_health[health_key]
        sh.enabled = True
        sh.source_mode = obs.source_mode
        sh.coordinate_frame = obs.coordinate_frame
        if obs.vehicle_id:
            sh.vehicle_id = obs.vehicle_id
        sh.last_message_timestamp = obs.sensor_timestamp
        sh.last_ingested_at = obs.ingested_at
        sh.valid_message_count += 1
        sh.is_stale = (
            False
            if obs.source_mode == SensorSourceModeEnum.PHYSICAL
            else (obs.ingested_at - obs.sensor_timestamp) > sh.stale_after_sec
        )
        sh.status = (
            SensorHealthStatusEnum.STALE
            if sh.is_stale
            else (
                SensorHealthStatusEnum.SIMULATED
                if obs.source_mode == SensorSourceModeEnum.SIMULATED
                else SensorHealthStatusEnum.CONNECTED
            )
        )

    # 3. Attempt cross-sensor fusion if complementary sensor observation exists
    complementary_type = (
        SensorTypeEnum.LIDAR
        if obs.sensor_type == SensorTypeEnum.DEPTH_CAMERA
        else (
            SensorTypeEnum.DEPTH_CAMERA
            if obs.sensor_type == SensorTypeEnum.LIDAR
            else None
        )
    )

    best_fusion_obj: Optional[TrackedObject] = None
    best_fusion_result = None

    if complementary_type is not None:
        # Check recent observations of the complementary sensor type
        recent_comp_obs = [
            prev
            for prev in reversed(world.environment.observation_history[:-1])
            if prev.sensor_type == complementary_type
        ]
        for comp_obs in recent_comp_obs:
            eval_res = evaluate_camera_lidar_fusion(
                obs if obs.sensor_type == SensorTypeEnum.DEPTH_CAMERA else comp_obs,
                comp_obs if obs.sensor_type == SensorTypeEnum.DEPTH_CAMERA else obs,
                transform_registry=registry,
                current_time=obs.ingested_at,
                max_time_diff_sec=max_dt,
                association_distance_m=fusion_gate,
                conflict_distance_m=conflict_gate,
            )
            if eval_res.status in (
                EpistemicStatusEnum.CORROBORATED,
                EpistemicStatusEnum.CONFLICTED,
            ):
                # Find the TrackedObject associated with comp_obs
                for tracked in world.environment.tracked_objects.values():
                    if comp_obs.observation_id in tracked.contributing_observation_ids:
                        best_fusion_obj = tracked
                        best_fusion_result = eval_res
                        break
                if best_fusion_obj is not None:
                    break

    if best_fusion_obj is not None and best_fusion_result is not None:
        tracked = best_fusion_obj
        if obs.observation_id not in tracked.contributing_observation_ids:
            tracked.contributing_observation_ids.append(obs.observation_id)
        if obs.sensor_type.value not in tracked.contributing_sensor_types:
            tracked.contributing_sensor_types.append(obs.sensor_type.value)

        tracked.last_observed_timestamp = max(
            tracked.last_observed_timestamp, obs.sensor_timestamp
        )
        tracked.last_ingested_at = max(tracked.last_ingested_at, obs.ingested_at)
        tracked.last_known_source = obs.sensor_id
        tracked.currently_visible = True

        if obs.classification and (
            tracked.label == "obstacle" or obs.sensor_type == SensorTypeEnum.DEPTH_CAMERA
        ):
            tracked.label = obs.classification
        if obs.confidence is not None:
            tracked.classification_confidence = max(
                tracked.classification_confidence, obs.confidence
            )

        if (
            best_fusion_result.status == EpistemicStatusEnum.CORROBORATED
            and best_fusion_result.fused_position is not None
        ):
            tracked.position = best_fusion_result.fused_position
            if best_fusion_result.common_frame:
                tracked.coordinate_frame = best_fusion_result.common_frame
            tracked.position_uncertainty_m = best_fusion_result.fused_position.uncertainty_m
            tracked.epistemic_status = EpistemicStatusEnum.CORROBORATED
            tracked.tracking_status = TrackingStatusEnum.ACTIVE
            tracked.corroborated_by_multiple_sensors = True
            tracked.fusion_timestamp = obs.ingested_at
            tracked.association_method = best_fusion_result.association_method
            tracked.association_score = best_fusion_result.association_score
            tracked.conflict_details = None
            tracked.discrepant_positions = []
            if (
                tracked.coordinate_frame == "map"
                and world.environment.transform_map_validated
            ):
                tracked.map_position = Position2D(
                    x=tracked.position.x, y=tracked.position.y
                )
        elif best_fusion_result.status == EpistemicStatusEnum.CONFLICTED:
            # Preserve disagreement without averaging incompatible measurements!
            tracked.epistemic_status = EpistemicStatusEnum.CONFLICTED
            tracked.tracking_status = TrackingStatusEnum.CONFLICTED
            tracked.corroborated_by_multiple_sensors = False
            tracked.conflict_details = best_fusion_result.conflict_details
            tracked.discrepant_positions = list(best_fusion_result.discrepant_positions)
        return world

    # 4. Same-sensor / same-frame persistent object association
    label_clean = (obs.classification or ("obstacle" if obs.is_obstacle else "unknown")).strip()
    matched_tracked: Optional[TrackedObject] = None
    best_dist = float("inf")

    for candidate in world.environment.tracked_objects.values():
        if candidate.coordinate_frame != obs.coordinate_frame:
            continue
        same_label = candidate.label.lower() == label_clean.lower()
        if not same_label:
            continue

        dist = euclidean_distance_3d(candidate.position, obs.position)
        combined_unc = candidate.position_uncertainty_m + obs.position.uncertainty_m
        if dist <= (assoc_gate + combined_unc) and dist < best_dist:
            best_dist = dist
            matched_tracked = candidate
        elif (
            obs.sensor_type in candidate.contributing_sensor_types
            and angular_separation_rad(candidate.position, obs.position) <= 0.15
            and abs(obs.sensor_timestamp - candidate.last_observed_timestamp) <= 1e-3
            and dist > (assoc_gate + combined_unc)
        ):
            # Conflicting simultaneous report in the same direction
            candidate.epistemic_status = EpistemicStatusEnum.CONFLICTED
            candidate.tracking_status = TrackingStatusEnum.CONFLICTED
            candidate.conflict_details = (
                f"Conflicting position observation {obs.observation_id} for {candidate.label} "
                f"in frame '{obs.coordinate_frame}' (distance delta {dist:.2f}m)."
            )
            candidate.discrepant_positions.append(
                {
                    "observation_id": obs.observation_id,
                    "sensor_id": obs.sensor_id,
                    "timestamp": obs.sensor_timestamp,
                    "frame": obs.coordinate_frame,
                    "position": obs.position.model_dump(),
                }
            )
            if obs.observation_id not in candidate.contributing_observation_ids:
                candidate.contributing_observation_ids.append(obs.observation_id)
            return world
        elif (
            obs.sensor_type in candidate.contributing_sensor_types
            and angular_separation_rad(candidate.position, obs.position) <= 0.25
            and obs.sensor_timestamp > candidate.last_observed_timestamp
            and dist < best_dist
        ):
            # Same object moving closer/farther across sequential frames
            best_dist = dist
            matched_tracked = candidate

    is_stale_obs = (
        False
        if obs.source_mode == SensorSourceModeEnum.PHYSICAL
        else (obs.ingested_at - obs.sensor_timestamp) > ultron_config.camera_stale_after_sec
    )
    map_pos: Optional[Position2D] = None
    if obs.coordinate_frame == "map" and world.environment.transform_map_validated:
        map_pos = Position2D(x=obs.position.x, y=obs.position.y)

    if matched_tracked is not None:
        prev_range = matched_tracked.position.computed_range_m()
        new_range = obs.position.computed_range_m()
        matched_tracked.position = obs.position
        matched_tracked.position_uncertainty_m = obs.position.uncertainty_m
        if map_pos is not None:
            matched_tracked.map_position = map_pos
        if obs.confidence is not None:
            matched_tracked.classification_confidence = obs.confidence
        matched_tracked.last_observed_timestamp = max(
            matched_tracked.last_observed_timestamp, obs.sensor_timestamp
        )
        matched_tracked.last_ingested_at = max(
            matched_tracked.last_ingested_at, obs.ingested_at
        )
        matched_tracked.last_known_source = obs.sensor_id
        matched_tracked.last_detector_object_id = obs.detector_object_id
        matched_tracked.source_mode = obs.source_mode
        if obs.is_obstacle:
            matched_tracked.is_obstacle = True
            # If obstacle moved closer or entered stopping range, re-arm it so avoidance/replanning triggers
            if matched_tracked.handled and (new_range <= 1.8 and (prev_range > 1.8 or (prev_range - new_range) >= 0.2)):
                matched_tracked.handled = False
        if obs.observation_id not in matched_tracked.contributing_observation_ids:
            matched_tracked.contributing_observation_ids.append(obs.observation_id)
        if obs.sensor_type.value not in matched_tracked.contributing_sensor_types:
            matched_tracked.contributing_sensor_types.append(obs.sensor_type.value)
        matched_tracked.currently_visible = not is_stale_obs
        if is_stale_obs:
            matched_tracked.tracking_status = TrackingStatusEnum.STALE
            matched_tracked.epistemic_status = EpistemicStatusEnum.STALE
        elif matched_tracked.epistemic_status != EpistemicStatusEnum.CORROBORATED:
            matched_tracked.tracking_status = TrackingStatusEnum.ACTIVE
            matched_tracked.epistemic_status = EpistemicStatusEnum.OBSERVED
    else:
        obj_id = _next_tracked_object_id(world)
        new_tracked = TrackedObject(
            object_id=obj_id,
            label=label_clean,
            position=obs.position,
            coordinate_frame=obs.coordinate_frame,
            map_position=map_pos,
            position_uncertainty_m=obs.position.uncertainty_m,
            classification_confidence=obs.confidence if obs.confidence is not None else 0.85,
            first_observed_timestamp=obs.sensor_timestamp,
            last_observed_timestamp=obs.sensor_timestamp,
            last_ingested_at=obs.ingested_at,
            last_known_source=obs.sensor_id,
            vehicle_id=obs.vehicle_id,
            source_mode=obs.source_mode,
            contributing_observation_ids=[obs.observation_id],
            contributing_sensor_types=[obs.sensor_type.value],
            last_detector_object_id=obs.detector_object_id,
            tracking_status=(
                TrackingStatusEnum.STALE if is_stale_obs else TrackingStatusEnum.ACTIVE
            ),
            epistemic_status=(
                EpistemicStatusEnum.STALE if is_stale_obs else EpistemicStatusEnum.OBSERVED
            ),
            currently_visible=not is_stale_obs,
            corroborated_by_multiple_sensors=False,
            is_obstacle=obs.is_obstacle,
            is_hazard=obs.is_hazard,
        )
        world.environment.tracked_objects[obj_id] = new_tracked

    # 5. Synchronize legacy `objects`, `hazards`, and `known_obstacles` lists for backward compatibility
    vid_enum = VehicleID.SCOUT
    if obs.is_hazard:
        world.environment.hazards.append(
            HazardEntity(
                id=f"H-{uuid.uuid4().hex[:6]}",
                location=Position2D(x=obs.position.x, y=obs.position.y),
                coordinate_frame=obs.coordinate_frame,
                confidence=obs.confidence if obs.confidence is not None else 0.9,
                source_vehicle=vid_enum,
                type=label_clean,
                observation_id=obs.observation_id,
                timestamp=obs.sensor_timestamp,
                epistemic_status=(
                    EpistemicStatusEnum.STALE if is_stale_obs else EpistemicStatusEnum.OBSERVED
                ),
                source_mode=obs.source_mode,
            )
        )
    elif obs.is_obstacle and obs.coordinate_frame == "map":
        pt = Position2D(x=obs.position.x, y=obs.position.y)
        if not any(
            math.hypot(existing.x - pt.x, existing.y - pt.y) < 0.25
            for existing in world.environment.known_obstacles
        ):
            world.environment.known_obstacles.append(pt)

    if not obs.is_hazard:
        # Update or append in world.environment.objects
        existing_legacy = next(
            (
                o
                for o in world.environment.objects
                if o.label.lower() == label_clean.lower()
                and o.coordinate_frame == obs.coordinate_frame
                and math.hypot(o.location.x - obs.position.x, o.location.y - obs.position.y)
                <= assoc_gate
            ),
            None,
        )
        if existing_legacy is not None:
            existing_legacy.location = Position2D(x=obs.position.x, y=obs.position.y)
            existing_legacy.confidence = (
                obs.confidence if obs.confidence is not None else existing_legacy.confidence
            )
            existing_legacy.observation_id = obs.observation_id
            existing_legacy.timestamp = obs.sensor_timestamp
            existing_legacy.source_mode = obs.source_mode
        else:
            world.environment.objects.append(
                ObjectEntity(
                    id=f"O-{uuid.uuid4().hex[:6]}",
                    location=Position2D(x=obs.position.x, y=obs.position.y),
                    coordinate_frame=obs.coordinate_frame,
                    label=label_clean,
                    confidence=obs.confidence if obs.confidence is not None else 0.85,
                    source_vehicle=vid_enum,
                    observation_id=obs.observation_id,
                    timestamp=obs.sensor_timestamp,
                    epistemic_status=(
                        EpistemicStatusEnum.STALE
                        if is_stale_obs
                        else EpistemicStatusEnum.OBSERVED
                    ),
                    source_mode=obs.source_mode,
                )
            )

    return world


def refresh_world_model_freshness(
    world: WorldModel,
    *,
    current_time: Optional[float] = None,
    stale_after_sec: Optional[float] = None,
) -> WorldModel:
    """Evaluate observation & object freshness at `current_time` and transition expired records to `STALE`."""
    now = current_time if current_time is not None else time.time()
    threshold = (
        stale_after_sec
        if stale_after_sec is not None
        else ultron_config.camera_stale_after_sec
    )

    for tracked in world.environment.tracked_objects.values():
        age_obs = now - tracked.last_observed_timestamp
        if tracked.source_mode == SensorSourceModeEnum.PHYSICAL and tracked.last_ingested_at:
            age = min(abs(age_obs), max(0.0, now - tracked.last_ingested_at))
        else:
            age = age_obs
        if age > threshold:
            tracked.currently_visible = False
            if tracked.tracking_status != TrackingStatusEnum.CONFLICTED:
                tracked.tracking_status = TrackingStatusEnum.STALE
            if tracked.epistemic_status != EpistemicStatusEnum.CONFLICTED:
                tracked.epistemic_status = EpistemicStatusEnum.STALE

    for sh in world.environment.sensor_health.values():
        if not sh.enabled or sh.status == SensorHealthStatusEnum.UNAVAILABLE:
            continue
        if sh.last_ingested_at is not None and (now - sh.last_ingested_at) > sh.stale_after_sec:
            sh.is_stale = True
            if sh.status not in (
                SensorHealthStatusEnum.DISCONNECTED,
                SensorHealthStatusEnum.ERROR,
            ):
                sh.status = SensorHealthStatusEnum.STALE

    return world


def update_world_model(
    world: WorldModel,
    obs: StructuredObservation | SensorObservation,
) -> WorldModel:
    """Deterministically merges new sensor or vision observations into the shared World Model."""
    if isinstance(obs, SensorObservation):
        return ingest_sensor_observation(world, obs)

    sensor_obs = structured_to_sensor_observation(obs)
    if obs.event == "obstacle_blocked":
        pt = obs.location
        if not any(
            math.hypot(existing.x - pt.x, existing.y - pt.y) < 0.1
            for existing in world.environment.known_obstacles
        ):
            world.environment.known_obstacles.append(pt)
    return ingest_sensor_observation(world, sensor_obs)
