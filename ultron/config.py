import os
from pydantic import BaseModel, Field


class UltronConfig(BaseModel):
    """Configuration settings for the ULTRON agent."""
    gemini_model: str = Field(default="gemini-2.0-flash")
    planning_timeout_sec: float = Field(default=8.0)
    replanning_timeout_sec: float = Field(default=5.0)
    max_validation_retries: int = Field(default=3)
    hardware_lockout: bool = Field(default=True)
    default_vehicle_mode: str = Field(default="simulated")
    scout_id: str = "SCOUT01"
    hawk_id: str = "HAWK01"


config = UltronConfig(
    gemini_model=os.getenv("SHADOW_LLM_MODEL", "gemini-2.0-flash"),
    hardware_lockout=os.getenv("SHADOW_HARDWARE_LOCKOUT", "true").lower() == "true",
    default_vehicle_mode=os.getenv("SHADOW_DEFAULT_VEHICLE_MODE", "simulated"),
)
