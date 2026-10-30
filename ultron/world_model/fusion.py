"""Camera and LiDAR multi-sensor fusion and coordinate-transform verification (`ultron/world_model/fusion.py` — Section 5).

Enforces all 5 mandatory fusion prerequisites before associating or fusing
observations across sensors:
1. Timestamps are sufficiently aligned (`abs(t_cam - t_lidar) <= max_time_diff_sec`)
   and neither observation is stale.
2. Coordinate frames are known and non-empty.
3. Required coordinate transforms AND calibrated camera-to-LiDAR extrinsics are
   explicitly registered and validated (`FrameTransformRegistry`). Until confirmed by
   the hardware team, observations stay in their original frames and are NEVER
   falsely corroborated.
4. Spatial uncertainty (`uncertainty_m`) from both sensors is accounted for.
5. Spatial association is plausible:
   - Within association gate -> `CORROBORATED` (inverse-variance weighted fusion).
   - Within conflict zone (same bearing / region but incompatible range/position) ->
     `CONFLICTED` (both measurements preserved without averaging).
   - Beyond conflict gate -> separate non-matching entities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

from ultron.schemas.commands import Position2D
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorTypeEnum,
)
from ultron.schemas.world import TrackedObject, TrackingStatusEnum


@dataclass(frozen=True)
class RigidTransform3D:
    """Validated 3D translation (and optional yaw rotation in radians) between coordinate frames."""

    source_frame: str
    target_frame: str
    tx: float = 0.0
    ty: float = 0.0
    tz: float = 0.0
    yaw_rad: float = 0.0
    transform_uncertainty_m: float = 0.02
    is_calibrated: bool = True

    def apply(self, pos: Position3DMeasurement) -> Position3DMeasurement:
        cos_y = math.cos(self.yaw_rad)
        sin_y = math.sin(self.yaw_rad)
        rx = pos.x * cos_y - pos.y * sin_y + self.tx
        ry = pos.x * sin_y + pos.y * cos_y + self.ty
        rz = pos.z + self.tz
        new_range = round(math.sqrt(rx * rx + ry * ry + rz * rz), 4)
        new_unc = round(
            math.sqrt(pos.uncertainty_m ** 2 + self.transform_uncertainty_m ** 2),
            4,
        )
        return Position3DMeasurement(
            x=round(rx, 4),
            y=round(ry, 4),
            z=round(rz, 4),
            range_m=new_range,
            uncertainty_m=new_unc,
        )


class FrameTransformRegistry:
    """Registry of validated coordinate frame transforms and sensor extrinsics.

    By default, NO cross-frame transforms or camera-to-LiDAR extrinsics are marked
    validated, matching the current hardware status where `/tf` (`map -> odom -> oak_parent_frame`)
    and camera-to-LiDAR extrinsics are unconfirmed.
    """

    KNOWN_FRAMES = {
        "oak_rgb_camera_optical_frame",
        "oak_parent_frame",
        "lidar_link",
        "laser_frame",
        "base_link",
        "odom",
        "map",
    }

    def __init__(
        self,
        *,
        map_frame_confirmed: bool = False,
        camera_lidar_extrinsics_confirmed: bool = False,
    ) -> None:
        self.map_frame_confirmed = map_frame_confirmed
        self.camera_lidar_extrinsics_confirmed = camera_lidar_extrinsics_confirmed
        self._transforms: Dict[Tuple[str, str], RigidTransform3D] = {}

    def register_transform(self, transform: RigidTransform3D) -> None:
        """Register an explicitly calibrated transform between two coordinate frames."""
        if not transform.source_frame or not transform.target_frame:
            raise ValueError("source_frame and target_frame must be non-empty.")
        self._transforms[(transform.source_frame, transform.target_frame)] = transform
        self.KNOWN_FRAMES.add(transform.source_frame)
        self.KNOWN_FRAMES.add(transform.target_frame)

    def is_known_frame(self, frame: str) -> bool:
        return bool(frame and frame.strip() in self.KNOWN_FRAMES)

    def can_transform(self, source_frame: str, target_frame: str) -> bool:
        if not self.is_known_frame(source_frame) or not self.is_known_frame(target_frame):
            return False
        if source_frame == target_frame:
            if target_frame == "map" and not self.map_frame_confirmed:
                return False
            return True
        if target_frame == "map" and not self.map_frame_confirmed:
            return False
        tf = self._transforms.get((source_frame, target_frame))
        return tf is not None and tf.is_calibrated

    def transform_position(
        self,
        pos: Position3DMeasurement,
        source_frame: str,
        target_frame: str,
    ) -> Optional[Position3DMeasurement]:
        """Transform `pos` from `source_frame` to `target_frame` ONLY if validated."""
        if not self.can_transform(source_frame, target_frame):
            return None
        if source_frame == target_frame:
            return pos.model_copy(deep=True)
        tf = self._transforms.get((source_frame, target_frame))
        if tf is None or not tf.is_calibrated:
            return None
        return tf.apply(pos)


@dataclass
class FusionEvaluationResult:
    """Detailed outcome of attempting to fuse a camera observation and a LiDAR observation."""

    fused: bool
    status: EpistemicStatusEnum
    reason: str
    common_frame: Optional[str] = None
    fused_position: Optional[Position3DMeasurement] = None
    association_score: Optional[float] = None
    association_method: Optional[str] = None
    distance_m: Optional[float] = None
    time_diff_sec: Optional[float] = None
    conflict_details: Optional[str] = None
    discrepant_positions: List[Dict[str, Any]] = field(default_factory=list)


def euclidean_distance_3d(a: Position3DMeasurement, b: Position3DMeasurement) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def angular_separation_rad(a: Position3DMeasurement, b: Position3DMeasurement) -> float:
    """Compute angle in radians between two 3D vectors from sensor origin."""
    ra = math.sqrt(a.x ** 2 + a.y ** 2 + a.z ** 2)
    rb = math.sqrt(b.x ** 2 + b.y ** 2 + b.z ** 2)
    if ra < 1e-6 or rb < 1e-6:
        return 0.0
    cos_theta = (a.x * b.x + a.y * b.y + a.z * b.z) / (ra * rb)
    cos_theta = max(-1.0, min(1.0, cos_theta))
    return math.acos(cos_theta)


def evaluate_camera_lidar_fusion(
    camera_obs: SensorObservation,
    lidar_obs: SensorObservation,
    *,
    transform_registry: FrameTransformRegistry,
    current_time: Optional[float] = None,
    stale_after_sec: float = 2.0,
    max_time_diff_sec: float = 0.25,
    association_distance_m: float = 0.5,
    conflict_distance_m: float = 1.2,
    conflict_bearing_rad: float = 0.20,
) -> FusionEvaluationResult:
    """Evaluate whether a depth-camera observation and a LiDAR observation can be fused (§5).

    Strictly checks:
    1. Sensor modalities (one camera, one LiDAR) and valid measurements.
    2. Freshness and timestamp alignment (`abs(t_cam - t_lidar) <= max_time_diff_sec`).
    3. Known coordinate frames and validated camera-to-LiDAR extrinsics + transforms.
    4. Combined spatial uncertainty.
    5. Plausible spatial association vs. sensor conflict vs. distinct objects.
    """
    # Ensure ordering: camera_obs is DEPTH_CAMERA, lidar_obs is LIDAR
    if (
        camera_obs.sensor_type == SensorTypeEnum.LIDAR
        and lidar_obs.sensor_type == SensorTypeEnum.DEPTH_CAMERA
    ):
        camera_obs, lidar_obs = lidar_obs, camera_obs

    if (
        camera_obs.sensor_type != SensorTypeEnum.DEPTH_CAMERA
        or lidar_obs.sensor_type != SensorTypeEnum.LIDAR
    ):
        return FusionEvaluationResult(
            fused=False,
            status=EpistemicStatusEnum.OBSERVED,
            reason="incompatible_sensor_modalities",
        )

    # Prerequisite 1: Timestamp alignment & staleness
    if current_time is not None:
        if (current_time - camera_obs.sensor_timestamp) > stale_after_sec or (
            current_time - lidar_obs.sensor_timestamp
        ) > stale_after_sec:
            return FusionEvaluationResult(
                fused=False,
                status=EpistemicStatusEnum.STALE,
                reason="stale_observation_timestamp",
                time_diff_sec=round(
                    abs(camera_obs.sensor_timestamp - lidar_obs.sensor_timestamp), 4
                ),
            )

    time_diff = abs(camera_obs.sensor_timestamp - lidar_obs.sensor_timestamp)
    if time_diff > max_time_diff_sec:
        return FusionEvaluationResult(
            fused=False,
            status=EpistemicStatusEnum.OBSERVED,
            reason=f"timestamp_mismatch ({time_diff:.3f}s > {max_time_diff_sec:.3f}s)",
            time_diff_sec=round(time_diff, 4),
        )

    # Prerequisite 2: Coordinate frames are known
    if not transform_registry.is_known_frame(
        camera_obs.coordinate_frame
    ) or not transform_registry.is_known_frame(lidar_obs.coordinate_frame):
        return FusionEvaluationResult(
            fused=False,
            status=EpistemicStatusEnum.OBSERVED,
            reason=(
                f"unknown_coordinate_frame "
                f"(camera='{camera_obs.coordinate_frame}', lidar='{lidar_obs.coordinate_frame}')"
            ),
            time_diff_sec=round(time_diff, 4),
        )

    # Prerequisite 3: Extrinsics and coordinate transforms are explicitly validated
    if not transform_registry.camera_lidar_extrinsics_confirmed:
        return FusionEvaluationResult(
            fused=False,
            status=EpistemicStatusEnum.OBSERVED,
            reason="camera_lidar_extrinsics_unconfirmed",
            time_diff_sec=round(time_diff, 4),
        )

    target_frame = camera_obs.coordinate_frame
    cam_pos = camera_obs.position
    lidar_pos_in_target = transform_registry.transform_position(
        lidar_obs.position,
        source_frame=lidar_obs.coordinate_frame,
        target_frame=target_frame,
    )
    if lidar_pos_in_target is None:
        # Try transforming camera into lidar frame if direct transform isn't registered
        target_frame = lidar_obs.coordinate_frame
        cam_pos_transformed = transform_registry.transform_position(
            camera_obs.position,
            source_frame=camera_obs.coordinate_frame,
            target_frame=target_frame,
        )
        if cam_pos_transformed is None:
            return FusionEvaluationResult(
                fused=False,
                status=EpistemicStatusEnum.OBSERVED,
                reason=(
                    f"missing_or_unvalidated_transform "
                    f"('{camera_obs.coordinate_frame}' <-> '{lidar_obs.coordinate_frame}')"
                ),
                time_diff_sec=round(time_diff, 4),
            )
        cam_pos = cam_pos_transformed
        lidar_pos_in_target = lidar_obs.position

    # Prerequisite 4: Spatial uncertainty accounting
    sigma_cam = max(0.02, cam_pos.uncertainty_m)
    sigma_lidar = max(0.02, lidar_pos_in_target.uncertainty_m)
    combined_uncertainty = math.sqrt(sigma_cam ** 2 + sigma_lidar ** 2)

    # Prerequisite 5: Spatial proximity vs conflict check
    dist = euclidean_distance_3d(cam_pos, lidar_pos_in_target)
    angle_rad = angular_separation_rad(cam_pos, lidar_pos_in_target)
    effective_gate = association_distance_m + combined_uncertainty

    if dist <= effective_gate:
        # Inverse-variance weighted fusion
        w_cam = 1.0 / (sigma_cam ** 2)
        w_lidar = 1.0 / (sigma_lidar ** 2)
        w_sum = w_cam + w_lidar
        fx = round((cam_pos.x * w_cam + lidar_pos_in_target.x * w_lidar) / w_sum, 4)
        fy = round((cam_pos.y * w_cam + lidar_pos_in_target.y * w_lidar) / w_sum, 4)
        fz = round((cam_pos.z * w_cam + lidar_pos_in_target.z * w_lidar) / w_sum, 4)
        frange = round(math.sqrt(fx * fx + fy * fy + fz * fz), 4)
        fused_sigma = round(math.sqrt(1.0 / w_sum), 4)
        score = round(max(0.0, 1.0 - (dist / max(0.01, effective_gate))), 4)

        return FusionEvaluationResult(
            fused=True,
            status=EpistemicStatusEnum.CORROBORATED,
            reason="corroborated_multi_sensor_match",
            common_frame=target_frame,
            fused_position=Position3DMeasurement(
                x=fx,
                y=fy,
                z=fz,
                range_m=frange,
                uncertainty_m=fused_sigma,
            ),
            association_score=score,
            association_method="inverse_variance_3d_gated",
            distance_m=round(dist, 4),
            time_diff_sec=round(time_diff, 4),
        )

    # Check if sensors are looking along the same ray/region (`angle_rad <= conflict_bearing_rad`
    # or within `conflict_distance_m`) but report conflicting range/position
    if dist <= conflict_distance_m or angle_rad <= conflict_bearing_rad:
        conflict_msg = (
            f"Sensor conflict in frame '{target_frame}': depth_camera ({camera_obs.observation_id}) "
            f"reports ({cam_pos.x:.2f}, {cam_pos.y:.2f}, {cam_pos.z:.2f})m "
            f"[range={cam_pos.computed_range_m():.2f}m ±{sigma_cam:.2f}m], whereas "
            f"lidar ({lidar_obs.observation_id}) reports "
            f"({lidar_pos_in_target.x:.2f}, {lidar_pos_in_target.y:.2f}, {lidar_pos_in_target.z:.2f})m "
            f"[range={lidar_pos_in_target.computed_range_m():.2f}m ±{sigma_lidar:.2f}m]; "
            f"discrepancy={dist:.2f}m exceeds gate={effective_gate:.2f}m."
        )
        discrepant = [
            {
                "observation_id": camera_obs.observation_id,
                "sensor_id": camera_obs.sensor_id,
                "sensor_type": camera_obs.sensor_type.value,
                "timestamp": camera_obs.sensor_timestamp,
                "frame": target_frame,
                "position": cam_pos.model_dump(),
            },
            {
                "observation_id": lidar_obs.observation_id,
                "sensor_id": lidar_obs.sensor_id,
                "sensor_type": lidar_obs.sensor_type.value,
                "timestamp": lidar_obs.sensor_timestamp,
                "frame": target_frame,
                "position": lidar_pos_in_target.model_dump(),
            },
        ]
        return FusionEvaluationResult(
            fused=False,
            status=EpistemicStatusEnum.CONFLICTED,
            reason="conflicting_sensor_measurements",
            common_frame=target_frame,
            distance_m=round(dist, 4),
            time_diff_sec=round(time_diff, 4),
            conflict_details=conflict_msg,
            discrepant_positions=discrepant,
        )

    return FusionEvaluationResult(
        fused=False,
        status=EpistemicStatusEnum.OBSERVED,
        reason=f"non_matching_spatial_separation ({dist:.2f}m > {conflict_distance_m:.2f}m)",
        common_frame=target_frame,
        distance_m=round(dist, 4),
        time_diff_sec=round(time_diff, 4),
    )
