from typing import TypedDict, Optional, List
from ultron.schemas.world import WorldModel
from ultron.schemas.mission import MissionPlan


class UltronState(TypedDict):
    world: WorldModel
    proposed_plan: Optional[MissionPlan]
    validation_errors: List[str]
    repair_prompt: Optional[str]
    retry_count: int
    trigger_rationale: Optional[str]
