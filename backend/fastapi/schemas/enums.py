"""Enumerations for Operation Shadow domain entities and WebSocket contracts.

Explicitly documented values from `Operation Shadow — Backend & Frontend Architecture.md`:
- Vehicle IDs: `SCOUT01`, `HAWK01` (§5.1, §7)
- Emergency Stop targets: `ALL`, `SCOUT01`, `HAWK01` (§7)
- Vehicle operating modes: `real`, `simulated` (§6)
- Vehicle status/connection timeout state: `UNKNOWN` (§5.1)
- Dashboard WebSocket envelope types: `telemetry`, `event`, `reasoning_trace`, `mission_status`, `detections` (§5.2)

Provisional values for states not exhaustively enumerated in the Backend & Frontend
Architecture document are marked below and recorded in `docs/assumptions.md`.
"""

from __future__ import annotations

from enum import StrEnum


class VehicleIdEnum(StrEnum):
    """Documented vehicle identifiers (§5.1, §7)."""

    SCOUT01 = "SCOUT01"


class EmergencyStopTargetEnum(StrEnum):
    """Documented target selector for POST /emergency-stop (§7)."""

    ALL = "ALL"
    SCOUT01 = "SCOUT01"


class VehicleModeEnum(StrEnum):
    """Documented vehicle operating modes in VEHICLES.mode (§6)."""

    SIMULATED = "simulated"
    REAL = "real"


class TelemetrySourceEnum(StrEnum):
    """Source of perception/telemetry data (strictly separated from actuator control mode)."""

    MOCK = "mock"
    ROSBRIDGE = "rosbridge"


class RosConnectionStateEnum(StrEnum):
    """Connection states for the read-only ROSBridge perception subscriber."""

    DISABLED = "DISABLED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


class DetectionCoordinateFrameEnum(StrEnum):
    """Known coordinate frames for rover object detections (`/detections_json`).

    Note: Incoming frames may also contain unrecognized frame strings; `DetectionFrame.frame`
    preserves the exact incoming string verbatim while classifying it via
    `DetectionFrameClassificationEnum`.
    """

    OAK_RGB_CAMERA_OPTICAL_FRAME = "oak_rgb_camera_optical_frame"
    OAK_PARENT_FRAME = "oak_parent_frame"
    MAP = "map"


class DetectionFrameClassificationEnum(StrEnum):
    """Coordinate-frame safety classification for UI display and map-projection gating."""

    CAMERA_OPTICAL_RELATIVE = "CAMERA_OPTICAL_RELATIVE"
    CAMERA_PARENT_RELATIVE = "CAMERA_PARENT_RELATIVE"
    CONFIRMED_MAP = "CONFIRMED_MAP"
    UNCONFIRMED_MAP = "UNCONFIRMED_MAP"
    UNSUPPORTED_FRAME = "UNSUPPORTED_FRAME"


class VehicleTypeEnum(StrEnum):
    """Provisional vehicle domain classifications (see docs/assumptions.md)."""

    GROUND = "ground"
    AERIAL = "aerial"


class VehicleConnectionStateEnum(StrEnum):
    """Vehicle WebSocket connection states.

    `UNKNOWN` is explicitly documented for missed heartbeat timeouts (§5.1).
    `CONNECTED` and `DISCONNECTED` are provisional states (see docs/assumptions.md).
    """

    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    UNKNOWN = "UNKNOWN"


class VehicleStatusEnum(StrEnum):
    """Vehicle operational status values.

    `UNKNOWN` is explicitly documented when heartbeats time out (§5.1).
    Remaining operational states are provisional (see docs/assumptions.md).
    """

    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    EXECUTING = "EXECUTING"
    UNKNOWN = "UNKNOWN"
    EMERGENCY_STOPPED = "EMERGENCY_STOPPED"
    FAULT = "FAULT"


class MissionStatusEnum(StrEnum):
    """Mission lifecycle states."""

    PENDING = "PENDING"
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"


class TaskStatusEnum(StrEnum):
    """Provisional task lifecycle states (see docs/assumptions.md)."""

    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class EventCategoryEnum(StrEnum):
    """Provisional event log categories for EVENTS.category (see docs/assumptions.md)."""

    SYSTEM = "SYSTEM"
    MISSION = "MISSION"
    TELEMETRY = "TELEMETRY"
    OBSERVATION = "OBSERVATION"
    REASONING = "REASONING"
    SAFETY = "SAFETY"
    SECURITY = "SECURITY"


class DashboardEnvelopeTypeEnum(StrEnum):
    """Documented dashboard WebSocket message types (§5.2) plus perception detections."""

    TELEMETRY = "telemetry"
    EVENT = "event"
    REASONING_TRACE = "reasoning_trace"
    MISSION_STATUS = "mission_status"
    DETECTIONS = "detections"
