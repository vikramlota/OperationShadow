import time
from typing import List
from ultron.schemas.world import WorldModel, VehicleMode
from ultron.schemas.observations import StructuredObservation
from ultron.schemas.mission import VehicleID
from ultron.schemas.commands import Position2D


class SyntheticDataGenerator:
    """
    Generates plausible telemetry and injects scripted triggers for simulated vehicles.
    """
    def __init__(self, hazard_delay_sec: float = 5.0) -> None:
        self.start_time = time.time()
        self.hazard_delay_sec = hazard_delay_sec
        self.scripted_hazard_injected = False
        self.scripted_target_injected = False

    def generate_telemetry(self, world: WorldModel) -> List[StructuredObservation]:
        """
        Produces synthetic observations for any vehicle currently set to SIMULATED mode.
        """
        observations = []
        elapsed = time.time() - self.start_time

        # 1. Simulate HAWK telemetry and inject the demo's centerpiece hazard
        if world.vehicles.get("HAWK01") and world.vehicles["HAWK01"].mode == VehicleMode.SIMULATED:
            world.vehicles["HAWK01"].battery = max(0.0, 100.0 - (elapsed * 0.1))

            if elapsed >= self.hazard_delay_sec and not self.scripted_hazard_injected:
                observations.append(
                    StructuredObservation(
                        event="possible_hazard",
                        source_vehicle=VehicleID.HAWK,
                        confidence=0.89,
                        location=Position2D(x=5.1, y=3.0),
                        visual_evidence="smoke_like_pattern",
                        timestamp=time.time()
                    )
                )
                self.scripted_hazard_injected = True

        # 2. Simulate SCOUT telemetry
        if world.vehicles.get("SCOUT01") and world.vehicles["SCOUT01"].mode == VehicleMode.SIMULATED:
            world.vehicles["SCOUT01"].battery = max(0.0, 100.0 - (elapsed * 0.05))

            if self.scripted_hazard_injected and elapsed >= (self.hazard_delay_sec + 5.0) and not self.scripted_target_injected:
                observations.append(
                    StructuredObservation(
                        event="target_detected",
                        source_vehicle=VehicleID.SCOUT,
                        confidence=0.92,
                        location=Position2D(x=6.0, y=4.2),
                        visual_evidence="red_package",
                        timestamp=time.time()
                    )
                )
                self.scripted_target_injected = True

        return observations
