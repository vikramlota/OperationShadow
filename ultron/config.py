import os
from pydantic import BaseModel, Field


class UltronConfig(BaseModel):
    """Configuration settings for the ULTRON agent - HARDWARE LOCKOUT DISABLED."""
    gemini_model: str = Field(default="gemini-3.5-flash-lite")
    planning_timeout_sec: float = Field(default=30.0)
    replanning_timeout_sec: float = Field(default=20.0)
    max_validation_retries: int = Field(default=3)
    hardware_lockout: bool = Field(default=False)  # DISABLED
    default_vehicle_mode: str = Field(default="real")  # DEFAULT TO REAL
    scout_id: str = "SCOUT01"


config = UltronConfig(
    gemini_model=os.getenv("SHADOW_LLM_MODEL", "gemini-3.5-flash-lite"),
    hardware_lockout=False,  # Force disabled
    default_vehicle_mode="real",  # Force real mode
)
