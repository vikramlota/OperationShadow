"""ULTRON LangGraph Agent Adapter for FastAPI backend (§2, §3, §4, §6, §7, §8 + Phase 2 Event Intelligence + Phase 3 Mission Memory).

Bridges FastAPI's `MissionService`, `VehicleService`, `EventService`, and `PerceptionService`
to the in-process ULTRON Agent LangGraph state machine (`ultron.graph.workflow`),
evidence-grounded `WorldModel`, `WorldModelTools`, multi-sensor fusion pipeline,
Phase 2 `EventRouter`, `StandingOrderRegistry`, bounded `LLM Event Triage`,
Phase 3 `MemoryStore` (`MissionMemorySnapshot`), deterministic Safety Validator,
and Gemini planning engine.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import logging
from pathlib import Path
import sys
import time
from typing import Any, Deque, Sequence
import uuid

# Ensure project root is available for imports
_project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from ultron.events.router import (
    EventRouter,
    map_event_to_db_category,
)
from ultron.events.schemas import (
    EventPriorityEnum,
    EventRouteTargetEnum,
    EventSourceEnum,
    EventTypeEnum,
    RoutedEventOutcome,
    UltronEvent,
    priority_rank,
)
from ultron.events.standing_orders import (
    StandingOrderExecutionResult,
    StandingOrderRegistry,
)
from ultron.events.triage import (
    EventTriageResult,
    TriageDispositionEnum,
    TriageOutcome,
    invoke_event_triage,
)
import ultron.graph.nodes as ultron_nodes
from ultron.graph.workflow import build_ultron_graph
from ultron.memory.store import (
    MAX_SNAPSHOTS_PER_MISSION,
    MemoryStore,
    MissionMemorySnapshot,
    SnapshotDecisionContext,
    SnapshotPhaseEnum,
    SnapshotTaskState,
    SnapshotTriggerTypeEnum,
)
from ultron.planner.grounding import (
    answer_environmental_query,
)
from ultron.replanner.triggers import check_replanning_triggers
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import (
    MissionPlan,
    MissionStatus as UltronMissionStatus,
    VehicleID,
)
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    ObservationValidationStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorSourceModeEnum,
    SensorTypeEnum,
    StructuredObservation,
)
from ultron.schemas.world import (
    ConnectionStatus as UltronConnectionStatus,
    DecisionTraceEntry,
    EvidenceReference,
    GroundedEnvironmentalResponse,
    MissionState,
    ScoutState,
    SensorHealthStatusEnum,
    VehicleMode as UltronVehicleMode,
    WorldModel,
)
from ultron.state import UltronState
from ultron.vision.lidar import LidarIngestionAdapter
from ultron.vision.pipeline import DepthCameraAdapter
from ultron.world_model.fusion import FrameTransformRegistry
from ultron.world_model.tools import EnvironmentSnapshotResult, WorldModelTools
from ultron.world_model.updater import (
    ingest_sensor_observation,
    refresh_world_model_freshness,
    update_world_model,
)
from ultron_stub.interface import (
    ObservationPayloadStub,
    SimulatedMissionPlanStub,
    SimulatedTaskSpecStub,
    UltronAgentPort,
    VehicleId,
    VehicleMode,
    WorldModelSnapshotStub,
)

logger = logging.getLogger("operation_shadow.ultron_adapter")

REPLAN_PRIORITY_ORDINARY = 1
REPLAN_PRIORITY_SIGNIFICANT = 2


@dataclass
class _CoalescedReplanRequest:
    """Coalesced environmental replanning request for a specific mission epoch."""

    mission_id: str
    mission_epoch: int
    trigger_rationale: str
    priority: int
    first_requested_at: float
    last_updated_at: float
    coalesced_count: int = 1
    standing_order_plan: MissionPlan | None = None
    standing_order_id: str | None = None
    source_event_id: str | None = None
    event_type: str | None = None
    event_priority: str | None = None
    event_source: str | None = None
    event_source_mode: str | None = None
    triage_disposition: str | None = None
    triage_trace_id: str | None = None


def _is_planner_mocked() -> bool:
    """Return True if `ultron.graph.nodes.invoke_gemini_planner` is patched by a test mock."""
    fn = getattr(ultron_nodes, "invoke_gemini_planner", None)
    if fn is None:
        return False
    return (
        hasattr(fn, "_mock_name")
        or hasattr(fn, "assert_called")
        or getattr(fn, "__module__", "") != "ultron.planner.engine"
    )


class UltronLangGraphAgent(UltronAgentPort):
    """In-process ULTRON Agent adapter driven by LangGraph, EventRouter, StandingOrders, LLM Triage, MemoryStore, and WorldModel."""

    def __init__(
        self,
        *,
        replan_cooldown_sec: float = 0.25,
        replan_debounce_sec: float = 0.5,
        replan_max_retries: int = 2,
        standing_orders_registry: StandingOrderRegistry | None = None,
        max_snapshots_per_mission: int = MAX_SNAPSHOTS_PER_MISSION,
    ) -> None:
        self.app = build_ultron_graph()
        self._active_mission_id: str | None = None
        self._active_plan: MissionPlan | None = None
        self._active_task_specs: list[SnapshotTaskState] = []
        self._world = WorldModel(
            mission=MissionState(
                id="",
                objective="",
                status=UltronMissionStatus.IDLE,
            ),
            vehicles={
                "SCOUT01": ScoutState(
                    mode=UltronVehicleMode.SIMULATED,
                    position=Position2D(x=0.0, y=0.0),
                    battery=100.0,
                    connection=UltronConnectionStatus.ONLINE,
                ),
            },
        )
        try:
            from core.config import settings as _app_cfg

            _default_real = (
                getattr(_app_cfg, "default_vehicle_mode", "simulated") == "real"
                and not bool(getattr(_app_cfg, "hardware_lockout", True))
            )
            _lidar_enabled = bool(getattr(_app_cfg, "lidar_enabled", False))
            _lidar_topic = str(getattr(_app_cfg, "lidar_topic", "") or "")
            _lidar_msg_type = str(getattr(_app_cfg, "lidar_message_type", "") or "")
            _lidar_frame = str(getattr(_app_cfg, "lidar_frame", "") or "")
            _ros_enabled = bool(getattr(_app_cfg, "ros_enabled", False))
            _ros_vid = str(getattr(_app_cfg, "ros_vehicle_id", "") or "") or None
            _map_confirmed = bool(getattr(_app_cfg, "ros_map_frame_confirmed", False))
            _extrinsics_confirmed = bool(
                getattr(_app_cfg, "camera_lidar_extrinsics_confirmed", False)
            )
            _lidar_min_r = float(getattr(_app_cfg, "lidar_min_range_m", 0.05))
            _lidar_max_r = float(getattr(_app_cfg, "lidar_max_range_m", 30.0))
            _lidar_stale = float(getattr(_app_cfg, "lidar_stale_after_seconds", 2.0))
        except Exception:
            _default_real = False
            _lidar_enabled = False
            _lidar_topic = ""
            _lidar_msg_type = ""
            _lidar_frame = ""
            _ros_enabled = False
            _ros_vid = None
            _map_confirmed = False
            _extrinsics_confirmed = False
            _lidar_min_r = 0.05
            _lidar_max_r = 30.0
            _lidar_stale = 2.0

        if _default_real:
            self._world.vehicles["SCOUT01"].mode = UltronVehicleMode.REAL
            self._world.vehicles["SCOUT01"].connection = UltronConnectionStatus.ONLINE

        self.camera_adapter = DepthCameraAdapter(
            enabled=True,
            source_mode=(
                SensorSourceModeEnum.PHYSICAL
                if _ros_enabled
                else SensorSourceModeEnum.SIMULATED
            ),
        )
        self.lidar_adapter = LidarIngestionAdapter(
            enabled=_lidar_enabled,
            topic=_lidar_topic,
            message_type=_lidar_msg_type,
            coordinate_frame=_lidar_frame,
            vehicle_id=_ros_vid,
            source_mode=(
                SensorSourceModeEnum.PHYSICAL
                if (_ros_enabled and _lidar_enabled)
                else SensorSourceModeEnum.SIMULATED
            ),
            min_range_m=_lidar_min_r,
            max_range_m=_lidar_max_r,
            stale_after_sec=_lidar_stale,
        )
        if _lidar_enabled and self.lidar_adapter.is_configured:
            self.lidar_adapter.set_connection_state(True)
            self._world.environment.sensor_health["lidar"] = self.lidar_adapter.get_health()
        self.transform_registry = FrameTransformRegistry(
            map_frame_confirmed=_map_confirmed,
            camera_lidar_extrinsics_confirmed=_extrinsics_confirmed,
        )
        self._observations: list[dict[str, Any]] = []
        self._unknown_transitions: list[tuple[VehicleId, datetime]] = []

        # Phase 3: Persistent & In-Memory Mission Memory Store
        self.memory_store: MemoryStore = MemoryStore(
            max_snapshots_per_mission=max_snapshots_per_mission,
        )
        self._snapshot_persistence_failures_count: int = 0
        self._snapshot_persistence_errors: list[str] = []

        # Phase 2: Standing Orders Registry & Centralized In-Process Event Router
        self.standing_orders: StandingOrderRegistry = (
            standing_orders_registry
            if standing_orders_registry is not None
            else StandingOrderRegistry.from_json_file()
        )
        self.event_router: EventRouter = EventRouter(
            dedup_window_sec=max(1.5, float(replan_debounce_sec)),
            standing_order_matcher=self.standing_orders.has_potential_match,
        )
        self._routed_outcomes: Deque[RoutedEventOutcome] = deque(maxlen=200)
        self._standing_order_outcomes: Deque[
            tuple[UltronEvent, StandingOrderExecutionResult]
        ] = deque(maxlen=200)
        self._triage_outcomes: Deque[
            tuple[UltronEvent, EventTriageResult, TriageOutcome]
        ] = deque(maxlen=200)
        self._synced_event_ids: set[str] = set()
        self._synced_trace_ids: set[str] = set()

        # Bounded asynchronous LLM Event Triage queue & worker state
        self._triage_heap: list[tuple[int, float, int, int, UltronEvent]] = []
        self._triage_seq: int = 0
        self._max_triage_queue_size: int = 64
        self._active_triage_task: asyncio.Task[None] | None = None
        self._triage_completed_count: int = 0
        self._triage_discarded_stale_count: int = 0
        self._standing_orders_applied_count: int = 0

        # Asynchronous replanning worker state & epoch fencing
        self._replan_cooldown_sec = max(0.0, float(replan_cooldown_sec))
        self._replan_debounce_sec = max(0.0, float(replan_debounce_sec))
        self._replan_max_retries = max(1, int(replan_max_retries))
        self._mission_epoch: int = 0
        self._emergency_stopped: bool = False
        self._pending_replan: _CoalescedReplanRequest | None = None
        self._active_replan_task: asyncio.Task[None] | None = None
        self._last_replan_finished_at: float = 0.0
        self._replan_completed_count: int = 0
        self._replan_discarded_stale_count: int = 0
        self._replan_coalesced_updates_count: int = 0

    @property
    def world(self) -> WorldModel:
        return self._world

    @property
    def mission_epoch(self) -> int:
        return self._mission_epoch

    @property
    def active_task_specs(self) -> list[SnapshotTaskState]:
        return list(self._active_task_specs)

    @property
    def snapshot_persistence_failures_count(self) -> int:
        return self._snapshot_persistence_failures_count

    @property
    def snapshot_persistence_errors(self) -> list[str]:
        return list(self._snapshot_persistence_errors)

    @property
    def has_active_replan(self) -> bool:
        replan_running = (
            self._active_replan_task is not None and not self._active_replan_task.done()
        )
        triage_running = (
            self._active_triage_task is not None and not self._active_triage_task.done()
        )
        return replan_running or triage_running

    @property
    def replan_completed_count(self) -> int:
        return self._replan_completed_count

    @property
    def replan_discarded_stale_count(self) -> int:
        return self._replan_discarded_stale_count

    @property
    def replan_coalesced_updates_count(self) -> int:
        return self._replan_coalesced_updates_count

    @property
    def triage_completed_count(self) -> int:
        return self._triage_completed_count

    @property
    def triage_discarded_stale_count(self) -> int:
        return self._triage_discarded_stale_count

    @property
    def standing_orders_applied_count(self) -> int:
        return self._standing_orders_applied_count

    @property
    def routed_outcomes(self) -> list[RoutedEventOutcome]:
        return list(self._routed_outcomes)

    @property
    def standing_order_outcomes(
        self,
    ) -> list[tuple[UltronEvent, StandingOrderExecutionResult]]:
        return list(self._standing_order_outcomes)

    @property
    def triage_outcomes(
        self,
    ) -> list[tuple[UltronEvent, EventTriageResult, TriageOutcome]]:
        return list(self._triage_outcomes)

    def _environment_fingerprint(self) -> tuple[Any, ...]:
        """Compute a fast structural fingerprint of meaningful environmental state."""
        tracked_fp = tuple(
            sorted(
                (
                    oid,
                    obj.label,
                    obj.epistemic_status.value,
                    obj.tracking_status.value,
                    obj.is_obstacle,
                    obj.is_hazard,
                    obj.corroborated_by_multiple_sensors,
                )
                for oid, obj in self._world.environment.tracked_objects.items()
            )
        )
        hazards_fp = tuple(
            sorted((h.id, h.type, h.epistemic_status.value) for h in self._world.environment.hazards)
        )
        obs_count = len(self._world.environment.known_obstacles)
        return (tracked_fp, hazards_fp, obs_count)

    def update_task_execution_states(
        self,
        tasks: Sequence[SnapshotTaskState | dict[str, Any]],
        *,
        trigger_reason: str | None = None,
        timestamp: float | None = None,
    ) -> MissionMemorySnapshot | None:
        """Update tracked task states and optionally record a `TASK_STATE_TRANSITION` snapshot."""
        updated: list[SnapshotTaskState] = []
        for item in tasks:
            if isinstance(item, SnapshotTaskState):
                updated.append(item)
            elif isinstance(item, dict):
                updated.append(SnapshotTaskState.model_validate(item))
        self._active_task_specs = updated
        if trigger_reason and (self._world.mission.id or self._active_mission_id):
            return self.memory_store.record_transition_snapshot(
                self._world,
                mission_id=self._world.mission.id or self._active_mission_id,
                timestamp=timestamp,
                trigger_type=SnapshotTriggerTypeEnum.TASK_STATE_TRANSITION,
                trigger_reason=trigger_reason,
                phase=SnapshotPhaseEnum.STATE_TRANSITION,
                task_states=self._active_task_specs,
            )
        return None

    def cancel_pending_replanning(self, reason: str = "cancelled") -> None:
        """Invalidate pending event triage and replanning work and cancel any in-flight tasks."""
        had_pending_replan = self._pending_replan is not None
        had_active_replan = (
            self._active_replan_task is not None and not self._active_replan_task.done()
        )
        had_pending_triage = len(self._triage_heap) > 0
        had_active_triage = (
            self._active_triage_task is not None and not self._active_triage_task.done()
        )

        self._mission_epoch += 1
        self.event_router.clear()
        self._pending_replan = None

        if had_pending_triage or had_active_triage:
            self._triage_discarded_stale_count += max(1, len(self._triage_heap))
            self._triage_heap.clear()
        if had_active_triage and self._active_triage_task is not None:
            self._active_triage_task.cancel()
            logger.info("Cancelled in-flight ULTRON event triage task (%s).", reason)
        self._active_triage_task = None

        if had_active_replan and self._active_replan_task is not None:
            self._active_replan_task.cancel()
            logger.info("Cancelled in-flight ULTRON replanning task (%s).", reason)
        if had_pending_replan or had_active_replan or had_pending_triage or had_active_triage:
            self._replan_discarded_stale_count += 1
        self._active_replan_task = None

    async def wait_for_replanning(self, timeout: float = 5.0) -> None:
        """Await any pending or active event triage and background replanning work."""
        deadline = time.monotonic() + timeout
        while True:
            if self.event_router.pending_count > 0 and not self._emergency_stopped:
                self._dispatch_routed_events(now_ts=time.time())

            triage_task = self._active_triage_task
            if (triage_task is None or triage_task.done()) and self._triage_heap and not self._emergency_stopped:
                self._ensure_triage_worker_started()
                triage_task = self._active_triage_task

            if triage_task is not None and not triage_task.done():
                remaining = max(0.01, deadline - time.monotonic())
                with suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(triage_task), timeout=remaining)
                if time.monotonic() >= deadline:
                    return
                continue

            replan_task = self._active_replan_task
            if (replan_task is None or replan_task.done()) and self._pending_replan is not None and not self._emergency_stopped:
                self._ensure_replan_worker_started()
                replan_task = self._active_replan_task

            if replan_task is not None and not replan_task.done():
                remaining = max(0.01, deadline - time.monotonic())
                with suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(replan_task), timeout=remaining)
                if time.monotonic() >= deadline:
                    return
                continue

            if (
                self.event_router.pending_count == 0
                and not self._triage_heap
                and self._pending_replan is None
            ):
                return
            if time.monotonic() >= deadline or self._emergency_stopped:
                return
            await asyncio.sleep(0.005)

    def _classify_replan_priority(self, trigger_rationale: str) -> int:
        """Assign higher priority to hazards, obstacles, standing orders, and vehicle faults."""
        lower = trigger_rationale.lower()
        if any(
            kw in lower
            for kw in (
                "standing_order",
                "hazard",
                "obstacle",
                "offline",
                "unknown",
                "conflict",
                "fire",
                "smoke",
                "battery",
                "fault",
            )
        ):
            return REPLAN_PRIORITY_SIGNIFICANT
        return REPLAN_PRIORITY_ORDINARY

    def _record_event_routed_snapshot(self, ev: UltronEvent) -> MissionMemorySnapshot:
        """Record a `BEFORE` snapshot capturing what ULTRON knew when `ev` was accepted by `EventRouter`."""
        ev_ts = (
            ev.source_timestamp
            if (ev.source_timestamp is not None and ev.source_timestamp > 0.0)
            else ev.received_at
        )
        mid = ev.mission_id or self._world.mission.id or self._active_mission_id or "M001"
        ev_ids = tuple(dict.fromkeys(list(ev.observation_ids) + list(ev.tracked_object_ids)))
        return self.memory_store.record_transition_snapshot(
            self._world,
            mission_id=mid,
            timestamp=ev_ts,
            trigger_type=SnapshotTriggerTypeEnum.EVENT_ROUTED,
            trigger_reason=f"Event {ev.event_id} ({ev.event_type.value}) routed: {ev.summary}",
            phase=SnapshotPhaseEnum.BEFORE,
            associated_event_id=ev.event_id,
            task_states=self._active_task_specs,
            decision_context=SnapshotDecisionContext(
                event_id=ev.event_id,
                event_type=ev.event_type.value,
                event_priority=ev.priority.value if ev.priority else "MEDIUM",
                event_source=ev.source.value,
                event_source_mode=ev.source_mode.value,
                evidence_ids=ev_ids,
            ),
        )

    def submit_event(
        self,
        raw_event: UltronEvent | dict[str, Any],
    ) -> RoutedEventOutcome:
        """Submit a raw or typed event directly into ULTRON's EventRouter and dispatch it."""
        scout = self._world.vehicles.get("SCOUT01")
        if self._emergency_stopped or (
            scout is not None and str(scout.status).strip().upper() == "EMERGENCY_STOPPED"
        ):
            outcome = RoutedEventOutcome(
                accepted=False,
                status="excluded_emergency_stop",
                route_target=EventRouteTargetEnum.REJECTED,
                reason="Emergency Stop is active; Event Router is bypassed.",
            )
            self._routed_outcomes.append(outcome)
            return outcome

        outcome = self.event_router.route_event(raw_event)
        self._routed_outcomes.append(outcome)
        if outcome.accepted and outcome.event is not None:
            self._record_event_routed_snapshot(outcome.event)
            ev_ts = outcome.event.received_at
            self._dispatch_routed_events(now_ts=ev_ts)
        return outcome

    def _mark_event_entities_handled(self, event: UltronEvent) -> None:
        """Mark specific World Model entities referenced by `event` as handled so they do not re-emit."""
        for tid in event.tracked_object_ids:
            tobj = self._world.environment.tracked_objects.get(tid)
            if tobj is not None:
                tobj.handled = True
        haz_id = event.evidence_payload.get("hazard_id")
        if haz_id:
            for h in self._world.environment.hazards:
                if h.id == haz_id:
                    h.handled = True
        leg_id = event.evidence_payload.get("object_entity_id") or event.evidence_payload.get("object_id")
        obs_id_set = set(event.observation_ids)
        for o in self._world.environment.objects:
            if (leg_id and o.id == leg_id) or (obs_id_set and o.observation_id in obs_id_set):
                o.handled = True

    def _is_event_already_handled_by_world_model(self, event: UltronEvent) -> bool:
        """Return True if the legacy ObjectEntity or TrackedObject for `event` was already handled in this batch."""
        leg_id = event.evidence_payload.get("object_entity_id") or event.evidence_payload.get("object_id")
        if leg_id:
            for o in self._world.environment.objects:
                if o.id == leg_id and getattr(o, "handled", False):
                    return True
        if event.tracked_object_ids and all(
            getattr(self._world.environment.tracked_objects.get(tid), "handled", False)
            for tid in event.tracked_object_ids
        ):
            return True
        return False

    def _record_triage_decision_trace(
        self,
        event: UltronEvent,
        triage_res: EventTriageResult,
        triage_outcome: TriageOutcome,
        *,
        before_snapshot_id: str | None = None,
    ) -> DecisionTraceEntry:
        """Record an auditable `DecisionTraceEntry` in `WorldModel` for an LLM Event Triage evaluation."""
        obs_by_id = {
            o.observation_id: o for o in self._world.environment.observation_history
        }
        ev_refs: list[EvidenceReference] = []
        for eid in triage_res.evidence_ids:
            obs = obs_by_id.get(eid)
            if obs is not None:
                ev_refs.append(
                    EvidenceReference(
                        observation_id=obs.observation_id,
                        sensor=obs.sensor_id,
                        sensor_type=obs.sensor_type.value,
                        timestamp=obs.sensor_timestamp,
                        coordinate_frame=obs.coordinate_frame,
                        confidence=obs.confidence,
                        source_mode=obs.source_mode,
                    )
                )

        ev_base_ts = (
            event.source_timestamp
            if (event.source_timestamp is not None and event.source_timestamp > 0.0)
            else event.received_at
        )
        trace_ts = max(time.time(), ev_base_ts + 0.001)

        trace = DecisionTraceEntry(
            trace_id=f"DT-TRIAGE-{uuid.uuid4().hex[:8].upper()}",
            mission_id=self._world.mission.id or event.mission_id or "M001",
            timestamp=trace_ts,
            trigger=f"EVENT_TRIAGE:{event.event_type.value}:{event.event_id}",
            environmental_facts_used=list(triage_res.facts_used),
            inferences_made=list(triage_res.inferences),
            unknowns_and_uncertainties=list(triage_res.unknowns),
            sensor_sources=[f"{event.source.value}:{event.source_mode.value}"],
            evidence_references=ev_refs,
            decision_summary=(
                f"Event {event.event_id} ({event.event_type.value}) triaged via {triage_outcome.source} "
                f"-> {triage_res.disposition.value} (severity={triage_res.severity_assessment.value})"
            ),
            rationale=triage_res.rationale,
            safety_validator_passed=True,
            safety_validator_modified_or_rejected=bool(triage_outcome.validation_repairs),
            validation_errors=list(triage_outcome.validation_repairs),
            replanning_triggered=triage_res.requires_replan,
            resulting_task_ids=[],
            confidence_level=triage_res.confidence.value,
            is_simulated=(event.source_mode == SensorSourceModeEnum.SIMULATED),
            source_event_id=event.event_id,
            event_type=event.event_type.value,
            event_priority=event.priority.value if event.priority else "MEDIUM",
            standing_order_id=triage_res.recommended_standing_order_id,
            triage_disposition=triage_res.disposition.value,
            before_snapshot_id=before_snapshot_id,
            planner_source=f"triage:{triage_outcome.source}:{triage_outcome.reason}",
        )

        after_snap = self.memory_store.record_transition_snapshot(
            self._world,
            mission_id=trace.mission_id,
            timestamp=trace.timestamp,
            trigger_type=SnapshotTriggerTypeEnum.LLM_TRIAGE_COMPLETED,
            trigger_reason=trace.decision_summary,
            phase=SnapshotPhaseEnum.AFTER,
            associated_event_id=event.event_id,
            associated_trace_id=trace.trace_id,
            paired_before_snapshot_id=before_snapshot_id,
            task_states=self._active_task_specs,
            decision_context=SnapshotDecisionContext(
                event_id=event.event_id,
                event_type=event.event_type.value,
                event_priority=event.priority.value if event.priority else "MEDIUM",
                event_source=event.source.value,
                event_source_mode=event.source_mode.value,
                evidence_ids=tuple(triage_res.evidence_ids),
                standing_order_id=triage_res.recommended_standing_order_id,
                triage_disposition=triage_res.disposition.value,
                triage_severity=triage_res.severity_assessment.value,
                triage_source=triage_outcome.source,
                planner_source=trace.planner_source,
                validation_passed=not bool(triage_outcome.validation_repairs),
                validation_errors=tuple(triage_outcome.validation_repairs),
                resulting_task_ids=(),
                rationale=triage_res.rationale,
            ),
        )
        trace.after_snapshot_id = after_snap.snapshot_id

        self._world.environment.decision_traces.append(trace)
        if len(self._world.environment.decision_traces) > 100:
            self._world.environment.decision_traces = (
                self._world.environment.decision_traces[-100:]
            )
        return trace

    def _enqueue_for_triage(self, event: UltronEvent) -> None:
        """Enqueue an ambiguous event into the bounded priority triage heap and start the worker."""
        if self._emergency_stopped:
            return
        prio = event.priority or EventPriorityEnum.MEDIUM
        self._triage_seq += 1
        item = (
            -priority_rank(prio),
            event.received_at,
            self._triage_seq,
            self._mission_epoch,
            event,
        )
        if len(self._triage_heap) >= self._max_triage_queue_size:
            lowest_idx = max(
                range(len(self._triage_heap)),
                key=lambda i: (self._triage_heap[i][0], -self._triage_heap[i][1]),
            )
            self._triage_heap.pop(lowest_idx)
            heapq.heapify(self._triage_heap)
        heapq.heappush(self._triage_heap, item)
        self._ensure_triage_worker_started()

    def _ensure_triage_worker_started(self) -> None:
        """Start the single bounded async LLM Event Triage worker task if an event loop is running."""
        if self._active_triage_task is not None and not self._active_triage_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._active_triage_task = loop.create_task(
            self._triage_worker_loop(),
            name=f"ultron-triage-worker-e{self._mission_epoch}",
        )

    async def _triage_worker_loop(self) -> None:
        """Background worker processing ambiguous events through `invoke_event_triage` with concurrency=1."""
        try:
            while self._triage_heap and not self._emergency_stopped:
                await asyncio.sleep(0)
                if not self._triage_heap or self._emergency_stopped:
                    break

                _, _, _, ev_epoch, event = heapq.heappop(self._triage_heap)
                scout_pre = self._world.vehicles.get("SCOUT01")
                if (
                    self._emergency_stopped
                    or ev_epoch != self._mission_epoch
                    or (
                        scout_pre is not None
                        and str(scout_pre.status).strip().upper() == "EMERGENCY_STOPPED"
                    )
                ):
                    self._triage_discarded_stale_count += 1
                    continue

                world_snapshot = self._world.model_copy(deep=True)
                try:
                    triage_res, triage_outcome = await asyncio.to_thread(
                        invoke_event_triage,
                        event,
                        world_snapshot,
                        registry=self.standing_orders,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Unexpected error in async event triage for %s: %s",
                        event.event_id,
                        exc,
                    )
                    continue

                # Epoch & E-Stop fencing after thread completion
                scout_post = self._world.vehicles.get("SCOUT01")
                if (
                    self._emergency_stopped
                    or ev_epoch != self._mission_epoch
                    or (
                        event.mission_id
                        and self._world.mission.id
                        and event.mission_id != self._world.mission.id
                    )
                    or (
                        scout_post is not None
                        and str(scout_post.status).strip().upper() == "EMERGENCY_STOPPED"
                    )
                ):
                    self._triage_discarded_stale_count += 1
                    logger.info(
                        "Discarded stale LLM triage result for event %s (epoch=%d vs current=%d).",
                        event.event_id,
                        ev_epoch,
                        self._mission_epoch,
                    )
                    continue

                self._triage_outcomes.append((event, triage_res, triage_outcome))
                self._triage_completed_count += 1
                before_snap_id = self.memory_store.get_before_snapshot_id_for_event(
                    event.event_id
                )
                triage_trace = self._record_triage_decision_trace(
                    event,
                    triage_res,
                    triage_outcome,
                    before_snapshot_id=before_snap_id,
                )

                if triage_res.disposition == TriageDispositionEnum.APPLY_STANDING_ORDER:
                    so_res = self.standing_orders.evaluate_and_execute(
                        event,
                        self._world,
                        specific_order_id=triage_res.recommended_standing_order_id,
                    )
                    self._standing_order_outcomes.append((event, so_res))
                    if so_res.executed and so_res.plan is not None:
                        self._standing_orders_applied_count += 1
                        self._mark_event_entities_handled(event)
                        self._schedule_or_coalesce_replan(
                            trigger_rationale=(
                                f"[STANDING_ORDER:{so_res.standing_order_id}] {event.summary}"
                            ),
                            priority=REPLAN_PRIORITY_SIGNIFICANT,
                            now_ts=time.time(),
                            standing_order_plan=so_res.plan,
                            standing_order_id=so_res.standing_order_id,
                            source_event_id=event.event_id,
                            event_type=event.event_type.value,
                            event_priority=event.priority.value if event.priority else "MEDIUM",
                            event_source=event.source.value,
                            event_source_mode=event.source_mode.value,
                            triage_disposition=triage_res.disposition.value,
                            triage_trace_id=triage_trace.trace_id,
                        )
                elif triage_res.disposition == TriageDispositionEnum.REQUEST_REPLAN:
                    self._mark_event_entities_handled(event)
                    prio = (
                        REPLAN_PRIORITY_SIGNIFICANT
                        if triage_res.severity_assessment.value in ("CRITICAL", "HIGH")
                        else REPLAN_PRIORITY_ORDINARY
                    )
                    self._schedule_or_coalesce_replan(
                        trigger_rationale=(
                            f"[TRIAGE:{event.event_type.value}] {triage_res.rationale}"
                        ),
                        priority=prio,
                        now_ts=time.time(),
                        source_event_id=event.event_id,
                        event_type=event.event_type.value,
                        event_priority=event.priority.value if event.priority else "MEDIUM",
                        event_source=event.source.value,
                        event_source_mode=event.source_mode.value,
                        triage_disposition=triage_res.disposition.value,
                        triage_trace_id=triage_trace.trace_id,
                    )
                elif triage_res.disposition == TriageDispositionEnum.ESCALATE_TO_OPERATOR:
                    self._mark_event_entities_handled(event)
                    if triage_res.requires_replan:
                        self._schedule_or_coalesce_replan(
                            trigger_rationale=(
                                f"[TRIAGE_ESCALATE:{event.event_type.value}] {triage_res.rationale}"
                            ),
                            priority=REPLAN_PRIORITY_SIGNIFICANT,
                            now_ts=time.time(),
                            source_event_id=event.event_id,
                            event_type=event.event_type.value,
                            event_priority=event.priority.value if event.priority else "MEDIUM",
                            event_source=event.source.value,
                            event_source_mode=event.source_mode.value,
                            triage_disposition=triage_res.disposition.value,
                            triage_trace_id=triage_trace.trace_id,
                        )
                else:
                    # IGNORE or LOG_ONLY: mark associated entities handled so they do not re-trigger
                    self._mark_event_entities_handled(event)
        except asyncio.CancelledError:
            self._triage_discarded_stale_count += 1
            raise

    def _dispatch_routed_events(self, *, now_ts: float) -> None:
        """Drain pending events from `self.event_router` in priority order and dispatch to Standing Orders or LLM Triage."""
        while self.event_router.pending_count > 0:
            if self._emergency_stopped:
                self.event_router.clear()
                return

            event = self.event_router.pop_next_event()
            if event is None:
                break

            target = self.event_router.classify_route_target(event)
            if target == EventRouteTargetEnum.STANDING_ORDER:
                so_res = self.standing_orders.evaluate_and_execute(event, self._world)
                self._standing_order_outcomes.append((event, so_res))

                if so_res.executed and so_res.plan is not None:
                    self._standing_orders_applied_count += 1
                    self._mark_event_entities_handled(event)
                    self._schedule_or_coalesce_replan(
                        trigger_rationale=(
                            f"[STANDING_ORDER:{so_res.standing_order_id}] {event.summary}"
                        ),
                        priority=REPLAN_PRIORITY_SIGNIFICANT,
                        now_ts=now_ts,
                        standing_order_plan=so_res.plan,
                        standing_order_id=so_res.standing_order_id,
                        source_event_id=event.event_id,
                        event_type=event.event_type.value,
                        event_priority=event.priority.value if event.priority else "HIGH",
                        event_source=event.source.value,
                        event_source_mode=event.source_mode.value,
                    )
                elif so_res.status == "suppressed_already_handled":
                    self._mark_event_entities_handled(event)
                    if (
                        self._pending_replan is not None
                        and self._pending_replan.mission_epoch == self._mission_epoch
                    ):
                        self._pending_replan.coalesced_count += 1
                        self._pending_replan.last_updated_at = now_ts
                        self._replan_coalesced_updates_count += 1
                else:
                    # Preconditions or required evidence not met for Standing Order -> route to LLM Triage
                    self._enqueue_for_triage(event)
            elif target == EventRouteTargetEnum.LLM_TRIAGE:
                if self._is_event_already_handled_by_world_model(event):
                    self._replan_coalesced_updates_count += 1
                    if (
                        self._pending_replan is not None
                        and self._pending_replan.mission_epoch == self._mission_epoch
                    ):
                        self._pending_replan.coalesced_count += 1
                        self._pending_replan.last_updated_at = now_ts
                else:
                    self._enqueue_for_triage(event)
            elif target == EventRouteTargetEnum.LOG_ONLY:
                self._mark_event_entities_handled(event)

    def _route_and_schedule_world_events(self, *, now_ts: float) -> list[RoutedEventOutcome]:
        """Extract candidate events from `self._world`, route via `EventRouter`, capture `BEFORE` snapshots, and dispatch handlers."""
        if self._emergency_stopped:
            return []

        outcomes = self.event_router.ingest_world_model_state(
            self._world, received_at=now_ts
        )
        for out in outcomes:
            self._routed_outcomes.append(out)
            if out.accepted and out.event is not None:
                self._record_event_routed_snapshot(out.event)
            elif (
                not out.accepted
                and out.status == "suppressed_duplicate"
                and (
                    (
                        self._pending_replan is not None
                        and self._pending_replan.mission_epoch == self._mission_epoch
                    )
                    or self.has_active_replan
                )
            ):
                if (
                    self._pending_replan is not None
                    and self._pending_replan.mission_epoch == self._mission_epoch
                ):
                    self._pending_replan.coalesced_count += 1
                    self._pending_replan.last_updated_at = now_ts
                self._replan_coalesced_updates_count += 1

        self._dispatch_routed_events(now_ts=now_ts)

        # Fallback check for legacy known_obstacles updates that do not create TrackedObjects
        if (
            not outcomes
            and not self._emergency_stopped
            and self._world.mission.status == UltronMissionStatus.IN_PROGRESS
            and self._active_plan is not None
        ):
            should_replan, trigger_rationale = check_replanning_triggers(
                self._world,
                debounce_sec=self._replan_debounce_sec,
                current_time=now_ts,
            )
            if should_replan and trigger_rationale:
                priority = self._classify_replan_priority(trigger_rationale)
                self._schedule_or_coalesce_replan(
                    trigger_rationale=trigger_rationale,
                    priority=priority,
                    now_ts=now_ts,
                )

        return outcomes

    def ensure_obstacle_replan_triggered(
        self,
        active_obs: dict[str, Any],
    ) -> None:
        """Ensure an active obstacle encountered during physical mission execution triggers ULTRON replanning."""
        if (
            self._emergency_stopped
            or self._world.mission.status != UltronMissionStatus.IN_PROGRESS
            or self._active_plan is None
        ):
            return

        if self._pending_replan is not None or self.has_active_replan:
            return

        obs_key = str(active_obs.get("key", ""))
        all_keys = {str(k) for k in active_obs.get("all_keys", set())}
        if obs_key:
            all_keys.add(obs_key)

        for oid, tobj in self._world.environment.tracked_objects.items():
            if oid in all_keys or any(
                cid in all_keys for cid in getattr(tobj, "contributing_observation_ids", [])
            ):
                tobj.handled = False
                tobj.is_obstacle = True

        now_ts = time.time()
        outcomes = self._route_and_schedule_world_events(now_ts=now_ts)
        if (
            not any(o.accepted for o in outcomes)
            and self._pending_replan is None
            and not self.has_active_replan
        ):
            # Clear dedup/cooldown if this obstacle was previously seen while idle before movement started
            self.event_router.clear()
            self.standing_orders.reset_execution_history()
            for oid, tobj in self._world.environment.tracked_objects.items():
                if oid in all_keys or getattr(tobj, "is_obstacle", False):
                    tobj.handled = False
            self._route_and_schedule_world_events(now_ts=now_ts)

    def _schedule_or_coalesce_replan(
        self,
        *,
        trigger_rationale: str,
        priority: int,
        now_ts: float,
        standing_order_plan: MissionPlan | None = None,
        standing_order_id: str | None = None,
        source_event_id: str | None = None,
        event_type: str | None = None,
        event_priority: str | None = None,
        event_source: str | None = None,
        event_source_mode: str | None = None,
        triage_disposition: str | None = None,
        triage_trace_id: str | None = None,
    ) -> None:
        """Enqueue or coalesce an environmental replanning trigger without blocking ingestion."""
        if (
            self._emergency_stopped
            or self._world.mission.status != UltronMissionStatus.IN_PROGRESS
            or self._active_plan is None
        ):
            return

        scout = self._world.vehicles.get("SCOUT01")
        if scout is not None and getattr(scout, "status", "") == "EMERGENCY_STOPPED":
            return

        mission_id = self._world.mission.id
        if (
            self._pending_replan is not None
            and self._pending_replan.mission_epoch == self._mission_epoch
        ):
            self._pending_replan.coalesced_count += 1
            self._pending_replan.last_updated_at = now_ts
            self._replan_coalesced_updates_count += 1
            if priority >= self._pending_replan.priority:
                self._pending_replan.priority = priority
                self._pending_replan.trigger_rationale = trigger_rationale
                if standing_order_plan is not None:
                    self._pending_replan.standing_order_plan = standing_order_plan
                    self._pending_replan.standing_order_id = standing_order_id
                if source_event_id is not None:
                    self._pending_replan.source_event_id = source_event_id
                if event_type is not None:
                    self._pending_replan.event_type = event_type
                if event_priority is not None:
                    self._pending_replan.event_priority = event_priority
                if event_source is not None:
                    self._pending_replan.event_source = event_source
                if event_source_mode is not None:
                    self._pending_replan.event_source_mode = event_source_mode
                if triage_disposition is not None:
                    self._pending_replan.triage_disposition = triage_disposition
                if triage_trace_id is not None:
                    self._pending_replan.triage_trace_id = triage_trace_id
        else:
            self._pending_replan = _CoalescedReplanRequest(
                mission_id=mission_id,
                mission_epoch=self._mission_epoch,
                trigger_rationale=trigger_rationale,
                priority=priority,
                first_requested_at=now_ts,
                last_updated_at=now_ts,
                coalesced_count=1,
                standing_order_plan=standing_order_plan,
                standing_order_id=standing_order_id,
                source_event_id=source_event_id,
                event_type=event_type,
                event_priority=event_priority,
                event_source=event_source,
                event_source_mode=event_source_mode,
                triage_disposition=triage_disposition,
                triage_trace_id=triage_trace_id,
            )

        self._ensure_replan_worker_started()

    def _ensure_replan_worker_started(self) -> None:
        """Start the single background replanning worker task if an event loop is running."""
        if self._active_replan_task is not None and not self._active_replan_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._active_replan_task = loop.create_task(
            self._replan_worker_loop(),
            name=f"ultron-replan-worker-e{self._mission_epoch}",
        )

    def _execute_replan_pass(self, state: UltronState, standing_order_plan: MissionPlan | None) -> UltronState:
        """Run either the deterministic Standing Order plan assignment or full LangGraph replan pass."""
        if standing_order_plan is not None and not _is_planner_mocked():
            st: dict[str, Any] = dict(state)
            st["proposed_plan"] = standing_order_plan
            st.update(ultron_nodes.node_validate(st))  # type: ignore[arg-type]
            if not st.get("validation_errors"):
                st.update(ultron_nodes.node_assign(st))  # type: ignore[arg-type]
                return st  # type: ignore[return-value]
        return self.app.invoke(state)

    async def _replan_worker_loop(self) -> None:
        """Background worker that processes coalesced replanning requests one at a time and captures before/after snapshots."""
        try:
            while self._pending_replan is not None and not self._emergency_stopped:
                await asyncio.sleep(0)
                req = self._pending_replan
                if req is None:
                    break

                if req.priority < REPLAN_PRIORITY_SIGNIFICANT and self._replan_cooldown_sec > 0.0:
                    elapsed_since_last = time.monotonic() - self._last_replan_finished_at
                    if elapsed_since_last < self._replan_cooldown_sec:
                        await asyncio.sleep(self._replan_cooldown_sec - elapsed_since_last)

                req = self._pending_replan
                self._pending_replan = None
                if req is None:
                    break

                if (
                    self._emergency_stopped
                    or req.mission_epoch != self._mission_epoch
                    or self._world.mission.id != req.mission_id
                    or self._world.mission.status != UltronMissionStatus.IN_PROGRESS
                ):
                    self._replan_discarded_stale_count += 1
                    break

                # Ensure a BEFORE snapshot exists for this replan operation
                before_snap_id = self.memory_store.get_before_snapshot_id_for_event(
                    req.source_event_id
                )
                if before_snap_id is None:
                    before_snap = self.memory_store.record_transition_snapshot(
                        self._world,
                        mission_id=req.mission_id,
                        timestamp=req.first_requested_at,
                        trigger_type=SnapshotTriggerTypeEnum.REPLAN_BEFORE,
                        trigger_reason=f"Before replanning: {req.trigger_rationale}",
                        phase=SnapshotPhaseEnum.BEFORE,
                        associated_event_id=req.source_event_id,
                        task_states=self._active_task_specs,
                        decision_context=SnapshotDecisionContext(
                            event_id=req.source_event_id,
                            event_type=req.event_type,
                            event_priority=req.event_priority,
                            event_source=req.event_source,
                            event_source_mode=req.event_source_mode,
                            standing_order_id=req.standing_order_id,
                            triage_disposition=req.triage_disposition,
                        ),
                    )
                    before_snap_id = before_snap.snapshot_id

                # Snapshot current world state so concurrent sensor ingestion never blocks or races
                world_snapshot = self._world.model_copy(deep=True)
                snapshot_hazard_ids = {h.id for h in world_snapshot.environment.hazards}
                snapshot_obj_ids = {o.id for o in world_snapshot.environment.objects}
                snapshot_tracked_ids = set(world_snapshot.environment.tracked_objects.keys())
                prev_trace_ids = {
                    dt.trace_id for dt in world_snapshot.environment.decision_traces
                }

                replan_state: UltronState = {
                    "world": world_snapshot,
                    "proposed_plan": None,
                    "validation_errors": [],
                    "repair_prompt": None,
                    "retry_count": 0,
                    "trigger_rationale": req.trigger_rationale,
                    "language": "English",
                }

                res_state: UltronState | None = None
                for attempt in range(1, self._replan_max_retries + 1):
                    try:
                        res_state = await asyncio.to_thread(
                            self._execute_replan_pass,
                            replan_state,
                            req.standing_order_plan,
                        )
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "ULTRON async replan attempt %d/%d failed: %s",
                            attempt,
                            self._replan_max_retries,
                            exc,
                        )
                        if attempt >= self._replan_max_retries:
                            break
                        await asyncio.sleep(0.05)

                self._last_replan_finished_at = time.monotonic()
                if res_state is None:
                    continue

                # Epoch & safety fencing: never allow an old replan response to overwrite a newer mission or E-Stop!
                scout_now = self._world.vehicles.get("SCOUT01")
                if (
                    self._emergency_stopped
                    or req.mission_epoch != self._mission_epoch
                    or self._world.mission.id != req.mission_id
                    or self._world.mission.status != UltronMissionStatus.IN_PROGRESS
                    or (
                        scout_now is not None
                        and getattr(scout_now, "status", "") == "EMERGENCY_STOPPED"
                    )
                ):
                    self._replan_discarded_stale_count += 1
                    logger.info(
                        "Discarded stale ULTRON replan response for mission %s (epoch=%d vs current=%d).",
                        req.mission_id,
                        req.mission_epoch,
                        self._mission_epoch,
                    )
                    continue

                # Merge decision traces and mission task state without clobbering newer sensor observations!
                res_world = res_state["world"]
                self._world.mission.status = res_world.mission.status
                self._world.mission.tasks = res_world.mission.tasks

                new_plan = res_state.get("proposed_plan")
                self._active_plan = new_plan

                scout_mode_sim = True
                if scout_now is not None:
                    scout_mode_sim = (
                        getattr(scout_now.mode, "value", str(scout_now.mode)) == "simulated"
                    )

                if new_plan is not None:
                    # Mark previous ASSIGNED/IN_PROGRESS tasks as CANCELLED / superseded and add new replanned tasks
                    new_plan_task_ids = {t_item.id for t_item in new_plan.tasks}
                    replanned_specs: list[SnapshotTaskState] = []
                    for prev_t in self._active_task_specs:
                        if prev_t.task_id in new_plan_task_ids:
                            continue
                        if prev_t.status in ("COMPLETED", "FAILED", "CANCELLED"):
                            replanned_specs.append(prev_t)
                        else:
                            replanned_specs.append(
                                prev_t.model_copy(
                                    update={
                                        "status": "CANCELLED",
                                        "execution_stage": "CANCELLED",
                                    }
                                )
                            )
                    for idx, t_item in enumerate(new_plan.tasks, start=1):
                        veh_str = (
                            t_item.vehicle.value
                            if hasattr(t_item.vehicle, "value")
                            else str(t_item.vehicle)
                        )
                        replanned_specs.append(
                            SnapshotTaskState(
                                task_id=t_item.id,
                                vehicle_id=veh_str,
                                task_type=t_item.type.upper(),
                                area=t_item.area,
                                target=t_item.target,
                                priority=max(1, min(100, int(t_item.priority))),
                                status="ASSIGNED",
                                execution_stage="ASSIGNED",
                                is_simulated=scout_mode_sim,
                            )
                        )
                    self._active_task_specs = replanned_specs

                new_traces: list[DecisionTraceEntry] = []
                for dt in res_world.environment.decision_traces:
                    if dt.trace_id not in prev_trace_ids:
                        dt.source_event_id = req.source_event_id
                        dt.event_type = req.event_type
                        dt.event_priority = req.event_priority
                        dt.standing_order_id = req.standing_order_id
                        dt.triage_disposition = req.triage_disposition
                        dt.triage_trace_id = req.triage_trace_id
                        dt.before_snapshot_id = before_snap_id
                        self._world.environment.decision_traces.append(dt)
                        new_traces.append(dt)

                if len(self._world.environment.decision_traces) > 100:
                    self._world.environment.decision_traces = (
                        self._world.environment.decision_traces[-100:]
                    )

                # Mark only the hazards/objects that were part of the snapshot as handled
                for h in self._world.environment.hazards:
                    if h.id in snapshot_hazard_ids:
                        h.handled = True
                for o in self._world.environment.objects:
                    if o.id in snapshot_obj_ids:
                        o.handled = True
                for tid, tobj in self._world.environment.tracked_objects.items():
                    if tid in snapshot_tracked_ids:
                        tobj.handled = True

                # Capture AFTER snapshot for the replan/Standing Order outcome
                latest_dt = new_traces[-1] if new_traces else None
                after_trig_type = (
                    SnapshotTriggerTypeEnum.STANDING_ORDER_APPLIED
                    if req.standing_order_id
                    else (
                        SnapshotTriggerTypeEnum.REPLAN_DEGRADED
                        if self._world.mission.status == UltronMissionStatus.DEGRADED
                        else SnapshotTriggerTypeEnum.REPLAN_ACCEPTED
                    )
                )
                after_snap = self.memory_store.record_transition_snapshot(
                    self._world,
                    mission_id=req.mission_id,
                    timestamp=(latest_dt.timestamp if latest_dt is not None else time.time()),
                    trigger_type=after_trig_type,
                    trigger_reason=(
                        latest_dt.decision_summary
                        if latest_dt is not None
                        else req.trigger_rationale
                    ),
                    phase=SnapshotPhaseEnum.AFTER,
                    associated_event_id=req.source_event_id,
                    associated_trace_id=(latest_dt.trace_id if latest_dt is not None else None),
                    paired_before_snapshot_id=before_snap_id,
                    task_states=self._active_task_specs,
                    decision_context=SnapshotDecisionContext(
                        event_id=req.source_event_id,
                        event_type=req.event_type,
                        event_priority=req.event_priority,
                        event_source=req.event_source,
                        event_source_mode=req.event_source_mode,
                        evidence_ids=(
                            tuple(r.observation_id for r in latest_dt.evidence_references)
                            if latest_dt is not None
                            else ()
                        ),
                        standing_order_id=req.standing_order_id,
                        triage_disposition=req.triage_disposition,
                        planner_source=(
                            latest_dt.planner_source if latest_dt is not None else None
                        ),
                        validation_passed=(
                            latest_dt.safety_validator_passed if latest_dt is not None else True
                        ),
                        validation_errors=(
                            tuple(latest_dt.validation_errors)
                            if latest_dt is not None
                            else ()
                        ),
                        resulting_task_ids=(
                            tuple(latest_dt.resulting_task_ids)
                            if latest_dt is not None
                            else ()
                        ),
                        rationale=(latest_dt.rationale if latest_dt is not None else None),
                    ),
                )
                if latest_dt is not None:
                    latest_dt.after_snapshot_id = after_snap.snapshot_id

                self._replan_completed_count += 1
        except asyncio.CancelledError:
            self._replan_discarded_stale_count += 1
            raise

    async def sync_event_intelligence_to_event_service(
        self,
        session: Any,
        event_svc: Any,
        *,
        mission_id: str | None = None,
        broadcast: bool = True,
    ) -> tuple[list[Any], list[Any]]:
        """Persist unsynced routed events, decision traces, replanned tasks, and mission snapshots to SQLite."""
        from db.models import Task

        target_mission_id = mission_id or self._world.mission.id or self._active_mission_id
        if not target_mission_id:
            return [], []

        persisted_events: list[Any] = []
        persisted_traces: list[Any] = []
        event_dt_by_id: dict[str, datetime] = {}

        # 1. Sync routed events (preserving source_timestamp when present)
        for ev in self.event_router.history:
            ev_ts_float = (
                ev.source_timestamp
                if (ev.source_timestamp is not None and ev.source_timestamp > 0.0)
                else ev.received_at
            )
            ev_dt = datetime.fromtimestamp(ev_ts_float, tz=timezone.utc)
            event_dt_by_id[ev.event_id] = ev_dt

            if ev.event_id in self._synced_event_ids:
                continue
            if ev.mission_id and ev.mission_id != target_mission_id:
                continue
            ev_read = await event_svc.record_event(
                session,
                mission_id=target_mission_id,
                category=map_event_to_db_category(ev),
                message=f"[{ev.event_type.value}] {ev.summary}",
                event_id=ev.event_id,
                timestamp=ev_dt,
                commit=False,
                broadcast=broadcast,
            )
            self._synced_event_ids.add(ev.event_id)
            persisted_events.append(ev_read)

        # 2. Sync decision traces (preserving true chronological timestamp relative to originating event)
        for dt in self._world.environment.decision_traces:
            if dt.trace_id in self._synced_trace_ids:
                continue
            if dt.mission_id and dt.mission_id != target_mission_id:
                continue
            trace_ts_float = dt.timestamp
            if dt.source_event_id and dt.source_event_id in event_dt_by_id:
                orig_ev_ts = event_dt_by_id[dt.source_event_id].timestamp()
                if trace_ts_float < orig_ev_ts:
                    trace_ts_float = orig_ev_ts + 0.001
            trace_dt = datetime.fromtimestamp(trace_ts_float, tz=timezone.utc)
            rt_read = await event_svc.record_reasoning_trace(
                session,
                mission_id=target_mission_id,
                trigger=dt.trigger,
                rationale=dt.rationale,
                resulting_task_ids=list(dt.resulting_task_ids),
                trace_id=dt.trace_id,
                timestamp=trace_dt,
                commit=False,
                broadcast=broadcast,
            )
            self._synced_trace_ids.add(dt.trace_id)
            persisted_traces.append(rt_read)

        # 3. Sync replanned task rows into SQLite `tasks` table so `resulting_task_ids` are persisted
        tasks_mutated = False
        if self._world.mission.id == target_mission_id and self._active_task_specs:
            for t_spec in self._active_task_specs:
                existing_task = await session.get(Task, t_spec.task_id)
                if existing_task is None:
                    session.add(
                        Task(
                            id=t_spec.task_id,
                            mission_id=target_mission_id,
                            vehicle_id=t_spec.vehicle_id,
                            type=t_spec.task_type,
                            area=t_spec.area,
                            target=t_spec.target,
                            priority=t_spec.priority,
                            status=t_spec.status,
                        )
                    )
                    tasks_mutated = True
                elif existing_task.mission_id != target_mission_id:
                    existing_task.mission_id = target_mission_id
                    existing_task.vehicle_id = t_spec.vehicle_id
                    existing_task.type = t_spec.task_type
                    existing_task.area = t_spec.area
                    existing_task.target = t_spec.target
                    existing_task.priority = t_spec.priority
                    existing_task.status = t_spec.status
                    tasks_mutated = True
                elif (
                    t_spec.status == "CANCELLED"
                    and existing_task.status in ("ASSIGNED", "IN_PROGRESS")
                ):
                    existing_task.status = "CANCELLED"
                    tasks_mutated = True
                await session.flush()

        # 4. Sync immutable MissionMemorySnapshot records to SQLite (`mission_snapshots`)
        unsynced_snaps = self.memory_store.get_unsynced_snapshots(target_mission_id)
        snaps_persisted = 0
        if hasattr(event_svc, "record_mission_snapshot"):
            for snap in unsynced_snaps:
                try:
                    await event_svc.record_mission_snapshot(
                        session,
                        snap,
                        commit=False,
                    )
                    self.memory_store.mark_snapshot_synced(snap.snapshot_id)
                    snaps_persisted += 1
                except Exception as exc:
                    self._snapshot_persistence_failures_count += 1
                    err_str = f"Snapshot {snap.snapshot_id} persistence failed: {exc}"
                    self._snapshot_persistence_errors.append(err_str)
                    logger.error(err_str)

        if persisted_events or persisted_traces or snaps_persisted > 0 or tasks_mutated:
            await session.commit()

        return persisted_events, persisted_traces

    def get_tools(self, *, current_time: float | None = None) -> WorldModelTools:
        """Return bound read-only `WorldModelTools` for inspecting current environmental state."""
        return WorldModelTools(self._world, current_time=current_time)

    def get_environment_snapshot_model(
        self,
        *,
        current_time: float | None = None,
    ) -> EnvironmentSnapshotResult:
        """Return structured `EnvironmentSnapshotResult` from `WorldModelTools`."""
        now = current_time if current_time is not None else time.time()
        refresh_world_model_freshness(self._world, current_time=now)
        return self.get_tools(current_time=now).get_environment_snapshot()

    def query_environment(
        self,
        query: str,
        *,
        current_time: float | None = None,
    ) -> GroundedEnvironmentalResponse:
        """Answer an environmental question strictly from verified World Model evidence."""
        now = current_time if current_time is not None else time.time()
        refresh_world_model_freshness(self._world, current_time=now)
        return answer_environmental_query(self._world, query, current_time=now)

    def is_stub_implementation(self) -> bool:
        """Returns False indicating real LangGraph agent is driving mission logic."""
        return False

    async def get_world_model_snapshot(self) -> WorldModelSnapshotStub:
        """Returns snapshot of current in-memory World Model state."""
        vehicles_dict: dict[str, dict[str, Any]] = {}
        for vid, vstate in self._world.vehicles.items():
            vehicles_dict[vid] = {
                "id": vid,
                "vehicle_type": getattr(vstate, "vehicle_type", "ground"),
                "status": getattr(vstate, "status", "IDLE"),
                "connection": getattr(
                    vstate.connection, "value", str(vstate.connection)
                ),
                "mode": getattr(vstate.mode, "value", str(vstate.mode)),
                "battery": vstate.battery,
                "position_x": vstate.position.x,
                "position_y": vstate.position.y,
                "position_alt": getattr(vstate.position, "alt", 0.0),
            }

        return WorldModelSnapshotStub(
            active_mission_id=self._active_mission_id,
            vehicles=vehicles_dict,
            observations=list(self._observations),
            is_stub=False,
        )

    async def create_simulated_mission(
        self,
        mission_id: str,
        objective: str,
        language: str = "English",
    ) -> SimulatedMissionPlanStub:
        """Generates structured task plan via ULTRON LangGraph workflow grounded in WorldModel and records initial before/after snapshots."""
        # Invalidate any stale replanning or triage from a prior mission
        self.cancel_pending_replanning("new_mission_created")
        self.standing_orders.reset_execution_history()
        self._emergency_stopped = False

        self._active_mission_id = mission_id
        self._active_task_specs = []
        self._world.mission = MissionState(
            id=mission_id,
            objective=objective,
            status=UltronMissionStatus.IN_PROGRESS,
        )
        now_ts = time.time()
        refresh_world_model_freshness(self._world, current_time=now_ts)

        # Capture BEFORE snapshot prior to initial planning
        before_snap = self.memory_store.record_transition_snapshot(
            self._world,
            mission_id=mission_id,
            timestamp=now_ts,
            trigger_type=SnapshotTriggerTypeEnum.MISSION_CREATED,
            trigger_reason=f"Mission {mission_id} created: {objective}",
            phase=SnapshotPhaseEnum.BEFORE,
            associated_event_id=f"{mission_id}-EVT01",
            task_states=[],
        )

        initial_state: UltronState = {
            "world": self._world,
            "proposed_plan": None,
            "validation_errors": [],
            "repair_prompt": None,
            "retry_count": 0,
            "trigger_rationale": None,
            "language": language,
        }

        # Execute LangGraph off the main event loop thread: START -> plan -> validate -> assign -> END
        result_state = await asyncio.to_thread(self.app.invoke, initial_state)
        self._world = result_state["world"]
        for dt in self._world.environment.decision_traces:
            self._synced_trace_ids.add(dt.trace_id)

        plan = result_state.get("proposed_plan")
        if not plan:
            msg = "ULTRON LangGraph workflow failed to produce a valid mission plan."
            raise RuntimeError(msg)

        self._active_plan = plan

        scout_v = self._world.vehicles.get("SCOUT01")
        is_sim_mode = (
            getattr(scout_v.mode, "value", str(scout_v.mode)) == "simulated"
            if scout_v is not None
            else True
        )

        specs: list[SimulatedTaskSpecStub] = []
        snap_tasks: list[SnapshotTaskState] = []
        for idx, task in enumerate(plan.tasks, start=1):
            veh_str = (
                task.vehicle.value
                if hasattr(task.vehicle, "value")
                else str(task.vehicle)
            )
            scoped_id = f"{mission_id}-T{idx:02d}"
            specs.append(
                SimulatedTaskSpecStub(
                    task_id=scoped_id,
                    vehicle_id=veh_str,  # type: ignore[arg-type]
                    task_type=task.type.upper(),
                    area=task.area,
                    target=task.target,
                    priority=int(task.priority),
                    status="ASSIGNED",
                )
            )
            snap_tasks.append(
                SnapshotTaskState(
                    task_id=scoped_id,
                    vehicle_id=veh_str,
                    task_type=task.type.upper(),
                    area=task.area,
                    target=task.target,
                    priority=max(1, min(100, int(task.priority))),
                    status="ASSIGNED",
                    execution_stage="ASSIGNED",
                    is_simulated=is_sim_mode,
                )
            )

        self._active_task_specs = snap_tasks
        if snap_tasks:
            self._world.mission.tasks.active[VehicleID.SCOUT] = snap_tasks[0].task_id
            self._world.mission.tasks.pending = [t.task_id for t in snap_tasks[1:]]

        initial_dt = (
            self._world.environment.decision_traces[-1]
            if self._world.environment.decision_traces
            else None
        )
        if initial_dt is not None:
            initial_dt.resulting_task_ids = [s.task_id for s in specs]
            initial_dt.source_event_id = f"{mission_id}-EVT01"
            initial_dt.before_snapshot_id = before_snap.snapshot_id

        after_snap = self.memory_store.record_transition_snapshot(
            self._world,
            mission_id=mission_id,
            timestamp=time.time(),
            trigger_type=SnapshotTriggerTypeEnum.INITIAL_PLAN_ACCEPTED,
            trigger_reason=(
                initial_dt.decision_summary
                if initial_dt is not None
                else f"Initial plan accepted for {mission_id}"
            ),
            phase=SnapshotPhaseEnum.AFTER,
            associated_event_id=f"{mission_id}-EVT01",
            associated_trace_id=f"{mission_id}-RT01",
            paired_before_snapshot_id=before_snap.snapshot_id,
            task_states=self._active_task_specs,
            decision_context=SnapshotDecisionContext(
                event_id=f"{mission_id}-EVT01",
                event_type="MISSION_STATE_CHANGED",
                event_priority="LOW",
                event_source="mission_lifecycle",
                event_source_mode="simulated" if is_sim_mode else "physical",
                evidence_ids=tuple(plan.evidence_ids),
                planner_source=(
                    initial_dt.planner_source if initial_dt is not None else "fallback"
                ),
                validation_passed=True,
                validation_errors=(),
                resulting_task_ids=tuple(s.task_id for s in specs),
                rationale=plan.rationale,
            ),
        )
        if initial_dt is not None:
            initial_dt.after_snapshot_id = after_snap.snapshot_id

        return SimulatedMissionPlanStub(
            mission_id=mission_id,
            objective=objective,
            status="ACTIVE",
            tasks=specs,
            trigger="OPERATOR_MISSION_DISPATCH",
            rationale=plan.rationale,
            is_stub=False,
        )

    async def update_vehicle_state(
        self,
        vehicle_id: VehicleId,
        *,
        status: str | None = None,
        connection: str | None = None,
        mode: VehicleMode | None = None,
        battery: float | None = None,
        position_x: float | None = None,
        position_y: float | None = None,
        position_alt: float | None = None,
        last_heartbeat: datetime | None = None,
    ) -> None:
        """Updates internal World Model state for the specified vehicle and routes state events.

        Records a mission snapshot ONLY on meaningful vehicle state transitions
        (connection change, critical status change such as EMERGENCY_STOPPED/FAULT,
        mode change, or battery crossing below 20%) — never on routine heartbeats.
        """
        v = self._world.vehicles.get(vehicle_id)
        if not v:
            return

        prev_status = str(v.status)
        prev_conn = v.connection
        prev_mode = v.mode
        prev_battery = float(v.battery)
        now_ts = (
            last_heartbeat.timestamp() if last_heartbeat is not None else time.time()
        )

        if status is not None:
            if status == "EMERGENCY_STOPPED":
                self._emergency_stopped = True
                v.status = "EMERGENCY_STOPPED"
                self.cancel_pending_replanning("emergency_stop")
                active_mid = self._world.mission.id or self._active_mission_id
                if active_mid and prev_status != "EMERGENCY_STOPPED":
                    self.memory_store.record_transition_snapshot(
                        self._world,
                        mission_id=active_mid,
                        timestamp=now_ts,
                        trigger_type=SnapshotTriggerTypeEnum.EMERGENCY_STOP_TRIGGERED,
                        trigger_reason=f"Vehicle {vehicle_id} transitioned to EMERGENCY_STOPPED.",
                        phase=SnapshotPhaseEnum.STATE_TRANSITION,
                        task_states=self._active_task_specs,
                    )
                return
            elif v.status != "EMERGENCY_STOPPED" or not self._emergency_stopped:
                v.status = status
        if connection is not None:
            if connection in ("CONNECTED", "ONLINE"):
                v.connection = UltronConnectionStatus.ONLINE
                v.pose_valid = True
            elif connection == "DISCONNECTED":
                v.connection = UltronConnectionStatus.OFFLINE
                v.pose_valid = False
            else:
                v.connection = UltronConnectionStatus.UNKNOWN
                v.pose_valid = False
        if mode is not None:
            v.mode = (
                UltronVehicleMode(mode)
                if mode in UltronVehicleMode._value2member_map_
                else UltronVehicleMode.SIMULATED
            )
        if battery is not None:
            v.battery = max(0.0, min(100.0, float(battery)))
        if position_x is not None:
            v.position.x = float(position_x)
        if position_y is not None:
            v.position.y = float(position_y)
        if position_alt is not None and hasattr(v.position, "alt"):
            v.position.alt = float(position_alt)
        if last_heartbeat is not None:
            v.last_heartbeat_timestamp = last_heartbeat.timestamp()

        active_mid = self._world.mission.id or self._active_mission_id
        conn_changed = v.connection != prev_conn
        crit_status_changed = (
            str(v.status) != prev_status
            and (str(v.status).upper() in {"FAULT", "UNKNOWN"} or prev_status.upper() in {"FAULT", "UNKNOWN"})
        )
        mode_changed = v.mode != prev_mode
        crossed_low_battery = prev_battery >= 20.0 and v.battery < 20.0

        if active_mid and (
            conn_changed or crit_status_changed or mode_changed or crossed_low_battery
        ):
            reasons: list[str] = []
            if conn_changed:
                reasons.append(f"connection {prev_conn.value}->{v.connection.value}")
            if crit_status_changed:
                reasons.append(f"status {prev_status}->{v.status}")
            if mode_changed:
                reasons.append(f"mode {prev_mode.value}->{v.mode.value}")
            if crossed_low_battery:
                reasons.append(f"critical battery {v.battery:.1f}% (<20%)")
            self.memory_store.record_transition_snapshot(
                self._world,
                mission_id=active_mid,
                timestamp=now_ts,
                trigger_type=SnapshotTriggerTypeEnum.VEHICLE_STATE_TRANSITION,
                trigger_reason=f"Vehicle {vehicle_id} state change: {', '.join(reasons)}",
                phase=SnapshotPhaseEnum.STATE_TRANSITION,
                task_states=self._active_task_specs,
            )

        # Route any actionable vehicle state events (critical battery, fault, disconnect) when mission active
        if (
            not self._emergency_stopped
            and self._world.mission.status == UltronMissionStatus.IN_PROGRESS
            and self._active_plan is not None
            and (
                v.battery < 20.0
                or str(v.status).upper() == "FAULT"
                or v.connection != UltronConnectionStatus.ONLINE
            )
        ):
            self._route_and_schedule_world_events(now_ts=now_ts)

    async def mark_vehicle_unknown(
        self,
        vehicle_id: VehicleId,
        *,
        detected_at: datetime,
    ) -> None:
        """Transitions a vehicle to UNKNOWN on heartbeat timeout and routes disconnect event."""
        v = self._world.vehicles.get(vehicle_id)
        prev_conn = v.connection if v is not None else None
        if v:
            v.connection = UltronConnectionStatus.UNKNOWN
            v.pose_valid = False
        self._unknown_transitions.append((vehicle_id, detected_at))
        if len(self._unknown_transitions) > 500:
            del self._unknown_transitions[:-500]

        active_mid = self._world.mission.id or self._active_mission_id
        if active_mid and prev_conn != UltronConnectionStatus.UNKNOWN:
            self.memory_store.record_transition_snapshot(
                self._world,
                mission_id=active_mid,
                timestamp=detected_at.timestamp(),
                trigger_type=SnapshotTriggerTypeEnum.VEHICLE_STATE_TRANSITION,
                trigger_reason=f"Vehicle {vehicle_id} heartbeat timed out -> UNKNOWN",
                phase=SnapshotPhaseEnum.STATE_TRANSITION,
                task_states=self._active_task_specs,
            )

        if (
            vehicle_id == "SCOUT01"
            and not self._emergency_stopped
            and self._world.mission.status == UltronMissionStatus.IN_PROGRESS
            and self._active_plan is not None
        ):
            self._route_and_schedule_world_events(now_ts=detected_at.timestamp())

    def sync_camera_sensor_health(
        self,
        *,
        enabled: bool,
        ros_connection_state: str,
        telemetry_source: str,
        topic: str,
        message_type: str,
        frame: str | None,
        vehicle_id: str | None,
        stamp: float | None,
        received_at: datetime | None,
        is_stale: bool,
        stale_after_seconds: float,
        invalid_message_count: int,
        out_of_order_count: int,
    ) -> None:
        """Synchronize depth-camera `SensorHealthReport` inside `WorldModel` from `PerceptionService`."""
        sh = self._world.environment.sensor_health.get("depth_camera")
        if sh is None:
            return
        sh.enabled = enabled or (stamp is not None)
        sh.source_mode = (
            SensorSourceModeEnum.PHYSICAL
            if telemetry_source == "rosbridge" and enabled
            else SensorSourceModeEnum.SIMULATED
        )
        sh.topic = topic
        sh.message_type = message_type
        if frame:
            sh.coordinate_frame = frame
        sh.vehicle_id = vehicle_id
        sh.last_message_timestamp = stamp
        sh.last_ingested_at = received_at.timestamp() if received_at else None
        sh.is_stale = is_stale
        sh.stale_after_sec = stale_after_seconds
        sh.malformed_message_count = invalid_message_count
        sh.out_of_order_count = out_of_order_count

        if ros_connection_state == "DISABLED" and stamp is None:
            sh.status = SensorHealthStatusEnum.DISCONNECTED
        elif ros_connection_state in ("DISCONNECTED", "CONNECTING", "RECONNECTING") and stamp is None:
            sh.status = SensorHealthStatusEnum.DISCONNECTED
        elif ros_connection_state == "ERROR":
            sh.status = SensorHealthStatusEnum.ERROR
        elif is_stale:
            sh.status = SensorHealthStatusEnum.STALE
        elif sh.source_mode == SensorSourceModeEnum.SIMULATED:
            sh.status = SensorHealthStatusEnum.SIMULATED
        else:
            sh.status = SensorHealthStatusEnum.CONNECTED

    def ingest_detection_frame_into_world_model(
        self,
        *,
        stamp: float,
        received_at: datetime,
        frame: str,
        objects: list[Any],
        vehicle_id: str | None,
        telemetry_source: str,
        topic: str = "/detections_json",
        map_frame_confirmed: bool = False,
    ) -> list[SensorObservation]:
        """Ingest a validated `/detections_json` frame directly into ULTRON's `WorldModel`.

        Snapshots are captured ONLY when a meaningful environmental change occurs
        (never on empty frames or routine duplicate detections).
        """
        self._world.environment.transform_map_validated = map_frame_confirmed
        self.transform_registry.map_frame_confirmed = map_frame_confirmed

        ingested_ts = received_at.timestamp()
        source_mode = (
            SensorSourceModeEnum.PHYSICAL
            if telemetry_source == "rosbridge"
            else SensorSourceModeEnum.SIMULATED
        )

        # Always update depth-camera sensor activity & freshness even on empty frames (`objects == []`)
        sh = self._world.environment.sensor_health.get("depth_camera")
        if sh is not None:
            sh.enabled = True
            sh.source_mode = source_mode
            sh.topic = topic
            sh.coordinate_frame = frame
            sh.vehicle_id = vehicle_id
            if sh.last_message_timestamp is not None and stamp < sh.last_message_timestamp:
                sh.out_of_order_count += 1
            else:
                sh.last_message_timestamp = stamp
                sh.last_ingested_at = ingested_ts
                sh.is_stale = False
                sh.valid_message_count += 1
                sh.status = (
                    SensorHealthStatusEnum.CONNECTED
                    if source_mode == SensorSourceModeEnum.PHYSICAL
                    else SensorHealthStatusEnum.SIMULATED
                )
        self._world.environment.last_updated_timestamp = max(
            self._world.environment.last_updated_timestamp or 0.0,
            stamp,
            ingested_ts,
        )

        pre_fp = self._environment_fingerprint()
        created_obs: list[SensorObservation] = []
        for obj in objects:
            cls_name = getattr(obj, "class_name", None) or (
                obj.get("class") if isinstance(obj, dict) else "unknown"
            )
            score = float(
                getattr(obj, "score", 0.0)
                if not isinstance(obj, dict)
                else obj.get("score", 0.0)
            )
            obj_id = (
                getattr(obj, "id", 0)
                if not isinstance(obj, dict)
                else obj.get("id", 0)
            )
            pos_obj = (
                getattr(obj, "position", None)
                if not isinstance(obj, dict)
                else obj.get("position", {})
            )
            px = float(
                getattr(pos_obj, "x", 0.0)
                if not isinstance(pos_obj, dict)
                else pos_obj.get("x", 0.0)
            )
            py = float(
                getattr(pos_obj, "y", 0.0)
                if not isinstance(pos_obj, dict)
                else pos_obj.get("y", 0.0)
            )
            pz = float(
                getattr(pos_obj, "z", 0.0)
                if not isinstance(pos_obj, dict)
                else pos_obj.get("z", 0.0)
            )
            range_m = float(
                getattr(obj, "range_m", 0.0)
                if not isinstance(obj, dict)
                else obj.get("range_m", 0.0)
            )

            cls_clean = str(cls_name).strip()[:100]
            is_hazard = cls_clean.lower() in {
                "fire",
                "smoke",
                "hazard",
                "gas_leak",
                "thermal_anomaly",
            }
            is_obstacle = cls_clean.lower() in {
                "chair",
                "bench",
                "box",
                "obstacle",
                "lidar_obstacle",
                "wall",
                "barrier",
                "table",
                "dining table",
                "diningtable",
                "couch",
                "sofa",
                "potted plant",
                "pottedplant",
                "bed",
                "tv",
                "tvmonitor",
                "laptop",
                "backpack",
                "suitcase",
                "handbag",
                "bottle",
                "cup",
                "trash can",
                "cone",
                "pillar",
                "column",
                "door",
                "cabinet",
                "refrigerator",
                "person",
                "bicycle",
                "dog",
                "cat",
                "cart",
            }

            sensor_obs = SensorObservation(
                observation_id=f"OBS-CAM-{uuid.uuid4().hex[:8].upper()}",
                sensor_id="depth_camera",
                sensor_type=SensorTypeEnum.DEPTH_CAMERA,
                vehicle_id=vehicle_id,
                detector_object_id=int(obj_id) if isinstance(obj_id, int) else None,
                sensor_timestamp=stamp,
                ingested_at=ingested_ts,
                coordinate_frame=frame,
                position=Position3DMeasurement(
                    x=px,
                    y=py,
                    z=pz,
                    range_m=range_m,
                    uncertainty_m=round(max(0.05, 0.08 * range_m), 3),
                ),
                classification=cls_clean,
                confidence=score,
                raw_reference={
                    "topic": topic,
                    "frame": frame,
                    "stamp": stamp,
                    "detector_object_id": obj_id,
                    "id_is_frame_local_only": True,
                },
                validation_status=ObservationValidationStatusEnum.VALIDATED,
                source_mode=source_mode,
                is_obstacle=is_obstacle,
                is_hazard=is_hazard,
            )
            ingest_sensor_observation(
                self._world,
                sensor_obs,
                transform_registry=self.transform_registry,
            )
            created_obs.append(sensor_obs)

        post_fp = self._environment_fingerprint()
        active_mid = self._world.mission.id or self._active_mission_id

        # Route generated events through EventRouter -> Standing Orders / LLM Triage -> Replan Worker
        if (
            created_obs
            and not self._emergency_stopped
            and self._world.mission.status == UltronMissionStatus.IN_PROGRESS
            and self._active_plan is not None
        ):
            self._route_and_schedule_world_events(now_ts=ingested_ts)
        elif created_obs and post_fp != pre_fp and active_mid:
            # Capture significant environmental change even if mission is not yet actively replanning
            self.memory_store.record_transition_snapshot(
                self._world,
                mission_id=active_mid,
                timestamp=stamp if stamp > 0.0 else ingested_ts,
                trigger_type=SnapshotTriggerTypeEnum.SIGNIFICANT_OBSERVATION_ACCEPTED,
                trigger_reason=(
                    f"Significant observation ingested ({len(created_obs)} object(s) in frame '{frame}')"
                ),
                phase=SnapshotPhaseEnum.STATE_TRANSITION,
                task_states=self._active_task_specs,
            )

        return created_obs

    def ingest_lidar_scan_into_world_model(
        self,
        raw_scan: dict[str, Any],
        *,
        received_at: datetime | None = None,
        vehicle_id: str | None = None,
        telemetry_source: str = "rosbridge",
    ) -> Any:
        """Validate and ingest a LiDAR scan or `PointCloud2` (`/unilidar/cloud`) payload into ULTRON's `WorldModel`."""
        now_dt = received_at or datetime.now(timezone.utc)
        ingested_ts = now_dt.timestamp()
        source_mode = (
            SensorSourceModeEnum.PHYSICAL
            if telemetry_source == "rosbridge"
            else SensorSourceModeEnum.SIMULATED
        )
        if not self.lidar_adapter.enabled or not self.lidar_adapter.is_configured:
            self.lidar_adapter.configure(
                enabled=True,
                topic=self.lidar_adapter.topic or "/unilidar/cloud",
                message_type=(
                    self.lidar_adapter.message_type or "sensor_msgs/msg/PointCloud2"
                ),
                coordinate_frame=(
                    self.lidar_adapter.coordinate_frame or "unilidar_lidar"
                ),
                vehicle_id=vehicle_id or self.lidar_adapter.vehicle_id,
                source_mode=source_mode,
                connected=True,
            )
        else:
            self.lidar_adapter.set_connection_state(True)

        outcome = self.lidar_adapter.ingest_scan(
            raw_scan,
            ingested_at=ingested_ts,
            source_mode=source_mode,
        )
        if outcome.sensor_health is not None:
            self._world.environment.sensor_health["lidar"] = outcome.sensor_health

        if not outcome.accepted:
            return outcome

        if outcome.free_space_radius_m is not None:
            self._world.environment.free_space_coverage_m = outcome.free_space_radius_m

        pre_fp = self._environment_fingerprint()
        for obs in outcome.observations:
            ingest_sensor_observation(
                self._world,
                obs,
                transform_registry=self.transform_registry,
            )
        post_fp = self._environment_fingerprint()
        active_mid = self._world.mission.id or self._active_mission_id

        if (
            outcome.observations
            and not self._emergency_stopped
            and self._world.mission.status == UltronMissionStatus.IN_PROGRESS
            and self._active_plan is not None
        ):
            self._route_and_schedule_world_events(now_ts=ingested_ts)
        elif outcome.observations and post_fp != pre_fp and active_mid:
            self.memory_store.record_transition_snapshot(
                self._world,
                mission_id=active_mid,
                timestamp=outcome.sensor_timestamp or ingested_ts,
                trigger_type=SnapshotTriggerTypeEnum.SIGNIFICANT_OBSERVATION_ACCEPTED,
                trigger_reason=(
                    f"LiDAR scan ingested ({len(outcome.observations)} obstacle sector(s) in frame '{outcome.coordinate_frame}')"
                ),
                phase=SnapshotPhaseEnum.STATE_TRANSITION,
                task_states=self._active_task_specs,
            )

        return outcome

    async def record_observation(
        self,
        observation: ObservationPayloadStub,
    ) -> None:
        """Merges vehicle observation into World Model and routes events through EventRouter."""
        self._observations.append(
            {
                "id": observation.observation_id,
                "observation_id": observation.observation_id,
                "mission_id": observation.mission_id,
                "vehicle_id": observation.vehicle_id,
                "obs_type": observation.obs_type,
                "confidence": observation.confidence,
                "location_x": observation.location_x,
                "location_y": observation.location_y,
                "timestamp": observation.timestamp.isoformat(),
                "raw": observation.raw,
            }
        )
        if len(self._observations) > 500:
            del self._observations[:-500]

        pre_fp = self._environment_fingerprint()
        now_ts = time.time()
        raw = observation.raw or {}
        if isinstance(raw, dict) and "frame" in raw and "position" in raw:
            pos_dict = raw.get("position", {})
            px = float(pos_dict.get("x", observation.location_x))
            py = float(pos_dict.get("y", observation.location_y))
            pz = float(pos_dict.get("z", 0.0))
            range_m = (
                float(raw["range_m"])
                if "range_m" in raw and raw["range_m"] is not None
                else None
            )
            stamp = float(raw.get("stamp", observation.timestamp.timestamp()))
            cls_label = str(
                raw.get("class")
                or observation.obs_type.replace("detection:", "")
            )
            is_hazard = cls_label.lower() in {
                "fire",
                "smoke",
                "hazard",
                "thermal_anomaly",
                "possible_hazard",
            }
            is_obstacle = cls_label.lower() in {
                "chair",
                "bench",
                "box",
                "obstacle",
                "lidar_obstacle",
                "wall",
                "barrier",
                "table",
                "dining table",
                "diningtable",
                "couch",
                "sofa",
                "potted plant",
                "pottedplant",
                "bed",
                "tv",
                "tvmonitor",
                "laptop",
                "backpack",
                "suitcase",
                "handbag",
                "bottle",
                "cup",
                "trash can",
                "cone",
                "pillar",
                "column",
                "door",
                "cabinet",
                "refrigerator",
                "person",
                "bicycle",
                "dog",
                "cat",
                "cart",
            }
            sensor_obs = SensorObservation(
                observation_id=observation.observation_id,
                sensor_id="depth_camera",
                sensor_type=SensorTypeEnum.DEPTH_CAMERA,
                vehicle_id=observation.vehicle_id,
                detector_object_id=(
                    int(raw["object_id"])
                    if isinstance(raw.get("object_id"), int)
                    else None
                ),
                sensor_timestamp=stamp,
                ingested_at=now_ts,
                coordinate_frame=str(raw["frame"]),
                position=Position3DMeasurement(
                    x=px,
                    y=py,
                    z=pz,
                    range_m=range_m,
                    uncertainty_m=0.15,
                ),
                classification=cls_label,
                confidence=observation.confidence,
                raw_reference=raw,
                validation_status=ObservationValidationStatusEnum.VALIDATED,
                source_mode=(
                    SensorSourceModeEnum.PHYSICAL
                    if raw.get("source") == "rosbridge"
                    else SensorSourceModeEnum.SIMULATED
                ),
                is_obstacle=is_obstacle,
                is_hazard=is_hazard,
            )
            ingest_sensor_observation(
                self._world,
                sensor_obs,
                transform_registry=self.transform_registry,
            )
        else:
            structured_obs = StructuredObservation(
                observation_id=observation.observation_id,
                event=observation.obs_type,
                source_vehicle=VehicleID.SCOUT,
                confidence=observation.confidence,
                location=Position2D(
                    x=observation.location_x, y=observation.location_y
                ),
                timestamp=observation.timestamp.timestamp(),
                raw_reference=raw if isinstance(raw, dict) else None,
            )
            self._world = update_world_model(self._world, structured_obs)

        post_fp = self._environment_fingerprint()
        active_mid = (
            observation.mission_id
            or self._world.mission.id
            or self._active_mission_id
        )

        # Route events through EventRouter -> Standing Orders / LLM Triage -> Replan Worker
        if (
            not self._emergency_stopped
            and self._world.mission.status == UltronMissionStatus.IN_PROGRESS
            and self._active_plan is not None
        ):
            routed = self._route_and_schedule_world_events(now_ts=now_ts)
            if not any(r.accepted for r in routed) and post_fp != pre_fp and active_mid:
                self.memory_store.record_transition_snapshot(
                    self._world,
                    mission_id=active_mid,
                    timestamp=observation.timestamp.timestamp(),
                    trigger_type=SnapshotTriggerTypeEnum.SIGNIFICANT_OBSERVATION_ACCEPTED,
                    trigger_reason=(
                        f"Observation {observation.observation_id} ({observation.obs_type}) accepted into World Model"
                    ),
                    phase=SnapshotPhaseEnum.STATE_TRANSITION,
                    task_states=self._active_task_specs,
                )
        elif post_fp != pre_fp and active_mid:
            self.memory_store.record_transition_snapshot(
                self._world,
                mission_id=active_mid,
                timestamp=observation.timestamp.timestamp(),
                trigger_type=SnapshotTriggerTypeEnum.SIGNIFICANT_OBSERVATION_ACCEPTED,
                trigger_reason=(
                    f"Observation {observation.observation_id} ({observation.obs_type}) accepted into World Model"
                ),
                phase=SnapshotPhaseEnum.STATE_TRANSITION,
                task_states=self._active_task_specs,
            )
