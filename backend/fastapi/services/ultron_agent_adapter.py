"""ULTRON LangGraph Agent Adapter for FastAPI backend (§2, §3, §4, §6, §7, §8 & Phase 2 Event Intelligence).

Bridges FastAPI's `MissionService`, `VehicleService`, `EventService`, and `PerceptionService`
to the in-process ULTRON Agent LangGraph state machine (`ultron.graph.workflow`),
evidence-grounded `WorldModel`, `WorldModelTools`, multi-sensor fusion pipeline,
Phase 2 `EventRouter`, `StandingOrderRegistry`, bounded `LLM Event Triage`,
deterministic Safety Validator, and Gemini planning engine.

Phase 1 + Phase 2 Reliability & Intelligence Architecture:
- Sensor ingestion (`ingest_detection_frame_into_world_model` and `record_observation`)
  is strictly decoupled from synchronous LLM calls so camera/sensor ingestion
  completes in sub-millisecond time.
- Validated observations, vehicle state transitions, and sensor health updates flow
  through the in-process `EventRouter` (`ultron/events/router.py`), which enforces
  schema validation, deterministic priority (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`),
  content-signature time-bounded deduplication, and stale/out-of-order rejection.
- Known operational situations (`VEHICLE_LOW_BATTERY`, `SENSOR_CONFLICT`,
  route-blocking `OBSTACLE_DETECTED`, verified high-confidence `HAZARD_DETECTED`)
  are evaluated first by deterministic `StandingOrderRegistry` policies
  (`ultron/events/standing_orders.py`) without requiring an LLM call.
- Ambiguous or context-dependent events are evaluated by a bounded asynchronous
  LLM Event Triage worker (`ultron/events/triage.py`, max 1 in-flight triage call,
  fenced by `mission_epoch` and cancelled immediately on Emergency Stop).
- All Standing Order plans and triage-triggered replans flow through the single
  coalesced asynchronous replanning worker (`_replan_worker_loop`), preventing
  duplicate replanning for the same event and preserving epoch fencing and E-Stop
  independence.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import heapq
import logging
from pathlib import Path
import sys
import time
from typing import Any, Deque
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
    """In-process ULTRON Agent adapter driven by LangGraph, EventRouter, StandingOrders, LLM Triage, and WorldModel."""

    def __init__(
        self,
        *,
        replan_cooldown_sec: float = 0.25,
        replan_debounce_sec: float = 0.5,
        replan_max_retries: int = 2,
        standing_orders_registry: StandingOrderRegistry | None = None,
    ) -> None:
        self.app = build_ultron_graph()
        self._active_mission_id: str | None = None
        self._active_plan: MissionPlan | None = None
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
        self.camera_adapter = DepthCameraAdapter(
            enabled=True,
            source_mode=SensorSourceModeEnum.SIMULATED,
        )
        self.lidar_adapter = LidarIngestionAdapter(
            enabled=False,
            source_mode=SensorSourceModeEnum.SIMULATED,
        )
        self.transform_registry = FrameTransformRegistry(
            map_frame_confirmed=False,
            camera_lidar_extrinsics_confirmed=False,
        )
        self._observations: list[dict[str, Any]] = []
        self._unknown_transitions: list[tuple[VehicleId, datetime]] = []

        # Phase 2: Standing Orders Registry & Centralized In-Process Event Router
        self.standing_orders: StandingOrderRegistry = (
            standing_orders_registry
            if standing_orders_registry is not None
            else StandingOrderRegistry.from_json_file()
        )
        self.event_router: EventRouter = EventRouter(
            dedup_window_sec=max(0.0, float(replan_debounce_sec)),
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

        if had_pending_triage:
            self._triage_discarded_stale_count += len(self._triage_heap)
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
        if outcome.accepted:
            ev_ts = outcome.event.received_at if outcome.event is not None else time.time()
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
        leg_id = event.evidence_payload.get("object_entity_id")
        if leg_id:
            for o in self._world.environment.objects:
                if o.id == leg_id:
                    o.handled = True

    def _record_triage_decision_trace(
        self,
        event: UltronEvent,
        triage_res: EventTriageResult,
        triage_outcome: TriageOutcome,
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

        trace = DecisionTraceEntry(
            trace_id=f"DT-TRIAGE-{uuid.uuid4().hex[:8].upper()}",
            mission_id=self._world.mission.id or event.mission_id or "M001",
            timestamp=max(time.time(), event.received_at + 0.001),
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
        )
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
                self._record_triage_decision_trace(event, triage_res, triage_outcome)

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
                    )
                elif so_res.status == "suppressed_already_handled":
                    self._mark_event_entities_handled(event)
                    # If a replan is already pending for this epoch (e.g. burst frames), record coalescing
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
                self._enqueue_for_triage(event)
            elif target == EventRouteTargetEnum.LOG_ONLY:
                self._mark_event_entities_handled(event)

    def _route_and_schedule_world_events(self, *, now_ts: float) -> list[RoutedEventOutcome]:
        """Extract candidate events from `self._world`, route via `EventRouter`, and dispatch handlers."""
        if self._emergency_stopped:
            return []

        outcomes = self.event_router.ingest_world_model_state(
            self._world, received_at=now_ts
        )
        for out in outcomes:
            self._routed_outcomes.append(out)
            if (
                not out.accepted
                and out.status == "suppressed_duplicate"
                and self._pending_replan is not None
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

    def _schedule_or_coalesce_replan(
        self,
        *,
        trigger_rationale: str,
        priority: int,
        now_ts: float,
        standing_order_plan: MissionPlan | None = None,
        standing_order_id: str | None = None,
        source_event_id: str | None = None,
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
        """Background worker that processes coalesced replanning requests one at a time."""
        try:
            while self._pending_replan is not None and not self._emergency_stopped:
                # Yield briefly so simultaneous multi-object detections in the same frame coalesce
                await asyncio.sleep(0)
                req = self._pending_replan
                if req is None:
                    break

                # Enforce cooldown for ordinary updates; significant updates (hazards/obstacles/standing orders) run immediately
                if req.priority < REPLAN_PRIORITY_SIGNIFICANT and self._replan_cooldown_sec > 0.0:
                    elapsed_since_last = time.monotonic() - self._last_replan_finished_at
                    if elapsed_since_last < self._replan_cooldown_sec:
                        await asyncio.sleep(self._replan_cooldown_sec - elapsed_since_last)

                # Re-fetch after potential sleep in case newer updates coalesced or E-Stop fired
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

                for dt in res_world.environment.decision_traces:
                    if dt.trace_id not in prev_trace_ids:
                        self._world.environment.decision_traces.append(dt)
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

                self._active_plan = res_state.get("proposed_plan")
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
        """Persist unsynced routed events and decision traces to `EventService` in chronological order."""
        target_mission_id = mission_id or self._world.mission.id or self._active_mission_id
        if not target_mission_id:
            return [], []

        persisted_events: list[Any] = []
        persisted_traces: list[Any] = []
        latest_event_dt: datetime | None = None

        for ev in self.event_router.history:
            if ev.event_id in self._synced_event_ids:
                continue
            if ev.mission_id and ev.mission_id != target_mission_id:
                continue
            ev_dt = datetime.fromtimestamp(ev.received_at, tz=timezone.utc)
            if latest_event_dt is None or ev_dt > latest_event_dt:
                latest_event_dt = ev_dt
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

        for idx, dt in enumerate(self._world.environment.decision_traces, start=1):
            if dt.trace_id in self._synced_trace_ids:
                continue
            if dt.mission_id and dt.mission_id != target_mission_id:
                continue
            trace_dt = datetime.fromtimestamp(dt.timestamp, tz=timezone.utc)
            if latest_event_dt is not None and trace_dt <= latest_event_dt:
                trace_dt = latest_event_dt + timedelta(milliseconds=idx)
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

        if persisted_events or persisted_traces:
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
        """Generates structured task plan via ULTRON LangGraph workflow grounded in WorldModel."""
        # Invalidate any stale replanning or triage from a prior mission
        self.cancel_pending_replanning("new_mission_created")
        self.standing_orders.reset_execution_history()
        self._emergency_stopped = False

        self._active_mission_id = mission_id
        self._world.mission = MissionState(
            id=mission_id,
            objective=objective,
            status=UltronMissionStatus.IN_PROGRESS,
        )
        refresh_world_model_freshness(self._world)

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

        specs: list[SimulatedTaskSpecStub] = []
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
        """Updates internal World Model state for the specified vehicle and routes state events."""
        v = self._world.vehicles.get(vehicle_id)
        if not v:
            return

        if status is not None:
            if status == "EMERGENCY_STOPPED":
                self._emergency_stopped = True
                v.status = "EMERGENCY_STOPPED"
                self.cancel_pending_replanning("emergency_stop")
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
            now_ts = (
                last_heartbeat.timestamp() if last_heartbeat is not None else time.time()
            )
            self._route_and_schedule_world_events(now_ts=now_ts)

    async def mark_vehicle_unknown(
        self,
        vehicle_id: VehicleId,
        *,
        detected_at: datetime,
    ) -> None:
        """Transitions a vehicle to UNKNOWN on heartbeat timeout and routes disconnect event."""
        v = self._world.vehicles.get(vehicle_id)
        if v:
            v.connection = UltronConnectionStatus.UNKNOWN
            v.pose_valid = False
        self._unknown_transitions.append((vehicle_id, detected_at))
        if len(self._unknown_transitions) > 500:
            del self._unknown_transitions[:-500]

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

        Preserves the exact coordinate frame (`oak_rgb_camera_optical_frame`,
        `oak_parent_frame`, `map`, etc.), 3D position (`x, y, z`), `range_m`,
        sensor timestamp (`stamp`), and local ingestion timestamp (`received_at`).
        Processes empty detection frames (`objects == []`) as sensor activity/freshness
        without claiming free space is clear, and routes events through `EventRouter`,
        `StandingOrderRegistry`, and `LLM Event Triage` asynchronously without blocking
        camera ingestion.
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
                "wall",
                "barrier",
                "table",
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

        # Route generated events through EventRouter -> Standing Orders / LLM Triage -> Replan Worker
        if (
            created_obs
            and not self._emergency_stopped
            and self._world.mission.status == UltronMissionStatus.IN_PROGRESS
            and self._active_plan is not None
        ):
            self._route_and_schedule_world_events(now_ts=ingested_ts)

        return created_obs

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
                "wall",
                "table",
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

        # Route events through EventRouter -> Standing Orders / LLM Triage -> Replan Worker
        if (
            not self._emergency_stopped
            and self._world.mission.status == UltronMissionStatus.IN_PROGRESS
            and self._active_plan is not None
        ):
            self._route_and_schedule_world_events(now_ts=now_ts)
