import uuid
from ultron.schemas.world import WorldModel, HazardEntity, ObjectEntity
from ultron.schemas.observations import StructuredObservation


def update_world_model(world: WorldModel, obs: StructuredObservation) -> WorldModel:
    """
    Deterministically merges new sensor or vision observations into the shared World Model.
    """
    if obs.event == "possible_hazard":
        hazard = HazardEntity(
            id=f"H-{uuid.uuid4().hex[:6]}",
            location=obs.location,
            confidence=obs.confidence,
            source_vehicle=obs.source_vehicle,
            type=obs.event
        )
        world.environment.hazards.append(hazard)

    elif obs.event == "target_detected":
        obj = ObjectEntity(
            id=f"O-{uuid.uuid4().hex[:6]}",
            location=obs.location,
            label=obs.visual_evidence or "unknown_target",
            confidence=obs.confidence,
            source_vehicle=obs.source_vehicle
        )
        world.environment.objects.append(obj)

    elif obs.event == "obstacle_blocked":
        world.environment.known_obstacles.append(obs.location)

    return world
