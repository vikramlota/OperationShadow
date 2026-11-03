"""ULTRON LangGraph Agent Adapter for FastAPI backend (§2, §3, §4, §6, §7, §8).

Bridges FastAPI's `MissionService`, `VehicleService`, and `PerceptionService` to the
in-process ULTRON Agent LangGraph state machine (`ultron.graph.workflow`),
evidence-grounded `WorldModel`, `WorldModelTools`, multi-sensor fusion pipeline,
deterministic Safety Validator, and Gemini planning engine.

Second-Pass Reliability Architecture:
- Sensor ingestion (`ingest_detection_frame_into_world_model` and `record_observation`)
  is strictly decoupled from synchronous LLM replanning so camera/sensor ingestion
  completes in sub-millisecond time even when Gemini takes several seconds.
- An asynchronous replanning coordinator coalesces repeated environmental updates,
  prioritizes significant changes (new hazards, new obstacles, vehicle faults) over
  ordinary repeated detections, enforces a single active replan per mission with
  configurable cooldowns and bounded retries, fences mission epochs so stale LLM
  responses can never overwrite newer mission or world state, and immediately cancels
  pending replanning upon Emergency Stop or mission cancellation/completion.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
import time
from typing import Any
import uuid

# Ensure project root is available for imports
_project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from ultron.graph.workflow import build_ultron_graph
from ultron.planner.grounding import (
    answer_environmental_query,
    validate_grounded_response,
)
from ultron.replanner.triggers import check_replanning_triggers
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import MissionPlan, MissionStatus as UltronMissionStatus, VehicleID
from ultron.schemas.observations import (
    ObservationValidationStatusEnum,
    Position3DMeasurement,
    SensorObservation,
    SensorSourceModeEnum,
    SensorTypeEnum,
    StructuredObservation,
)
from ultron.schemas.world import (
    ConnectionStatus as UltronConnectionStatus,
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


class UltronLangGraphAgent(UltronAgentPort):
    """In-process ULTRON Agent adapter driven by LangGraph state machine and evidence-grounded WorldModel."""

    def __init__(
        self,
        *,
        replan_cooldown_sec: float = 0.25,
        replan_debounce_sec: float = 0.5,
        replan_max_retries: int = 2,
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
        return self._active_replan_task is not None and not self._active_replan_task.done()

    @property
    def replan_completed_count(self) -> int:
        return self._replan_completed_count

    @property
    def replan_discarded_stale_count(self) -> int:
        return self._replan_discarded_stale_count

    @property
    def replan_coalesced_updates_count(self) -> int:
        return self._replan_coalesced_updates_count

    def cancel_pending_replanning(self, reason: str = "cancelled") -> None:
        """Invalidate pending replanning work and cancel any in-flight replanning task."""
        had_pending = self._pending_replan is not None
        had_active = (
            self._active_replan_task is not None and not self._active_replan_task.done()
        )
        self._mission_epoch += 1
        self._pending_replan = None
        if had_active and self._active_replan_task is not None:
            self._active_replan_task.cancel()
            logger.info("Cancelled in-flight ULTRON replanning task (%s).", reason)
        if had_pending or had_active:
            self._replan_discarded_stale_count += 1
        self._active_replan_task = None

    async def wait_for_replanning(self, timeout: float = 5.0) -> None:
        """Await any pending or active background replanning work (used in tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while True:
            task = self._active_replan_task
            if task is None or task.done():
                if self._pending_replan is None:
                    return
                # Spawn worker if pending request exists and loop is running
                self._ensure_replan_worker_started()
                task = self._active_replan_task
                if task is None:
                    return
            remaining = max(0.01, deadline - time.monotonic())
            with suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            if time.monotonic() >= deadline:
                return

    def _classify_replan_priority(self, trigger_rationale: str) -> int:
        """Assign higher priority to hazards, obstacles, and vehicle offline/unknown faults."""
        lower = trigger_rationale.lower()
        if any(
            kw in lower
            for kw in ("hazard", "obstacle", "offline", "unknown", "conflict", "fire", "smoke")
        ):
            return REPLAN_PRIORITY_SIGNIFICANT
        return REPLAN_PRIORITY_ORDINARY

    def _schedule_or_coalesce_replan(
        self,
        *,
        trigger_rationale: str,
        priority: int,
        now_ts: float,
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
        if self._pending_replan is not None and self._pending_replan.mission_epoch == self._mission_epoch:
            self._pending_replan.coalesced_count += 1
            self._pending_replan.last_updated_at = now_ts
            self._replan_coalesced_updates_count += 1
            if priority >= self._pending_replan.priority:
                self._pending_replan.priority = priority
                self._pending_replan.trigger_rationale = trigger_rationale
        else:
            self._pending_replan = _CoalescedReplanRequest(
                mission_id=mission_id,
                mission_epoch=self._mission_epoch,
                trigger_rationale=trigger_rationale,
                priority=priority,
                first_requested_at=now_ts,
                last_updated_at=now_ts,
                coalesced_count=1,
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

    async def _replan_worker_loop(self) -> None:
        """Background worker that processes coalesced replanning requests one at a time."""
        try:
            while self._pending_replan is not None and not self._emergency_stopped:
                # Yield briefly so simultaneous multi-object detections in the same frame coalesce
                await asyncio.sleep(0)
                req = self._pending_replan
                if req is None:
                    break

                # Enforce cooldown for ordinary updates; significant updates (hazards/obstacles) run immediately
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
                        res_state = await asyncio.to_thread(self.app.invoke, replan_state)
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
        # Invalidate any stale replanning from a prior mission
        self.cancel_pending_replanning("new_mission_created")
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
        """Updates internal World Model state for the specified vehicle."""
        v = self._world.vehicles.get(vehicle_id)
        if not v:
            return

        if status is not None:
            if status == "EMERGENCY_STOPPED":
                self._emergency_stopped = True
                v.status = "EMERGENCY_STOPPED"
                self.cancel_pending_replanning("emergency_stop")
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

    async def mark_vehicle_unknown(
        self,
        vehicle_id: VehicleId,
        *,
        detected_at: datetime,
    ) -> None:
        """Transitions a vehicle to UNKNOWN on heartbeat timeout."""
        v = self._world.vehicles.get(vehicle_id)
        if v:
            v.connection = UltronConnectionStatus.UNKNOWN
            v.pose_valid = False
        self._unknown_transitions.append((vehicle_id, detected_at))
        if len(self._unknown_transitions) > 500:
            del self._unknown_transitions[:-500]

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
        without claiming free space is clear, and schedules replanning asynchronously
        without blocking camera ingestion.
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

        # Evaluate environment-driven replanning triggers and schedule asynchronously (never block ingestion!)
        if (
            created_obs
            and not self._emergency_stopped
            and self._world.mission.status == UltronMissionStatus.IN_PROGRESS
            and self._active_plan is not None
        ):
            should_replan, trigger_rationale = check_replanning_triggers(
                self._world,
                debounce_sec=self._replan_debounce_sec,
                current_time=ingested_ts,
            )
            if should_replan and trigger_rationale:
                priority = self._classify_replan_priority(trigger_rationale)
                self._schedule_or_coalesce_replan(
                    trigger_rationale=trigger_rationale,
                    priority=priority,
                    now_ts=ingested_ts,
                )

        return created_obs

    async def record_observation(
        self,
        observation: ObservationPayloadStub,
    ) -> None:
        """Merges vehicle observation into World Model and schedules asynchronous replanning if needed."""
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

        # Schedule asynchronous replanning if a mission is active
        if (
            not self._emergency_stopped
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
