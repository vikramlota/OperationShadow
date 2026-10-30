from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field


class VehicleID(str, Enum):
    SCOUT = "SCOUT01"


class MissionStatus(str, Enum):
    IDLE = "IDLE"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEGRADED = "DEGRADED"


class TaskPriority(int, Enum):
    HIGH = 1
    MEDIUM = 2
    LOW = 3


class Task(BaseModel):
    id: str = Field(description="Unique identifier for the task, e.g., 'T001'")
    vehicle: VehicleID = Field(description="Designated vehicle executing this task: SCOUT01")
    type: str = Field(
        description="High-level task action, e.g., 'explore', 'aerial_scan', 'find_target'"
    )
    area: Optional[str] = Field(
        default=None, description="Target zone or sector identifier, e.g., 'sector_A', 'zone_2'"
    )
    target: Optional[str] = Field(
        default=None, description="Specific entity or item label, e.g., 'red_package'"
    )
    priority: int = Field(
        default=2, ge=1, le=3, description="Task urgency rating (1=High, 2=Medium, 3=Low)"
    )


class MissionPlan(BaseModel):
    """Schema matching the create_mission_plan tool call."""
    mission_id: str = Field(description="Unique mission identifier, e.g., 'M001'")
    objective: str = Field(description="Original or active mission objective")
    tasks: List[Task] = Field(
        description="Ordered list of discrete subtasks allocated across vehicles"
    )
    rationale: str = Field(
        description="Plain-language explanation of plan logic for the reasoning-trace log"
    )
