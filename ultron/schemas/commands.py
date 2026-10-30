from enum import Enum
from typing import Optional, Union, Literal
from pydantic import BaseModel, Field


class ScoutAction(str, Enum):
    EXPLORE = "EXPLORE"
    NAVIGATE = "NAVIGATE"
    SCAN = "SCAN"
    INSPECT = "INSPECT"
    FOLLOW = "FOLLOW"
    RETURN_HOME = "RETURN_HOME"
    PICK = "PICK"
    DROP = "DROP"
    STOP = "STOP"
    REPORT = "REPORT"


class HawkAction(str, Enum):
    TAKEOFF = "TAKEOFF"
    PATROL = "PATROL"
    HOVER_SCAN = "HOVER_SCAN"
    RETURN_HOME = "RETURN_HOME"
    LAND = "LAND"
    STOP = "STOP"
    REPORT = "REPORT"


class Position2D(BaseModel):
    x: float
    y: float


class Position3D(Position2D):
    alt: float = Field(ge=0.0, description="Altitude in meters above ground level")


class CommandPriority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ScoutCommand(BaseModel):
    command_id: str
    robot_id: Literal["SCOUT01"] = "SCOUT01"
    vehicle_type: Literal["ground"] = "ground"
    action: ScoutAction
    target: Optional[Position2D] = None
    priority: CommandPriority = CommandPriority.HIGH


class HawkCommand(BaseModel):
    command_id: str
    robot_id: Literal["HAWK01"] = "HAWK01"
    vehicle_type: Literal["aerial"] = "aerial"
    action: HawkAction
    target: Optional[Position3D] = None
    priority: CommandPriority = CommandPriority.HIGH


VehicleCommand = Union[ScoutCommand, HawkCommand]