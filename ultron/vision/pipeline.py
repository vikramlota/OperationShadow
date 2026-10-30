import time
from typing import Optional
from ultron.schemas.observations import StructuredObservation
from ultron.schemas.mission import VehicleID
from ultron.schemas.commands import Position2D


class VisionPipeline:
    """
    Vision Pipeline running laptop-side (TRD §13).
    Processes frames captured from SCOUT or HAWK camera streams,
    detects objects and hazards, and converts them into StructuredObservation
    entities tagged with source_vehicle.
    """

    def __init__(self, confidence_threshold: float = 0.5) -> None:
        self.confidence_threshold = confidence_threshold

    def process_detection(
        self,
        source_vehicle: VehicleID,
        label: str,
        confidence: float,
        location: Position2D,
        event_type: str = "target_detected"
    ) -> Optional[StructuredObservation]:
        """
        Filters by confidence and packages detection into a StructuredObservation.
        Treats detection labels strictly as environmental data (Agent Architecture §2, §15).
        """
        if confidence < self.confidence_threshold:
            return None

        sanitized_label = str(label).strip()[:100]

        return StructuredObservation(
            event=event_type,
            source_vehicle=source_vehicle,
            confidence=round(confidence, 4),
            location=location,
            visual_evidence=sanitized_label,
            timestamp=time.time()
        )
