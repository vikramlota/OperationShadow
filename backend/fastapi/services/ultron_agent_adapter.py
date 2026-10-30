"""ULTRON LangGraph Agent Adapter for FastAPI backend (§2, §3, §4, §6, §7, §8).

Bridges FastAPI's `MissionService`, `VehicleService`, and `PerceptionService` to the
in-process ULTRON Agent LangGraph state machine (`ultron.graph.workflow`),
evidence-grounded `WorldModel`, `WorldModelTools`, multi-sensor fusion pipeline,
deterministic Safety Validator, and Gemini planning engine.
"""

from __future__ import annotations

from datetime import datetime, timezone
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


class UltronLangGraphAgent(UltronAgentPort):
    """In-process ULTRON Agent adapter driven by LangGraph state machine and evidence-grounded WorldModel."""

    def __init__(self) -> None:
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

    @property
    def world(self) -> WorldModel:
        return self._world

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

        # Execute LangGraph: START -> plan -> validate -> assign -> END
        result_state = self.app.invoke(initial_state)
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
        """
        self._world.environment.transform_map_validated = map_frame_confirmed
        self.transform_registry.map_frame_confirmed = map_frame_confirmed

        ingested_ts = received_at.timestamp()
        source_mode = (
            SensorSourceModeEnum.PHYSICAL
            if telemetry_source == "rosbridge"
            else SensorSourceModeEnum.SIMULATED
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

        return created_obs

    async def record_observation(
        self,
        observation: ObservationPayloadStub,
    ) -> None:
        """Merges vehicle observation into World Model and runs replanning if needed."""
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
                ingested_at=time.time(),
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

        # Evaluate environment-driven replanning if a mission is active
        if self._world.mission.status == UltronMissionStatus.IN_PROGRESS and self._active_plan is not None:
            should_replan, trigger_rationale = check_replanning_triggers(
                self._world, debounce_sec=0.5
            )
            if should_replan and trigger_rationale:
                replan_state: UltronState = {
                    "world": self._world,
                    "proposed_plan": self._active_plan,
                    "validation_errors": [],
                    "repair_prompt": None,
                    "retry_count": 0,
                    "trigger_rationale": trigger_rationale,
                    "language": "English",
                }
                res_state = self.app.invoke(replan_state)
                self._world = res_state["world"]
                if res_state.get("proposed_plan") is not None:
                    self._active_plan = res_state["proposed_plan"]
