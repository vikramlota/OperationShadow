"""Lightweight in-process Event Router for Operation Shadow (`ultron/events/router.py`).

Responsibilities:
1. Validate incoming events against `UltronEvent` (rejecting malformed payloads,
   unsupported vehicle IDs, non-finite timestamps, and excluding Emergency Stop).
2. Normalize validated `WorldModel` observations, tracked objects, sensor health,
   and vehicle state transitions into consistent `EventTypeEnum` events.
3. Assign deterministic priority (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`) and maintain
   a bounded priority queue ordered by `(-priority_rank, received_at, sequence)`.
4. Suppress duplicate events across consecutive frames using stable content signatures
   and a bounded time window (`dedup_window_sec`), while allowing material changes
   in severity, source, confidence (`>= 0.20`), spatial position (`>= 0.50m`), or
   battery (`>= 5.0%`) to pass through immediately.
5. Reject stale and out-of-order source timestamps so older events never overwrite
   newer World Model evidence.
6. Bound all in-memory queues, histories, and deduplication tables.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import heapq
import logging
import math
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from pydantic import ValidationError

from ultron.config import config as ultron_config
from ultron.events.schemas import (
    EventPriorityEnum,
    EventRouteTargetEnum,
    EventSourceEnum,
    EventTypeEnum,
    RoutedEventOutcome,
    UltronEvent,
    default_priority_for_event_type,
    priority_rank,
)
from ultron.schemas.observations import EpistemicStatusEnum, SensorSourceModeEnum
from ultron.schemas.world import (
    SensorHealthStatusEnum,
    TrackingStatusEnum,
    WorldModel,
)

logger = logging.getLogger("operation_shadow.ultron.event_router")


@dataclass
class _DedupEntry:
    signature: str
    last_seen_at: float
    last_priority: EventPriorityEnum
    last_source: EventSourceEnum
    last_confidence: Optional[float]
    last_position: Optional[Tuple[float, float, float]]
    last_battery: Optional[float]
    coalesced_count: int = 1


def _extract_position_tuple(payload: Dict[str, Any]) -> Optional[Tuple[float, float, float]]:
    pos = payload.get("position")
    if isinstance(pos, dict):
        x = pos.get("x")
        y = pos.get("y")
        z = pos.get("z", 0.0)
        if (
            isinstance(x, (int, float))
            and isinstance(y, (int, float))
            and isinstance(z, (int, float))
            and not isinstance(x, bool)
            and not isinstance(y, bool)
            and not isinstance(z, bool)
        ):
            return (float(x), float(y), float(z))
    if "location_x" in payload and "location_y" in payload:
        lx = payload.get("location_x")
        ly = payload.get("location_y")
        if (
            isinstance(lx, (int, float))
            and isinstance(ly, (int, float))
            and not isinstance(lx, bool)
            and not isinstance(ly, bool)
        ):
            return (float(lx), float(ly), 0.0)
    return None


def compute_event_signature(event: UltronEvent) -> str:
    """Compute a stable, content-based deduplication signature (never using random event_id)."""
    payload = event.evidence_payload
    label = str(
        payload.get("label")
        or payload.get("hazard_type")
        or payload.get("sensor_id")
        or payload.get("connection")
        or payload.get("vehicle_status")
        or payload.get("mission_status")
        or ""
    ).strip().lower()
    tracked_key = ",".join(sorted(event.tracked_object_ids))
    frame_key = (event.coordinate_frame or "none").strip().lower()
    veh_key = event.vehicle_id or "unassociated"
    mission_key = event.mission_id or "no_mission"

    # Include coarse spatial bucket (0.5m grid) only when no persistent tracked_object_id exists
    spatial_bucket = ""
    if not tracked_key:
        pos = _extract_position_tuple(payload)
        if pos is not None:
            spatial_bucket = f"{round(pos[0] * 2.0) / 2.0:.1f},{round(pos[1] * 2.0) / 2.0:.1f}"

    return (
        f"{event.event_type.value}|{mission_key}|{veh_key}|"
        f"{frame_key}|{tracked_key}|{label}|{spatial_bucket}"
    )


def map_event_to_db_category(event: UltronEvent) -> str:
    """Map an internal `UltronEvent` to an existing `EventCategoryEnum` string for SQLite `EVENTS`."""
    if event.event_type in (
        EventTypeEnum.HAZARD_DETECTED,
        EventTypeEnum.VEHICLE_FAULT,
        EventTypeEnum.VEHICLE_LOW_BATTERY,
    ):
        return "SAFETY"
    if event.event_type in (
        EventTypeEnum.OBSTACLE_DETECTED,
        EventTypeEnum.TARGET_DETECTED,
        EventTypeEnum.SENSOR_CONFLICT,
        EventTypeEnum.OBSERVATION_STALE,
    ):
        return "OBSERVATION"
    if event.event_type in (
        EventTypeEnum.SENSOR_DEGRADED,
        EventTypeEnum.VEHICLE_DISCONNECTED,
    ):
        return "TELEMETRY"
    if event.event_type == EventTypeEnum.MISSION_STATE_CHANGED:
        return "MISSION"
    return "SYSTEM"


class EventRouter:
    """Centralized, non-blocking, bounded in-process Event Router for ULTRON Phase 2."""

    def __init__(
        self,
        *,
        dedup_window_sec: float = 2.0,
        stale_threshold_sec: float = 5.0,
        max_queue_size: int = 128,
        max_history_size: int = 200,
        max_dedup_entries: int = 256,
        standing_order_matcher: Optional[Callable[[UltronEvent], bool]] = None,
    ) -> None:
        if dedup_window_sec < 0.0:
            raise ValueError("dedup_window_sec must be >= 0.0")
        if stale_threshold_sec <= 0.0:
            raise ValueError("stale_threshold_sec must be > 0.0")
        if max_queue_size < 1 or max_history_size < 1 or max_dedup_entries < 1:
            raise ValueError("Queue, history, and dedup limits must be >= 1")

        self._dedup_window_sec = float(dedup_window_sec)
        self._stale_threshold_sec = float(stale_threshold_sec)
        self._max_queue_size = int(max_queue_size)
        self._max_history_size = int(max_history_size)
        self._max_dedup_entries = int(max_dedup_entries)
        self._standing_order_matcher = standing_order_matcher

        self._heap: List[Tuple[int, float, int, UltronEvent]] = []
        self._sequence: int = 0
        self._history: Deque[UltronEvent] = deque(maxlen=self._max_history_size)
        self._dedup_cache: OrderedDict[str, _DedupEntry] = OrderedDict()
        self._latest_source_timestamps: OrderedDict[str, float] = OrderedDict()

        # Observability counters
        self.routed_count: int = 0
        self.suppressed_duplicate_count: int = 0
        self.rejected_out_of_order_count: int = 0
        self.rejected_stale_count: int = 0
        self.rejected_malformed_count: int = 0
        self.dropped_queue_overflow_count: int = 0

    @property
    def pending_count(self) -> int:
        return len(self._heap)

    @property
    def history(self) -> List[UltronEvent]:
        return list(self._history)

    def set_standing_order_matcher(
        self,
        matcher: Optional[Callable[[UltronEvent], bool]],
    ) -> None:
        """Register a predicate returning True when an event matches a deterministic Standing Order."""
        self._standing_order_matcher = matcher

    def clear(self) -> None:
        """Clear pending queue and deduplication state (e.g., on shutdown or test reset)."""
        self._heap.clear()
        self._dedup_cache.clear()
        self._latest_source_timestamps.clear()

    def _stream_ordering_key(self, event: UltronEvent) -> str:
        """Key used to enforce monotonic `source_timestamp` per source stream."""
        sub_key = ""
        if event.tracked_object_ids:
            sub_key = event.tracked_object_ids[0]
        elif "sensor_id" in event.evidence_payload:
            sub_key = str(event.evidence_payload["sensor_id"])
        else:
            sub_key = event.event_type.value
        return f"{event.source.value}:{event.vehicle_id or 'none'}:{sub_key}"

    def _is_material_change(self, prev: _DedupEntry, event: UltronEvent) -> bool:
        """Return True if `event` represents a significant state change that must not be suppressed."""
        assert event.priority is not None
        # 1. Severity/priority escalation
        if priority_rank(event.priority) > priority_rank(prev.last_priority):
            return True

        # 2. Different originating source
        if event.source != prev.last_source:
            return True

        # 3. Confidence jump >= 0.20
        if (
            event.confidence is not None
            and prev.last_confidence is not None
            and abs(event.confidence - prev.last_confidence) >= 0.20
        ):
            return True

        # 4. Spatial displacement >= 0.50m
        new_pos = _extract_position_tuple(event.evidence_payload)
        if new_pos is not None and prev.last_position is not None:
            dist = math.sqrt(
                (new_pos[0] - prev.last_position[0]) ** 2
                + (new_pos[1] - prev.last_position[1]) ** 2
                + (new_pos[2] - prev.last_position[2]) ** 2
            )
            if dist >= 0.50:
                return True

        # 5. Battery drop >= 5.0%
        new_bat = event.evidence_payload.get("battery")
        if (
            isinstance(new_bat, (int, float))
            and not isinstance(new_bat, bool)
            and prev.last_battery is not None
            and (prev.last_battery - float(new_bat)) >= 5.0
        ):
            return True

        return False

    def classify_route_target(self, event: UltronEvent) -> EventRouteTargetEnum:
        """Determine whether an accepted event routes to Standing Orders, LLM Triage, or Log Only."""
        if self._standing_order_matcher is not None and self._standing_order_matcher(event):
            return EventRouteTargetEnum.STANDING_ORDER

        # Known deterministic standing-order candidates (critical battery, sensor conflict, blocked route)
        if event.event_type in (
            EventTypeEnum.VEHICLE_LOW_BATTERY,
            EventTypeEnum.SENSOR_CONFLICT,
        ):
            return EventRouteTargetEnum.STANDING_ORDER

        if event.event_type == EventTypeEnum.OBSTACLE_DETECTED and bool(
            event.evidence_payload.get("blocks_route", True)
        ):
            return EventRouteTargetEnum.STANDING_ORDER

        # Informational mission lifecycle events are logged without invoking LLM triage
        if event.event_type == EventTypeEnum.MISSION_STATE_CHANGED:
            return EventRouteTargetEnum.LOG_ONLY

        return EventRouteTargetEnum.LLM_TRIAGE

    def route_event(self, raw_event: UltronEvent | Dict[str, Any]) -> RoutedEventOutcome:
        """Validate, deduplicate, prioritize, and enqueue an event without blocking."""
        # Check for E-Stop exclusion before general validation if dict
        if isinstance(raw_event, dict):
            ev_payload = raw_event.get("evidence_payload")
            if (
                isinstance(ev_payload, dict)
                and str(ev_payload.get("vehicle_status", "")).strip().upper()
                == "EMERGENCY_STOPPED"
            ):
                return RoutedEventOutcome(
                    accepted=False,
                    status="excluded_emergency_stop",
                    route_target=EventRouteTargetEnum.REJECTED,
                    reason="Emergency Stop bypasses Event Router completely.",
                )

        try:
            event = (
                raw_event
                if isinstance(raw_event, UltronEvent)
                else UltronEvent.model_validate(raw_event)
            )
        except (ValidationError, ValueError, TypeError) as exc:
            self.rejected_malformed_count += 1
            logger.warning("Rejected malformed event in EventRouter: %s", exc)
            return RoutedEventOutcome(
                accepted=False,
                status="rejected_malformed",
                route_target=EventRouteTargetEnum.REJECTED,
                reason=f"malformed_event:{exc}",
            )

        # 1. Stale & Out-of-Order source timestamp protection
        if event.source_timestamp is not None:
            stream_key = self._stream_ordering_key(event)
            latest_ts = self._latest_source_timestamps.get(stream_key)
            if latest_ts is not None and event.source_timestamp < latest_ts:
                self.rejected_out_of_order_count += 1
                logger.warning(
                    "Rejected out-of-order event %s (%s): source_timestamp=%.3f < latest=%.3f",
                    event.event_id,
                    event.event_type.value,
                    event.source_timestamp,
                    latest_ts,
                )
                return RoutedEventOutcome(
                    accepted=False,
                    status="rejected_out_of_order",
                    route_target=EventRouteTargetEnum.REJECTED,
                    reason=(
                        f"out_of_order_source_timestamp:{event.source_timestamp:.3f}<{latest_ts:.3f}"
                    ),
                    event=event,
                )

            # Reject stale source timestamps except for events explicitly reporting staleness/degradation
            # or live physical ROSBridge events where rover-to-laptop clock offset may exist
            age_sec = event.received_at - event.source_timestamp
            if (
                age_sec > self._stale_threshold_sec
                and event.source_mode != SensorSourceModeEnum.PHYSICAL
                and event.event_type
                not in (EventTypeEnum.OBSERVATION_STALE, EventTypeEnum.SENSOR_DEGRADED)
            ):
                self.rejected_stale_count += 1
                logger.warning(
                    "Rejected stale event %s (%s): age=%.2fs > threshold=%.2fs",
                    event.event_id,
                    event.event_type.value,
                    age_sec,
                    self._stale_threshold_sec,
                )
                return RoutedEventOutcome(
                    accepted=False,
                    status="rejected_stale",
                    route_target=EventRouteTargetEnum.REJECTED,
                    reason=f"stale_source_timestamp:age={age_sec:.2f}s>{self._stale_threshold_sec:.2f}s",
                    event=event,
                )

            self._latest_source_timestamps[stream_key] = event.source_timestamp
            self._latest_source_timestamps.move_to_end(stream_key)
            while len(self._latest_source_timestamps) > self._max_dedup_entries:
                self._latest_source_timestamps.popitem(last=False)

        # 2. Deterministic signature & time-bounded deduplication
        sig = event.dedup_signature or compute_event_signature(event)
        event.dedup_signature = sig
        now_ts = event.received_at

        existing = self._dedup_cache.get(sig)
        if existing is not None:
            elapsed = now_ts - existing.last_seen_at
            if elapsed < self._dedup_window_sec and not self._is_material_change(existing, event):
                existing.coalesced_count += 1
                self.suppressed_duplicate_count += 1
                return RoutedEventOutcome(
                    accepted=False,
                    status="suppressed_duplicate",
                    route_target=EventRouteTargetEnum.SUPPRESSED,
                    reason=f"duplicate_within_{self._dedup_window_sec:.2f}s_window",
                    event=event,
                    dedup_signature=sig,
                    coalesced_count=existing.coalesced_count,
                )

        assert event.priority is not None
        bat_raw = event.evidence_payload.get("battery")
        bat_val = (
            float(bat_raw)
            if isinstance(bat_raw, (int, float)) and not isinstance(bat_raw, bool)
            else None
        )
        self._dedup_cache[sig] = _DedupEntry(
            signature=sig,
            last_seen_at=now_ts,
            last_priority=event.priority,
            last_source=event.source,
            last_confidence=event.confidence,
            last_position=_extract_position_tuple(event.evidence_payload),
            last_battery=bat_val,
            coalesced_count=1,
        )
        self._dedup_cache.move_to_end(sig)
        while len(self._dedup_cache) > self._max_dedup_entries:
            self._dedup_cache.popitem(last=False)

        # 3. Classify target and enqueue in bounded priority heap
        route_target = self.classify_route_target(event)
        self._sequence += 1
        heap_item = (
            -priority_rank(event.priority),
            event.received_at,
            self._sequence,
            event,
        )
        if len(self._heap) >= self._max_queue_size:
            # Drop lowest-priority / oldest item to stay bounded
            lowest_idx = max(
                range(len(self._heap)),
                key=lambda i: (self._heap[i][0], -self._heap[i][1]),
            )
            self._heap.pop(lowest_idx)
            heapq.heapify(self._heap)
            self.dropped_queue_overflow_count += 1

        heapq.heappush(self._heap, heap_item)
        self._history.append(event)
        self.routed_count += 1

        return RoutedEventOutcome(
            accepted=True,
            status="routed",
            route_target=route_target,
            reason="accepted",
            event=event,
            dedup_signature=sig,
            coalesced_count=1,
        )

    def pop_next_event(self) -> Optional[UltronEvent]:
        """Pop and return the highest-priority pending event (`CRITICAL` -> `LOW`, FIFO on ties)."""
        if not self._heap:
            return None
        _, _, _, ev = heapq.heappop(self._heap)
        return ev

    def drain_events(self, max_items: Optional[int] = None) -> List[UltronEvent]:
        """Drain pending events in deterministic priority order."""
        out: List[UltronEvent] = []
        limit = max_items if max_items is not None else len(self._heap)
        while self._heap and len(out) < limit:
            ev = self.pop_next_event()
            if ev is not None:
                out.append(ev)
        return out

    def ingest_world_model_state(
        self,
        world: WorldModel,
        *,
        received_at: float,
    ) -> List[RoutedEventOutcome]:
        """Extract normalized events from validated `WorldModel` state and route them."""
        candidates = generate_events_from_world_model(world, received_at=received_at)
        outcomes: List[RoutedEventOutcome] = []
        for cand in candidates:
            outcomes.append(self.route_event(cand))
        return outcomes


def generate_events_from_world_model(
    world: WorldModel,
    *,
    received_at: float,
) -> List[UltronEvent]:
    """Generate normalized `UltronEvent` candidates strictly from validated `WorldModel` entities.

    Respects `docs/SIGNAL_AUDIT.md`:
    - Preserves `source_mode` (`physical` vs `simulated`) from the underlying observation/sensor/vehicle.
    - Preserves `coordinate_frame` verbatim without projecting camera-relative frames to `map`.
    - Excludes `EMERGENCY_STOPPED` (handled by E-Stop fast path).
    """
    events: List[UltronEvent] = []
    mission_id = world.mission.id or None

    # Check if SCOUT01 is in EMERGENCY_STOPPED -> never generate routing events during E-Stop
    scout = world.vehicles.get("SCOUT01")
    if scout is not None and str(scout.status).strip().upper() == "EMERGENCY_STOPPED":
        return []

    # 1. Vehicle state events (FAULT, LOW_BATTERY, DISCONNECTED)
    if scout is not None:
        v_mode = (
            SensorSourceModeEnum.PHYSICAL
            if getattr(scout.mode, "value", str(scout.mode)) == "real"
            else SensorSourceModeEnum.SIMULATED
        )
        v_status = str(scout.status).strip().upper()
        v_conn = getattr(scout.connection, "value", str(scout.connection)).strip().upper()

        if v_status == "FAULT":
            events.append(
                UltronEvent(
                    event_type=EventTypeEnum.VEHICLE_FAULT,
                    source=EventSourceEnum.VEHICLE_STATE,
                    source_mode=v_mode,
                    mission_id=mission_id,
                    vehicle_id="SCOUT01",
                    source_timestamp=scout.last_heartbeat_timestamp,
                    received_at=received_at,
                    priority=EventPriorityEnum.CRITICAL,
                    epistemic_status=EpistemicStatusEnum.OBSERVED,
                    evidence_payload={
                        "vehicle_status": v_status,
                        "connection": v_conn,
                        "battery": scout.battery,
                    },
                    summary="SCOUT01 reported operational status FAULT.",
                )
            )

        if scout.battery < 20.0:
            events.append(
                UltronEvent(
                    event_type=EventTypeEnum.VEHICLE_LOW_BATTERY,
                    source=EventSourceEnum.TELEMETRY,
                    source_mode=v_mode,
                    mission_id=mission_id,
                    vehicle_id="SCOUT01",
                    source_timestamp=scout.last_heartbeat_timestamp,
                    received_at=received_at,
                    priority=EventPriorityEnum.HIGH,
                    epistemic_status=EpistemicStatusEnum.OBSERVED,
                    evidence_payload={
                        "battery": scout.battery,
                        "threshold_pct": 20.0,
                        "position": {"x": scout.position.x, "y": scout.position.y, "z": 0.0},
                    },
                    summary=f"Critical battery ({scout.battery:.1f}%) on SCOUT01 (< 20.0% threshold).",
                )
            )

        if v_conn in ("OFFLINE", "UNKNOWN"):
            events.append(
                UltronEvent(
                    event_type=EventTypeEnum.VEHICLE_DISCONNECTED,
                    source=EventSourceEnum.VEHICLE_STATE,
                    source_mode=v_mode,
                    mission_id=mission_id,
                    vehicle_id="SCOUT01",
                    source_timestamp=scout.last_heartbeat_timestamp,
                    received_at=received_at,
                    priority=EventPriorityEnum.MEDIUM,
                    epistemic_status=EpistemicStatusEnum.UNKNOWN,
                    evidence_payload={
                        "connection": v_conn,
                        "vehicle_status": v_status,
                    },
                    summary=f"SCOUT01 telemetry connection is {v_conn}.",
                )
            )

    # 2. Unhandled Hazards
    for hazard in world.environment.hazards:
        if getattr(hazard, "handled", False):
            continue
        obs_ids = [hazard.observation_id] if hazard.observation_id else []
        events.append(
            UltronEvent(
                event_type=EventTypeEnum.HAZARD_DETECTED,
                source=EventSourceEnum.CAMERA,
                source_mode=hazard.source_mode,
                mission_id=mission_id,
                vehicle_id="SCOUT01",
                source_timestamp=hazard.timestamp,
                received_at=received_at,
                priority=EventPriorityEnum.HIGH,
                observation_ids=obs_ids,
                coordinate_frame=hazard.coordinate_frame,
                confidence=hazard.confidence,
                epistemic_status=hazard.epistemic_status,
                evidence_payload={
                    "hazard_id": hazard.id,
                    "hazard_type": hazard.type,
                    "label": hazard.type,
                    "location_x": hazard.location.x,
                    "location_y": hazard.location.y,
                },
                summary=(
                    f"Hazard '{hazard.type}' ({hazard.id}) detected at "
                    f"({hazard.location.x:.2f}, {hazard.location.y:.2f}) in '{hazard.coordinate_frame}' "
                    f"(confidence={hazard.confidence:.2f})."
                ),
            )
        )

    # 3. Tracked Objects: SENSOR_CONFLICT, OBSTACLE_DETECTED, TARGET_DETECTED, OBSERVATION_STALE
    obj_words = {
        w.lower()
        for w in world.mission.objective.replace(",", " ").replace(".", " ").split()
        if len(w) > 2
    }
    clearance_limit = max(2.5, ultron_config.route_obstacle_clearance_m * 3.0)

    for tobj in world.environment.tracked_objects.values():
        if tobj.handled:
            continue

        ev_source = (
            EventSourceEnum.ROSBRIDGE
            if tobj.source_mode == SensorSourceModeEnum.PHYSICAL
            else EventSourceEnum.CAMERA
        )
        pos_dict = {
            "x": tobj.position.x,
            "y": tobj.position.y,
            "z": tobj.position.z,
            "range_m": tobj.position.computed_range_m(),
        }

        if tobj.epistemic_status == EpistemicStatusEnum.CONFLICTED:
            events.append(
                UltronEvent(
                    event_type=EventTypeEnum.SENSOR_CONFLICT,
                    source=ev_source,
                    source_mode=tobj.source_mode,
                    mission_id=mission_id,
                    vehicle_id=tobj.vehicle_id if tobj.vehicle_id == "SCOUT01" else None,
                    source_timestamp=tobj.last_observed_timestamp,
                    received_at=received_at,
                    priority=EventPriorityEnum.HIGH,
                    observation_ids=list(tobj.contributing_observation_ids),
                    tracked_object_ids=[tobj.object_id],
                    coordinate_frame=tobj.coordinate_frame,
                    confidence=tobj.classification_confidence,
                    epistemic_status=EpistemicStatusEnum.CONFLICTED,
                    evidence_payload={
                        "label": tobj.label,
                        "position": pos_dict,
                        "conflict_details": tobj.conflict_details or "sensor measurement discrepancy",
                        "discrepant_positions": list(tobj.discrepant_positions),
                    },
                    summary=(
                        f"Conflicting sensor evidence on {tobj.object_id} ('{tobj.label}') "
                        f"in frame '{tobj.coordinate_frame}': {tobj.conflict_details or 'position mismatch'}."
                    ),
                )
            )
            continue

        if (
            tobj.epistemic_status == EpistemicStatusEnum.STALE
            and tobj.tracking_status == TrackingStatusEnum.STALE
        ):
            events.append(
                UltronEvent(
                    event_type=EventTypeEnum.OBSERVATION_STALE,
                    source=ev_source,
                    source_mode=tobj.source_mode,
                    mission_id=mission_id,
                    vehicle_id=tobj.vehicle_id if tobj.vehicle_id == "SCOUT01" else None,
                    source_timestamp=tobj.last_observed_timestamp,
                    received_at=received_at,
                    priority=EventPriorityEnum.MEDIUM,
                    observation_ids=list(tobj.contributing_observation_ids),
                    tracked_object_ids=[tobj.object_id],
                    coordinate_frame=tobj.coordinate_frame,
                    confidence=tobj.classification_confidence,
                    epistemic_status=EpistemicStatusEnum.STALE,
                    evidence_payload={
                        "label": tobj.label,
                        "position": pos_dict,
                    },
                    summary=(
                        f"Observation for '{tobj.label}' ({tobj.object_id}) in frame "
                        f"'{tobj.coordinate_frame}' has become STALE."
                    ),
                )
            )
            continue

        if tobj.is_obstacle and tobj.epistemic_status != EpistemicStatusEnum.STALE:
            range_m = tobj.position.computed_range_m()
            blocks_route = range_m <= clearance_limit
            events.append(
                UltronEvent(
                    event_type=EventTypeEnum.OBSTACLE_DETECTED,
                    source=ev_source,
                    source_mode=tobj.source_mode,
                    mission_id=mission_id,
                    vehicle_id=tobj.vehicle_id if tobj.vehicle_id == "SCOUT01" else None,
                    source_timestamp=tobj.last_observed_timestamp,
                    received_at=received_at,
                    priority=EventPriorityEnum.HIGH if blocks_route else EventPriorityEnum.MEDIUM,
                    observation_ids=list(tobj.contributing_observation_ids),
                    tracked_object_ids=[tobj.object_id],
                    coordinate_frame=tobj.coordinate_frame,
                    confidence=tobj.classification_confidence,
                    epistemic_status=tobj.epistemic_status,
                    evidence_payload={
                        "label": tobj.label,
                        "position": pos_dict,
                        "range_m": range_m,
                        "blocks_route": blocks_route,
                    },
                    summary=(
                        f"Obstacle '{tobj.label}' ({tobj.object_id}) detected at range {range_m:.2f}m "
                        f"pos=({tobj.position.x:.2f}, {tobj.position.y:.2f}) in '{tobj.coordinate_frame}'."
                    ),
                )
            )
            continue

        if not tobj.is_obstacle and not tobj.is_hazard:
            is_known_target = (
                tobj.label.lower() == world.mission.objective.lower()
                or tobj.label.lower() in {"red_package", "target"}
                or tobj.label.lower() in obj_words
            )
            events.append(
                UltronEvent(
                    event_type=EventTypeEnum.TARGET_DETECTED,
                    source=ev_source,
                    source_mode=tobj.source_mode,
                    mission_id=mission_id,
                    vehicle_id=tobj.vehicle_id if tobj.vehicle_id == "SCOUT01" else None,
                    source_timestamp=tobj.last_observed_timestamp,
                    received_at=received_at,
                    priority=EventPriorityEnum.MEDIUM if is_known_target else EventPriorityEnum.LOW,
                    observation_ids=list(tobj.contributing_observation_ids),
                    tracked_object_ids=[tobj.object_id],
                    coordinate_frame=tobj.coordinate_frame,
                    confidence=tobj.classification_confidence,
                    epistemic_status=tobj.epistemic_status,
                    evidence_payload={
                        "label": tobj.label,
                        "position": pos_dict,
                        "matches_mission_objective": is_known_target,
                    },
                    summary=(
                        f"Object/target '{tobj.label}' ({tobj.object_id}) detected at "
                        f"({tobj.position.x:.2f}, {tobj.position.y:.2f}) in '{tobj.coordinate_frame}'."
                    ),
                )
            )

    # 4. Legacy ObjectEntity targets & known_obstacles fallback
    for legacy_obj in world.environment.objects:
        if getattr(legacy_obj, "handled", False) or legacy_obj.epistemic_status == EpistemicStatusEnum.STALE:
            continue
        obs_ids = [legacy_obj.observation_id] if legacy_obj.observation_id else []
        events.append(
            UltronEvent(
                event_type=EventTypeEnum.TARGET_DETECTED,
                source=EventSourceEnum.WORLD_MODEL,
                source_mode=legacy_obj.source_mode,
                mission_id=mission_id,
                vehicle_id="SCOUT01",
                source_timestamp=legacy_obj.timestamp,
                received_at=received_at,
                priority=EventPriorityEnum.MEDIUM,
                observation_ids=obs_ids,
                coordinate_frame=legacy_obj.coordinate_frame,
                confidence=legacy_obj.confidence,
                epistemic_status=legacy_obj.epistemic_status,
                evidence_payload={
                    "object_id": legacy_obj.id,
                    "label": legacy_obj.label,
                    "location_x": legacy_obj.location.x,
                    "location_y": legacy_obj.location.y,
                    "matches_mission_objective": (
                        legacy_obj.label == world.mission.objective
                        or legacy_obj.label == "red_package"
                    ),
                },
                summary=(
                    f"Target '{legacy_obj.label}' ({legacy_obj.id}) located at "
                    f"({legacy_obj.location.x}, {legacy_obj.location.y}) in '{legacy_obj.coordinate_frame}'."
                ),
            )
        )

    if world.environment.known_obstacles:
        latest_obs = world.environment.known_obstacles[-1]
        events.append(
            UltronEvent(
                event_type=EventTypeEnum.OBSTACLE_DETECTED,
                source=EventSourceEnum.WORLD_MODEL,
                source_mode=SensorSourceModeEnum.SIMULATED,
                mission_id=mission_id,
                vehicle_id="SCOUT01",
                source_timestamp=world.environment.last_updated_timestamp,
                received_at=received_at,
                priority=EventPriorityEnum.HIGH,
                coordinate_frame="map",
                epistemic_status=EpistemicStatusEnum.OBSERVED,
                evidence_payload={
                    "label": "obstacle",
                    "location_x": latest_obs.x,
                    "location_y": latest_obs.y,
                    "blocks_route": True,
                },
                summary=f"SCOUT01 reported path blocked at ({latest_obs.x}, {latest_obs.y}).",
            )
        )

    # 5. Sensor health degradation
    for sid, sh in world.environment.sensor_health.items():
        if (
            sh.enabled
            and sh.status in (SensorHealthStatusEnum.DISCONNECTED, SensorHealthStatusEnum.ERROR)
            and sh.valid_message_count > 0
        ):
            events.append(
                UltronEvent(
                    event_type=EventTypeEnum.SENSOR_DEGRADED,
                    source=EventSourceEnum.SENSOR_HEALTH,
                    source_mode=sh.source_mode,
                    mission_id=mission_id,
                    vehicle_id=sh.vehicle_id if sh.vehicle_id == "SCOUT01" else None,
                    source_timestamp=sh.last_message_timestamp,
                    received_at=received_at,
                    priority=EventPriorityEnum.MEDIUM,
                    coordinate_frame=sh.coordinate_frame or None,
                    epistemic_status=EpistemicStatusEnum.UNKNOWN,
                    evidence_payload={
                        "sensor_id": sid,
                        "sensor_type": sh.sensor_type.value,
                        "sensor_status": sh.status.value,
                        "is_stale": sh.is_stale,
                    },
                    summary=f"Sensor '{sid}' ({sh.sensor_type.value}) transitioned to {sh.status.value}.",
                )
            )

    return events
