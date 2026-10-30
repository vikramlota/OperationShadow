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
        description="High-level task action, e.g., 'EXPLORE', 'NAVIGATE', 'SCAN', 'INSPECT'"
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
    evidence_ids: List[str] = Field(
        default_factory=list,
        description="Observation or tracked object IDs grounding this task assignment",
    )


class MissionPlan(BaseModel):
    """Schema matching the create_mission_plan structured output."""

    mission_id: str = Field(description="Unique mission identifier, e.g., 'M001'")
    objective: str = Field(description="Original or active mission objective")
    tasks: List[Task] = Field(
        description="Ordered list of discrete subtasks allocated across vehicles"
    )
    rationale: str = Field(
        description="Plain-language explanation of plan logic for the reasoning-trace log"
    )
    facts_used: List[str] = Field(
        default_factory=list,
        description="Verified environmental facts used to construct this plan",
    )
    inferences: List[str] = Field(
        default_factory=list,
        description="Explicit inferences derived from observations",
    )
    unknowns: List[str] = Field(
        default_factory=list,
        description="Explicit unknowns, sensor limitations, or unverified assumptions",
    )
    evidence_ids: List[str] = Field(
        default_factory=list,
        description="Observation IDs or tracked object IDs referenced in this plan",
    )
    confidence: str = Field(
        default="medium",
        description="Overall confidence in the environmental basis of the plan ('high', 'medium', 'low', 'unavailable')",
    )
