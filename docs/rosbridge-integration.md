# Operation Shadow — ROS 2 Humble Live Perception & Evidence-Grounded Multi-Sensor ULTRON

## 1. Overview & Confirmed Hardware Specifications

This document specifies the live rover perception and environmental intelligence integration for Operation Shadow (`RosBridgeAdapter`, `PerceptionService`, `DepthCameraAdapter`, `LidarIngestionAdapter`, `FrameTransformRegistry`, and `WorldModelTools`), which connects the FastAPI backend, ULTRON LangGraph agent, and React tactical dashboard to the rover's onboard **ROS 2 Humble** ROSBridge WebSocket server (`ws://<SHADOW_ROS_HOST>:9090`).

### Confirmed Rover Environment
- **ROS Distribution**: ROS 2 Humble on Ubuntu
- **ROSBridge WebSocket Port**: `9090` (`ws://<rover_ip>:9090`)
- **Detection Topic**: `/detections_json` (up to `5 Hz`)
- **Message Type**: `std_msgs/msg/String` (ROS 2 interface notation; compatible with `std_msgs/String` and ROSBridge v2 JSON `publish` envelopes)
- **Currently Published Detection Frame**: `oak_rgb_camera_optical_frame` (parent frame: `oak_parent_frame`)
- **TF Topics Present on Rover**: `/tf` and `/tf_static` (previously included `map → odom → oak_parent_frame`, but live mapping/odometry transform and camera-to-LiDAR extrinsics are **not** yet validated)
- **LiDAR Status**: Topic name, message type, coordinate frame, publishing rate, and extrinsics are **not yet confirmed** by the hardware team; LiDAR remains disabled by default (`SHADOW_LIDAR_ENABLED=false`) and is explicitly reported as `UNAVAILABLE`.

---

## 2. Coordinate-Frame Rules, Multi-Sensor Fusion & Transform Interface

### Critical Implementation Rules
1. **Camera-Relative Default (`oak_rgb_camera_optical_frame` & `oak_parent_frame`)**:
   - Detections in `oak_rgb_camera_optical_frame` (`CAMERA_OPTICAL_RELATIVE`) and `oak_parent_frame` (`CAMERA_PARENT_RELATIVE`) are treated strictly as camera/frame-relative.
   - They are **never** plotted on the global SVG floor map (`FloorMap.tsx`), **never** confused with the rover's world position, and **never** assumed interchangeable with one another.
2. **Global Map Frame Gating (`map`)**:
   - Because the live `map → odom → oak_parent_frame` TF chain and camera-to-LiDAR extrinsics have not been validated, `SHADOW_ROS_MAP_FRAME_CONFIRMED` defaults to `false`.
   - Even if an incoming message reports `"frame": "map"`, it is classified as `UNCONFIRMED_MAP` (`map_projection_allowed = false`) unless `SHADOW_ROS_MAP_FRAME_CONFIRMED=true` is explicitly configured after calibration verification.
3. **Unknown Frame Preservation**:
   - Any unrecognized coordinate frame string is preserved verbatim across Pydantic models, SQLite `OBSERVATIONS`, WebSocket envelopes, and the React UI, and is explicitly labeled `UNSUPPORTED_FRAME` (`map_projection_allowed = false`).
4. **Camera + LiDAR Sensor Fusion (`ultron/world_model/fusion.py`)**:
   - Before associating or fusing camera and LiDAR observations, `evaluate_camera_lidar_fusion` verifies all 5 prerequisites:
     1. Timestamps are fresh and aligned within `SHADOW_FUSION_MAX_TIME_DIFF_SECONDS` (`0.25s`).
     2. Coordinate frames are known.
     3. Camera-to-LiDAR extrinsics (`SHADOW_CAMERA_LIDAR_EXTRINSICS_CONFIRMED=true`) and frame transforms (`FrameTransformRegistry`) are explicitly calibrated and validated.
     4. Spatial uncertainty from both sensors is accounted for.
     5. Spatial association is within `SHADOW_FUSION_ASSOCIATION_DISTANCE_M` (`CORROBORATED`), or if measurements disagree along the same ray/region within `SHADOW_FUSION_CONFLICT_DISTANCE_M`, both discrepant measurements are preserved and marked `CONFLICTED` without averaging.

---

## 3. Evidence-Grounded ULTRON & Read-Only World Model Tools

- **Persistent Object Tracking (`TrackedObject`)**:
  - `ultron/world_model/updater.py` assigns stable internal IDs (`OBJ-001`, `OBJ-002`, ...) across frames and never confuses a detector's frame-local `id` (`0, 1, ...`) with a persistent real-world identity.
- **Epistemic Status Classification (`EpistemicStatusEnum`)**:
  - Every tracked entity and environmental statement is classified as `OBSERVED`, `CORROBORATED`, `INFERRED`, `UNKNOWN`, `STALE`, or `CONFLICTED`.
- **Read-Only World Model Tools (`ultron/world_model/tools.py`)**:
  - `get_environment_snapshot`, `get_detected_objects`, `get_object_details`, `get_nearby_obstacles`, `get_free_space`, `get_sensor_health`, `get_observation_history`, `get_vehicle_pose`, and `get_environment_changes`.
  - `get_free_space` never claims an area is clear (`is_verified_clear=True`) unless active, fresh 360° LiDAR geometric coverage supports that conclusion.
- **Application-Level Evidence Verification (`ultron/planner/grounding.py`)**:
  - `validate_grounded_response` and `validate_plan_evidence` strip or reject any claim referencing nonexistent observation IDs, qualify stale observations as `STALE`, and prevent unverified multi-sensor corroboration claims.

---

## 4. Environment Configuration (`SHADOW_ROS_*` & `SHADOW_LIDAR_*`)

```env
SHADOW_HARDWARE_LOCKOUT=true
SHADOW_DEFAULT_VEHICLE_MODE=simulated
SHADOW_ROS_ENABLED=false
SHADOW_ROS_HOST=
SHADOW_ROS_PORT=9090
SHADOW_ROS_VEHICLE_ID=
SHADOW_ROS_DETECTIONS_TOPIC=/detections_json
SHADOW_ROS_DETECTIONS_MESSAGE_TYPE=std_msgs/msg/String
SHADOW_ROS_DETECTIONS_STALE_AFTER_SECONDS=2.0
SHADOW_ROS_MAP_FRAME_CONFIRMED=false

SHADOW_LIDAR_ENABLED=false
SHADOW_LIDAR_TOPIC=
SHADOW_LIDAR_MESSAGE_TYPE=
SHADOW_LIDAR_FRAME=
SHADOW_LIDAR_STALE_AFTER_SECONDS=2.0
SHADOW_LIDAR_MIN_RANGE_M=0.05
SHADOW_LIDAR_MAX_RANGE_M=30.0

SHADOW_CAMERA_LIDAR_EXTRINSICS_CONFIRMED=false
SHADOW_FUSION_MAX_TIME_DIFF_SECONDS=0.25
SHADOW_FUSION_ASSOCIATION_DISTANCE_M=0.5
SHADOW_FUSION_CONFLICT_DISTANCE_M=1.2
```
