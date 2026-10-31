"""Application configuration for Operation Shadow FastAPI service."""

from __future__ import annotations

import os
import sys
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_IS_PYTEST = "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


class Settings(BaseSettings):
    """Environment-backed settings for the Operation Shadow FastAPI service."""

    model_config = SettingsConfigDict(
        env_prefix="SHADOW_",
        env_file=(
            None
            if _IS_PYTEST
            else (
                ".env",
                "../.env",
                "../../.env",
                "ultron/.env",
                "../ultron/.env",
                "../../ultron/.env",
            )
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    database_url: str = "sqlite+aiosqlite:///./operation_shadow.db"
    vehicle_shared_secret: str = "change-me-local-demo-secret"
    hardware_lockout: bool = True
    default_vehicle_mode: Literal["simulated", "real"] = "simulated"
    rosbridge_enabled: bool = False
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    scout_serial_enabled: bool = False
    scout_serial_port: str = "/dev/ttyUSB0"
    scout_serial_baudrate: int = Field(default=115200, ge=1200, le=921600)
    scout_serial_failsafe_verified: bool = False

    # Structured logging settings
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    # Provisional heartbeat parameters
    heartbeat_interval_ms: int = Field(default=250, ge=50, le=5000)
    heartbeat_timeout_ms: int = Field(default=1500, ge=200, le=30000)

    # Read-only ROS 2 Humble ROSBridge perception integration settings
    # Vehicle ID remains unset ("") by default until confirmed by the hardware engineer.
    ros_enabled: bool = False
    ros_host: str = ""
    ros_port: int = Field(default=9090, ge=1, le=65535)
    ros_vehicle_id: str = ""
    ros_detections_topic: str = "/detections_json"
    ros_detections_message_type: str = "std_msgs/msg/String"
    ros_detections_stale_after_seconds: float = Field(
        default=2.0,
        gt=0.0,
        le=300.0,
        allow_inf_nan=False,
    )
    # Global map transform (`map -> odom -> oak_parent_frame` and camera-to-LiDAR extrinsics)
    # is NOT yet validated. Keep False until confirmed so detections are never plotted on the
    # global map prematurely.
    ros_map_frame_confirmed: bool = False
    ros_queue_maxsize: int = Field(default=128, ge=1, le=4096)
    ros_reconnect_interval_seconds: float = Field(
        default=2.0,
        gt=0.0,
        le=60.0,
        allow_inf_nan=False,
    )

    # Configurable LiDAR ingestion settings (disabled & unconfigured by default until hardware team confirms topic/type/frame)
    lidar_enabled: bool = False
    lidar_topic: str = ""
    lidar_message_type: str = ""
    lidar_frame: str = ""
    lidar_stale_after_seconds: float = Field(
        default=2.0,
        gt=0.0,
        le=300.0,
        allow_inf_nan=False,
    )
    lidar_min_range_m: float = Field(default=0.05, gt=0.0, le=50.0, allow_inf_nan=False)
    lidar_max_range_m: float = Field(default=30.0, gt=0.1, le=200.0, allow_inf_nan=False)

    # Camera + LiDAR sensor fusion settings (extrinsics unconfirmed by default)
    camera_lidar_extrinsics_confirmed: bool = False
    fusion_max_time_diff_seconds: float = Field(default=0.25, gt=0.0, le=10.0, allow_inf_nan=False)
    fusion_association_distance_m: float = Field(default=0.5, gt=0.01, le=10.0, allow_inf_nan=False)
    fusion_conflict_distance_m: float = Field(default=1.2, gt=0.05, le=20.0, allow_inf_nan=False)

    @field_validator(
        "ros_host",
        "ros_vehicle_id",
        "ros_detections_topic",
        "ros_detections_message_type",
        "lidar_topic",
        "lidar_message_type",
        "lidar_frame",
        mode="before",
    )
    @classmethod
    def _strip_strings(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @model_validator(mode="after")
    def enforce_hardware_lockout_and_ros_config(self) -> Settings:
        """Prevent real hardware mode while hardware_lockout is active and validate ROS/LiDAR config."""
        if self.hardware_lockout and self.default_vehicle_mode == "real":
            msg = (
                "Cannot configure default_vehicle_mode='real' while "
                "hardware_lockout=True. Confirm vehicle interface and safety "
                "specifications before disabling hardware_lockout."
            )
            raise ValueError(msg)
        if self.heartbeat_timeout_ms <= self.heartbeat_interval_ms:
            msg = "heartbeat_timeout_ms must be greater than heartbeat_interval_ms"
            raise ValueError(msg)

        if self.ros_enabled:
            if not self.ros_host or "<" in self.ros_host or ">" in self.ros_host or " " in self.ros_host:
                msg = (
                    "SHADOW_ROS_HOST must be explicitly configured with a valid IP/hostname "
                    "(not empty or placeholder '<rover_ip>') when SHADOW_ROS_ENABLED=true."
                )
                raise ValueError(msg)
            if not self.ros_vehicle_id:
                msg = (
                    "SHADOW_ROS_VEHICLE_ID must be explicitly configured (e.g. 'SCOUT01') "
                    "when SHADOW_ROS_ENABLED=true; do not assume a default vehicle mapping."
                )
                raise ValueError(msg)
            if self.ros_vehicle_id not in ("SCOUT01", "HAWK01"):
                msg = (
                    f"Invalid SHADOW_ROS_VEHICLE_ID='{self.ros_vehicle_id}'. "
                    "Must be an explicitly supported vehicle identifier ('SCOUT01' or 'HAWK01')."
                )
                raise ValueError(msg)
            if not self.ros_detections_topic or not self.ros_detections_topic.startswith("/"):
                msg = (
                    "SHADOW_ROS_DETECTIONS_TOPIC must be a valid ROS topic path starting with '/' "
                    "(e.g. '/detections_json')."
                )
                raise ValueError(msg)

        if self.lidar_enabled:
            if not self.lidar_topic or not self.lidar_topic.startswith("/"):
                msg = (
                    "SHADOW_LIDAR_TOPIC must be explicitly configured with a valid ROS topic "
                    "starting with '/' when SHADOW_LIDAR_ENABLED=true (do not assume an unconfirmed topic)."
                )
                raise ValueError(msg)
            if not self.lidar_message_type:
                msg = (
                    "SHADOW_LIDAR_MESSAGE_TYPE must be explicitly configured when "
                    "SHADOW_LIDAR_ENABLED=true."
                )
                raise ValueError(msg)
            if not self.lidar_frame:
                msg = (
                    "SHADOW_LIDAR_FRAME must be explicitly configured when "
                    "SHADOW_LIDAR_ENABLED=true."
                )
                raise ValueError(msg)
        return self

    def validate_requested_mode(self, mode: Literal["simulated", "real"]) -> None:
        """Enforce hardware lockout so real mode is blocked while hardware_lockout is True."""
        if self.hardware_lockout and mode == "real":
            msg = (
                "Real hardware mode is locked while SHADOW_HARDWARE_LOCKOUT=true. "
                "Vehicle interface and onboard failsafe specifications must be confirmed first."
            )
            raise ValueError(msg)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
