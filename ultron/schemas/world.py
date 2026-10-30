from enum import Enum
from typing import Dict, List, Optional, Literal, Union
from pydantic import BaseModel, Field
from ultron.schemas.mission import MissionStatus, VehicleID
from ultron.schemas.commands import Position2D, Position3D


class ConnectionStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class VehicleMode(str, Enum):
    REAL = "real"
    SIMULATED = "simulated"


class ScoutState(BaseModel):
    robot_id: Literal["SCOUT01"] = "SCOUT01"
    vehicle_type: Literal["ground"] = "ground"
    mode: VehicleMode = VehicleMode.REAL
    position: Position2D = Position2D(x=0.0, y=0.0)
    heading: float = 0.0
    battery: float = Field(ge=0.0, le=100.0, default=100.0)
    status: str = "IDLE"
    connection: ConnectionStatus = ConnectionStatus.ONLINE
    temperature: Optional[float] = None
    gas_level: Optional[str] = "normal"
    current_task: Optional[str] = None


class HawkState(BaseModel):
    robot_id: Literal["HAWK01"] = "HAWK01"
    vehicle_type: Literal["aerial"] = "aerial"
    mode: VehicleMode = VehicleMode.REAL
    position: Position3D = Position3D(x=0.0, y=0.0, alt=0.0)
    battery: float = Field(ge=0.0, le=100.0, default=100.0)
    status: str = "IDLE"
    connection: ConnectionStatus = ConnectionStatus.ONLINE
    current_task: Optional[str] = None


class HazardEntity(BaseModel):
    id: str
    location: Position2D
    confidence: float
    source_vehicle: VehicleID
    type: str = "possible_hazard"
    handled: bool = False


class ObjectEntity(BaseModel):
    id: str
    location: Position2D
    label: str
    confidence: float
    source_vehicle: VehicleID
    handled: bool = False


class TaskTracker(BaseModel):
    completed: List[str] = Field(default_factory=list)
    active: Dict[VehicleID, Optional[str]] = Field(
        default_factory=lambda: {VehicleID.SCOUT: None, VehicleID.HAWK: None}
    )
    pending: List[str] = Field(default_factory=list)


class MissionState(BaseModel):
    id: str
    objective: str
    status: MissionStatus = MissionStatus.IDLE
    tasks: TaskTracker = Field(default_factory=TaskTracker)


class EnvironmentState(BaseModel):
    explored_areas: List[str] = Field(default_factory=list)
    known_obstacles: List[Position2D] = Field(default_factory=list)
    hazards: List[HazardEntity] = Field(default_factory=list)
    objects: List[ObjectEntity] = Field(default_factory=list)


class WorldModel(BaseModel):
    mission: MissionState
    vehicles: Dict[str, Union[ScoutState, HawkState]] = Field(
        default_factory=lambda: {
            "SCOUT01": ScoutState(),
            "HAWK01": HawkState()
        }
    )
    environment: EnvironmentState = Field(default_factory=EnvironmentState)
