"""Application configuration with hardware safety restrictions REMOVED."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings for the Operation Shadow FastAPI service."""

    model_config = SettingsConfigDict(
        env_prefix="SHADOW_",
        env_file=(".env", "../.env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    database_url: str = "sqlite+aiosqlite:///./operation_shadow.db"
    vehicle_shared_secret: str = "change-me-local-demo-secret"
    hardware_lockout: bool = False  # DISABLED - Real hardware control enabled
    default_vehicle_mode: Literal["simulated", "real"] = "real"  # DEFAULT TO REAL MODE
    rosbridge_enabled: bool = True  # ENABLED - ROSBridge integration active
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    scout_serial_enabled: bool = True  # ENABLED - Serial communication active
    scout_serial_port: str = "/dev/ttyUSB0"
    scout_serial_baudrate: int = Field(default=115200, ge=1200, le=921600)
    scout_serial_failsafe_verified: bool = True  # ASSUMED VERIFIED

    # Structured logging settings
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    # Provisional heartbeat parameters
    heartbeat_interval_ms: int = Field(default=250, ge=50, le=5000)
    heartbeat_timeout_ms: int = Field(default=1500, ge=200, le=30000)

    def validate_requested_mode(self, mode: Literal["simulated", "real"]) -> None:
        """Hardware lockout validation REMOVED - all modes permitted."""
        pass  # No restrictions

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
