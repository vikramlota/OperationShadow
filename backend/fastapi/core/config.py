"""Application configuration and hardware safety lockout enforcement."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings for the Operation Shadow FastAPI service."""

    model_config = SettingsConfigDict(
        env_prefix="SHADOW_",
        env_file=(".env", "../.env"),
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
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Structured logging settings
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    # Provisional heartbeat parameters (Hardware & IoT doc §5.2 unresolved; see docs/assumptions.md)
    heartbeat_interval_ms: int = Field(default=250, ge=50, le=5000)
    heartbeat_timeout_ms: int = Field(default=1500, ge=200, le=30000)

    @model_validator(mode="after")
    def enforce_hardware_lockout(self) -> Settings:
        """Prevent real hardware mode while hardware_lockout is active."""
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
        return self

    def validate_requested_mode(self, mode: Literal["simulated", "real"]) -> None:
        """Raise ValueError if 'real' mode is requested while hardware lockout is enabled."""
        if self.hardware_lockout and mode == "real":
            msg = (
                "Real hardware mode is locked (SHADOW_HARDWARE_LOCKOUT=true). "
                "Only 'simulated' mode is permitted until hardware interfaces are confirmed."
            )
            raise ValueError(msg)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
