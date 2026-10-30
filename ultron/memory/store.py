from typing import List, Optional
from ultron.schemas.world import WorldModel


class MemoryStore:
    """
    In-memory episodic memory store that snapshots the World Model
    at each discrete state transition (observations, plan dispatches, replans).
    """

    def __init__(self) -> None:
        self.snapshots: List[WorldModel] = []

    def record_snapshot(self, world: WorldModel) -> None:
        """Deep copies and records the current World Model state."""
        self.snapshots.append(world.model_copy(deep=True))

    def get_latest(self) -> Optional[WorldModel]:
        """Returns the most recent World Model snapshot."""
        if not self.snapshots:
            return None
        return self.snapshots[-1]

    def get_timeline(self) -> List[WorldModel]:
        """Returns the chronological sequence of World Model snapshots for mission replay."""
        return list(self.snapshots)

    def clear(self) -> None:
        """Clears memory for a fresh mission run."""
        self.snapshots.clear()
