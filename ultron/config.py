import json
import os
from pathlib import Path
from typing import Any, Dict
from pydantic import BaseModel, Field

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"


def load_json_config(filename: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    """Loads a JSON configuration file from the root configs/ directory with safe fallback."""
    path = CONFIGS_DIR / filename
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return fallback


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class UltronConfig(BaseModel):
    """Configuration settings for the ULTRON agent and multi-sensor fusion layer."""

    gemini_model: str = Field(default="gemini-2.5-flash")
    planning_timeout_sec: float = Field(default=30.0)
    replanning_timeout_sec: float = Field(default=20.0)
    max_validation_retries: int = Field(default=3)
    hardware_lockout: bool = Field(default=True)
    default_vehicle_mode: str = Field(default="simulated")
    scout_id: str = "SCOUT01"

    # Sensor & freshness thresholds
    camera_enabled: bool = Field(default=False)
    camera_topic: str = Field(default="/detections_json")
    camera_frame: str = Field(default="oak_rgb_camera_optical_frame")
    camera_stale_after_sec: float = Field(default=2.0, gt=0.0)

    # LiDAR remains disabled and unconfigured by default until hardware team confirms topic/type/frame
    lidar_enabled: bool = Field(default=False)
    lidar_topic: str = Field(default="")
    lidar_message_type: str = Field(default="")
    lidar_frame: str = Field(default="")
    lidar_stale_after_sec: float = Field(default=2.0, gt=0.0)
    lidar_min_range_m: float = Field(default=0.05, gt=0.0)
    lidar_max_range_m: float = Field(default=30.0, gt=0.1)

    # Fusion & tracking parameters
    map_frame_confirmed: bool = Field(default=False)
    camera_lidar_extrinsics_confirmed: bool = Field(default=False)
    fusion_max_time_diff_sec: float = Field(default=0.25, gt=0.0)
    fusion_association_distance_m: float = Field(default=0.5, gt=0.01)
    fusion_conflict_distance_m: float = Field(default=1.2, gt=0.05)
    tracking_association_distance_m: float = Field(default=0.6, gt=0.01)
    replan_debounce_sec: float = Field(default=1.0, ge=0.0)
    route_obstacle_clearance_m: float = Field(default=0.75, gt=0.05)


config = UltronConfig(
    gemini_model=os.getenv("SHADOW_LLM_MODEL", "gemini-2.5-flash"),
    hardware_lockout=_env_bool("SHADOW_HARDWARE_LOCKOUT", True),
    default_vehicle_mode=os.getenv("SHADOW_DEFAULT_VEHICLE_MODE", "simulated"),
    camera_enabled=_env_bool("SHADOW_ROS_ENABLED", False),
    camera_topic=os.getenv("SHADOW_ROS_DETECTIONS_TOPIC", "/detections_json"),
    camera_stale_after_sec=_env_float("SHADOW_ROS_DETECTIONS_STALE_AFTER_SECONDS", 2.0),
    lidar_enabled=_env_bool("SHADOW_LIDAR_ENABLED", False),
    lidar_topic=os.getenv("SHADOW_LIDAR_TOPIC", ""),
    lidar_message_type=os.getenv("SHADOW_LIDAR_MESSAGE_TYPE", ""),
    lidar_frame=os.getenv("SHADOW_LIDAR_FRAME", ""),
    lidar_stale_after_sec=_env_float("SHADOW_LIDAR_STALE_AFTER_SECONDS", 2.0),
    map_frame_confirmed=_env_bool("SHADOW_ROS_MAP_FRAME_CONFIRMED", False),
    camera_lidar_extrinsics_confirmed=_env_bool("SHADOW_CAMERA_LIDAR_EXTRINSICS_CONFIRMED", False),
    fusion_max_time_diff_sec=_env_float("SHADOW_FUSION_MAX_TIME_DIFF_SECONDS", 0.25),
    fusion_association_distance_m=_env_float("SHADOW_FUSION_ASSOCIATION_DISTANCE_M", 0.5),
    fusion_conflict_distance_m=_env_float("SHADOW_FUSION_CONFLICT_DISTANCE_M", 1.2),
)
