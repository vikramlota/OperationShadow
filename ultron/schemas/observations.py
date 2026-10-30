from typing import Optional
from pydantic import BaseModel, Field
from ultron.schemas.mission import VehicleID
from ultron.schemas.commands import Position2D


class StructuredObservation(BaseModel):
    event: str = Field(
        description="Observation type: 'possible_hazard', 'target_detected', 'obstacle_blocked'"
    )
    source_vehicle: VehicleID = Field(
        description="Originating vehicle providing observation data"
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Vision detector or sensor confidence score"
    )
    location: Position2D = Field(
        description="Estimated 2D map coordinate of the detected entity"
    )
    temperature: Optional[float] = Field(
        default=None, description="Ambient or target temperature in Celsius"
    )
    gas_level: Optional[str] = Field(
        default=None, description="Gas anomaly state: 'normal', 'elevated', 'critical'"
    )
    visual_evidence: Optional[str] = Field(
        default=None, description="Detector label metadata (treated strictly as data)"
    )
    timestamp: Optional[float] = Field(
        default=None, description="Epoch timestamp of sensor observation"
    )
