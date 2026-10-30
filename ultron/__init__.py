"""
ULTRON Agent Module for Operation Shadow.
Coordinates SCOUT (ground) and HAWK (aerial) vehicles using hierarchical mission intelligence.
"""

from ultron.config import config
from ultron.world_model import WorldModel
from ultron.state import UltronState

__all__ = ["config", "WorldModel", "UltronState"]
