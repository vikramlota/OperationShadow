"""Event-driven intelligence layer for ULTRON (Phase 2).

Exports:
- Event schemas (`UltronEvent`, `EventTypeEnum`, `EventSourceEnum`, `EventPriorityEnum`, `RoutedEventOutcome`)
- Lightweight in-process `EventRouter`
"""

from ultron.events.router import EventRouter, compute_event_signature, map_event_to_db_category
from ultron.events.schemas import (
    EventPriorityEnum,
    EventRouteTargetEnum,
    EventSourceEnum,
    EventTypeEnum,
    RoutedEventOutcome,
    UltronEvent,
    priority_rank,
)

__all__ = [
    "EventPriorityEnum",
    "EventRouteTargetEnum",
    "EventRouter",
    "EventSourceEnum",
    "EventTypeEnum",
    "RoutedEventOutcome",
    "UltronEvent",
    "compute_event_signature",
    "map_event_to_db_category",
    "priority_rank",
]
