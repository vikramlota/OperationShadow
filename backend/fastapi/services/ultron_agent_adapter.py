"""ULTRON LangGraph Agent Adapter for FastAPI backend (§2, §3, §4).

Bridges FastAPI's `MissionService` and `VehicleService` to the in-process
ULTRON Agent LangGraph state machine (`ultron.graph.workflow`), Pydantic
`WorldModel`, deterministic Safety Validator, and Gemini planning engine.
"""

from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

# Ensure project root is available for imports
_project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from ultron.graph.workflow import build_ultron_graph
from ultron.schemas.world import (
    WorldModel,
    MissionState,
    ScoutState,
    HawkState,
    VehicleMode as UltronVehicleMode,
    ConnectionStatus as UltronConnectionStatus,
)
from ultron.schemas.mission import MissionStatus as UltronMissionStatus, VehicleID
from ultron.schemas.commands import Position2D, Position3D
from ultron.schemas.observations import StructuredObservation
from ultron.evaluator.updater import update_world_model
from ultron.state import UltronState

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
    """In-process ULTRON Agent adapter driven by LangGraph state machine."""

    def __init__(self) -> None:
        self.app = build_ultron_graph()
        self._active_mission_id: str | None = None
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
                "HAWK01": HawkState(
                    mode=UltronVehicleMode.SIMULATED,
                    position=Position3D(x=0.0, y=0.0, alt=0.0),
                    battery=100.0,
                    connection=UltronConnectionStatus.ONLINE,
                ),
            },
        )
        self._observations: list[dict[str, Any]] = []
        self._unknown_transitions: list[tuple[VehicleId, datetime]] = []

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
                "connection": getattr(vstate.connection, "value", str(vstate.connection)),
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
        """Generates structured task plan via ULTRON LangGraph workflow."""
        self._active_mission_id = mission_id
        self._world.mission = MissionState(
            id=mission_id,
            objective=objective,
            status=UltronMissionStatus.IN_PROGRESS,
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

        # Execute LangGraph: START -> plan -> validate -> assign -> END
        result_state = self.app.invoke(initial_state)

        plan = result_state.get("proposed_plan")
        if not plan:
            msg = "ULTRON LangGraph workflow failed to produce a valid mission plan."
            raise RuntimeError(msg)

        specs: list[SimulatedTaskSpecStub] = []
        for idx, task in enumerate(plan.tasks, start=1):
            veh_str = task.vehicle.value if hasattr(task.vehicle, "value") else str(task.vehicle)
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
            v.connection = UltronConnectionStatus(connection) if connection in UltronConnectionStatus._value2member_map_ else UltronConnectionStatus.ONLINE
        if mode is not None:
            v.mode = UltronVehicleMode(mode) if mode in UltronVehicleMode._value2member_map_ else UltronVehicleMode.SIMULATED
        if battery is not None:
            v.battery = max(0.0, min(100.0, float(battery)))
        if position_x is not None:
            v.position.x = float(position_x)
        if position_y is not None:
            v.position.y = float(position_y)
        if position_alt is not None and hasattr(v.position, "alt"):
            v.position.alt = float(position_alt)

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
        self._unknown_transitions.append((vehicle_id, detected_at))

    async def record_observation(
        self,
        observation: ObservationPayloadStub,
    ) -> None:
        """Merges vehicle observation into World Model and runs replanning if needed."""
        self._observations.append({
            "observation_id": observation.observation_id,
            "mission_id": observation.mission_id,
            "vehicle_id": observation.vehicle_id,
            "obs_type": observation.obs_type,
            "confidence": observation.confidence,
            "location_x": observation.location_x,
            "location_y": observation.location_y,
            "timestamp": observation.timestamp.isoformat(),
        })

        # Inject into World Model
        structured_obs = StructuredObservation(
            event=observation.obs_type,
            source_vehicle=VehicleID.HAWK if observation.vehicle_id == "HAWK01" else VehicleID.SCOUT,
            confidence=observation.confidence,
            location=Position2D(x=observation.location_x, y=observation.location_y),
            timestamp=observation.timestamp.timestamp(),
        )
        self._world = update_world_model(self._world, structured_obs)
