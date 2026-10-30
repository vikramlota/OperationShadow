"""Isolated ULTRON agent stub and deterministic vehicle simulation package.

Provides explicit protocol interfaces, a locked real-hardware adapter guard,
and a deterministic mock vehicle simulator until the ULTRON Agent Architecture,
TRD v2.0, and Hardware & IoT specifications are provided.
"""

from ultron_stub.interface import (
    CommandAcknowledgementStub,
    CommandAckStatus,
    EmergencyStopDispatchResultStub,
    EmergencyStopTarget,
    ObservationPayloadStub,
    SimulatedMissionPlanStub,
    SimulatedTaskSpecStub,
    TelemetryPayloadStub,
    UltronAgentPort,
    VehicleCommandEnvelopeStub,
    VehicleId,
    VehicleMode,
    VehicleType,
    WorldModelSnapshotStub,
)
from ultron_stub.simulator import (
    FaultInjectionConfig,
    MockFleetSimulator,
    MockVehicleAdapter,
    MockVehicleConfig,
)
from ultron_stub.stub_agent import StubUltronAgent
from ultron_stub.vehicle_adapter import (
    ClockPort,
    LockedRealVehicleAdapter,
    ManualClock,
    RealHardwareLockedError,
    SystemClock,
    VehicleAdapterPort,
    VehicleAdapterState,
)

__all__ = [
    "ClockPort",
    "CommandAckStatus",
    "CommandAcknowledgementStub",
    "EmergencyStopDispatchResultStub",
    "EmergencyStopTarget",
    "FaultInjectionConfig",
    "LockedRealVehicleAdapter",
    "ManualClock",
    "MockFleetSimulator",
    "MockVehicleAdapter",
    "MockVehicleConfig",
    "ObservationPayloadStub",
    "RealHardwareLockedError",
    "SimulatedMissionPlanStub",
    "SimulatedTaskSpecStub",
    "StubUltronAgent",
    "SystemClock",
    "TelemetryPayloadStub",
    "UltronAgentPort",
    "VehicleAdapterPort",
    "VehicleAdapterState",
    "VehicleCommandEnvelopeStub",
    "VehicleId",
    "VehicleMode",
    "VehicleType",
    "WorldModelSnapshotStub",
]
