"""Minimal development stub implementation of UltronAgentPort.

Maintains an in-memory World Model representation of SCOUT01, HAWK01, and
recorded observations, and generates deterministic stub mission decompositions
without implementing real ULTRON planning/replanning logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ultron_stub.interface import (
    ObservationPayloadStub,
    SimulatedMissionPlanStub,
    SimulatedTaskSpecStub,
    UltronAgentPort,
    VehicleId,
    VehicleMode,
    WorldModelSnapshotStub,
)


class StubUltronAgent(UltronAgentPort):
    """Minimal in-process stub for the ULTRON agent module."""

    def __init__(self) -> None:
        self._active_mission_id: str | None = None
        self._vehicles: dict[str, dict[str, Any]] = {
            "SCOUT01": {
                "id": "SCOUT01",
                "vehicle_type": "ground",
                "status": "IDLE",
                "connection": "DISCONNECTED",
                "mode": "simulated",
                "battery": 100.0,
                "position_x": 0.0,
                "position_y": 0.0,
                "position_alt": 0.0,
                "last_heartbeat": None,
            },
            "HAWK01": {
                "id": "HAWK01",
                "vehicle_type": "aerial",
                "status": "IDLE",
                "connection": "DISCONNECTED",
                "mode": "simulated",
                "battery": 100.0,
                "position_x": 0.0,
                "position_y": 0.0,
                "position_alt": 0.0,
                "last_heartbeat": None,
            },
        }
        self._observations: list[dict[str, Any]] = []
        self._unknown_transitions: list[tuple[VehicleId, datetime]] = []

    @property
    def unknown_transitions(self) -> list[tuple[VehicleId, datetime]]:
        """Recorded (vehicle_id, timestamp) pairs when marked UNKNOWN (§5.1)."""
        return list(self._unknown_transitions)

    def is_stub_implementation(self) -> bool:
        return True

    async def get_world_model_snapshot(self) -> WorldModelSnapshotStub:
        return WorldModelSnapshotStub(
            active_mission_id=self._active_mission_id,
            vehicles={k: dict(v) for k, v in self._vehicles.items()},
            observations=[dict(obs) for obs in self._observations],
            is_stub=True,
        )

    async def create_simulated_mission(
        self,
        mission_id: str,
        objective: str,
    ) -> SimulatedMissionPlanStub:
        """Return a deterministic stub task assignment for SCOUT01 and HAWK01."""
        self._active_mission_id = mission_id
        scout_task = SimulatedTaskSpecStub(
            task_id=f"{mission_id}-T01",
            vehicle_id="SCOUT01",
            task_type="SIMULATED_GROUND_RECON",
            area="SECTOR_A",
            target=objective[:120],
            priority=1,
            status="ASSIGNED",
        )
        hawk_task = SimulatedTaskSpecStub(
            task_id=f"{mission_id}-T02",
            vehicle_id="HAWK01",
            task_type="SIMULATED_AERIAL_OVERWATCH",
            area="SECTOR_B",
            target=objective[:120],
            priority=2,
            status="ASSIGNED",
        )
        return SimulatedMissionPlanStub(
            mission_id=mission_id,
            objective=objective,
            status="ACTIVE",
            tasks=[scout_task, hawk_task],
            trigger=f"Operator submitted mission {mission_id}",
            rationale=(
                "[ULTRON_STUB] Deterministic simulated decomposition assigning "
                "SCOUT01 ground recon and HAWK01 aerial overwatch."
            ),
            is_stub=True,
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
        entry = self._vehicles.setdefault(
            vehicle_id,
            {
                "id": vehicle_id,
                "vehicle_type": "ground" if vehicle_id == "SCOUT01" else "aerial",
                "status": "IDLE",
                "connection": "DISCONNECTED",
                "mode": "simulated",
                "battery": 100.0,
                "position_x": 0.0,
                "position_y": 0.0,
                "position_alt": 0.0,
                "last_heartbeat": None,
            },
        )
        if status is not None:
            entry["status"] = status
        if connection is not None:
            entry["connection"] = connection
        if mode is not None:
            entry["mode"] = mode
        if battery is not None:
            entry["battery"] = battery
        if position_x is not None:
            entry["position_x"] = position_x
        if position_y is not None:
            entry["position_y"] = position_y
        if position_alt is not None:
            entry["position_alt"] = position_alt
        if last_heartbeat is not None:
            entry["last_heartbeat"] = last_heartbeat

    async def mark_vehicle_unknown(
        self,
        vehicle_id: VehicleId,
        *,
        detected_at: datetime,
    ) -> None:
        entry = self._vehicles.get(vehicle_id)
        if entry is not None:
            entry["connection"] = "UNKNOWN"
            entry["status"] = "UNKNOWN"
        self._unknown_transitions.append((vehicle_id, detected_at))

    async def record_observation(
        self,
        observation: ObservationPayloadStub,
    ) -> None:
        self._observations.append(
            {
                "id": observation.observation_id,
                "mission_id": observation.mission_id,
                "vehicle_id": observation.vehicle_id,
                "obs_type": observation.obs_type,
                "confidence": observation.confidence,
                "location_x": observation.location_x,
                "location_y": observation.location_y,
                "timestamp": observation.timestamp,
                "raw": observation.raw,
            }
        )
