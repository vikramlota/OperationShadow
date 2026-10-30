import React, { createContext, useContext, useReducer, type ReactNode } from "react";
import type {
  DecisionTracePayload,
  DetectionCoordinateFrame,
  DetectionFrameClassification,
  DetectionObjectPayload,
  DetectionsPushPayload,
  EmergencyStopResponsePayload,
  EpistemicStatus,
  EventReadPayload,
  FloorPlanConfigPayload,
  FreeSpacePayload,
  MissionDetailPayload,
  MissionStatus,
  ObservationReadPayload,
  ReasoningTraceReadPayload,
  RosConnectionState,
  SensorHealthPayload,
  TaskReadPayload,
  TelemetrySource,
  TrackedObjectPayload,
  VehicleConnectionState,
  VehicleId,
  VehicleMode,
  VehicleReadPayload,
  VehicleStatus,
  VehicleType,
} from "../api/client";

export type {
  DecisionTracePayload,
  DetectionCoordinateFrame,
  DetectionFrameClassification,
  DetectionObjectPayload,
  DetectionsPushPayload,
  EpistemicStatus,
  FreeSpacePayload,
  MissionStatus,
  RosConnectionState,
  SensorHealthPayload,
  TelemetrySource,
  TrackedObjectPayload,
  VehicleConnectionState,
  VehicleId,
  VehicleMode,
  VehicleStatus,
  VehicleType,
};

/**
 * WebSocket message envelope types defined in Architecture §5.2 + initial snapshot + detections.
 */
export type DashboardEnvelopeType =
  | "snapshot"
  | "telemetry"
  | "event"
  | "reasoning_trace"
  | "mission_status"
  | "detections";

export interface TelemetryPushPayload {
  vehicle_id: VehicleId;
  status: VehicleStatus;
  connection: VehicleConnectionState;
  mode: VehicleMode;
  battery: number;
  position_x: number;
  position_y: number;
  position_alt: number;
  last_heartbeat: string | null;
}

export interface MissionStatusPushPayload {
  mission_id: string;
  objective: string;
  status: MissionStatus;
  tasks: TaskReadPayload[];
  completed_at: string | null;
}

export interface DashboardSnapshotWirePayload {
  protocol_version: string;
  generated_at: string;
  current_mission: MissionDetailPayload | null;
  vehicles: Partial<Record<VehicleId, VehicleReadPayload>>;
  detections?: Partial<Record<VehicleId, DetectionsPushPayload>>;
  recent_events: EventReadPayload[];
  recent_reasoning_traces: ReasoningTraceReadPayload[];
}

export interface DashboardSocketEnvelope<TPayload = unknown> {
  protocol_version?: string;
  envelope_id?: string | null;
  timestamp?: string;
  vehicle_id?: VehicleId | null;
  sequence?: number | null;
  type: DashboardEnvelopeType;
  payload: TPayload;
}

export interface VehicleSummaryState {
  id: VehicleId;
  vehicleType: VehicleType;
  status: VehicleStatus;
  connection: VehicleConnectionState;
  mode: VehicleMode;
  battery: number;
  position: { x: number; y: number; alt: number };
  lastHeartbeat: string | null;
  isStale: boolean;
  activeTaskId: string | null;
  lastSequence: number;
}

export interface VehicleDetectionSummaryState {
  vehicleId: VehicleId;
  configuredVehicleId: string | null;
  telemetrySource: TelemetrySource;
  actuationLocked: boolean;
  rosConnectionState: RosConnectionState;
  topic: string;
  messageType: string;
  stamp: number | null;
  sourceTimestamp: string | null;
  receivedAt: string | null;
  clockOffsetSeconds: number | null;
  frame: DetectionCoordinateFrame | null;
  frameClassification: DetectionFrameClassification | null;
  mapProjectionAllowed: boolean;
  objects: DetectionObjectPayload[];
  isStale: boolean;
  staleAfterSeconds: number;
  hasReceivedFrame: boolean;
  outOfOrderCount: number;
  invalidMessageCount: number;
  lastSequence: number;
  trackedObjects: TrackedObjectPayload[];
  sensorHealth: Record<string, SensorHealthPayload>;
  freeSpace: FreeSpacePayload | null;
  decisionTraces: DecisionTracePayload[];
}

export interface DashboardState {
  backendReachable: boolean;
  wsConnected: boolean;
  wsReconnectAttempt: number;
  wsLastError: string | null;
  hardwareLockout: boolean;
  ultronIsStub: boolean;
  selectedVehicleId: VehicleId;
  activeMissionId: string | null;
  missionObjective: string | null;
  missionStatus: string;
  missionTasks: TaskReadPayload[];
  vehicles: Record<VehicleId, VehicleSummaryState>;
  detections: Record<VehicleId, VehicleDetectionSummaryState>;
  observations: ObservationReadPayload[];
  events: EventReadPayload[];
  reasoningTraces: ReasoningTraceReadPayload[];
  floorPlan: FloorPlanConfigPayload | null;
  lastEmergencyStopResult: EmergencyStopResponsePayload | null;
}

export type DashboardAction =
  | { type: "SET_BACKEND_HEALTH"; payload: { reachable: boolean; hardwareLockout: boolean; ultronIsStub: boolean } }
  | { type: "SET_WS_CONNECTED"; payload: boolean }
  | { type: "SET_WS_RECONNECTING"; payload: { attempt: number; error: string | null } }
  | { type: "SELECT_VEHICLE"; payload: VehicleId }
  | { type: "SET_VEHICLE_MODE"; payload: { vehicleId: VehicleId; mode: VehicleMode } }
  | { type: "HYDRATE_SNAPSHOT"; payload: Partial<DashboardState> }
  | { type: "APPLY_WS_SNAPSHOT"; payload: DashboardSnapshotWirePayload }
  | { type: "APPLY_WS_TELEMETRY"; payload: { telemetry: TelemetryPushPayload; sequence?: number | null } }
  | { type: "APPLY_WS_DETECTIONS"; payload: { detections: DetectionsPushPayload; sequence?: number | null } }
  | { type: "SET_DETECTIONS_STALE"; payload: { vehicleId: VehicleId; isStale: boolean } }
  | { type: "APPLY_VEHICLE_STATUS"; payload: VehicleReadPayload }
  | { type: "APPLY_WS_EVENT"; payload: EventReadPayload }
  | { type: "SET_EVENTS"; payload: EventReadPayload[] }
  | { type: "APPLY_WS_REASONING_TRACE"; payload: ReasoningTraceReadPayload }
  | { type: "APPLY_WS_MISSION_STATUS"; payload: MissionStatusPushPayload }
  | { type: "SET_MISSION_DETAIL"; payload: MissionDetailPayload }
  | { type: "SET_OBSERVATIONS"; payload: ObservationReadPayload[] }
  | { type: "SET_FLOOR_PLAN"; payload: FloorPlanConfigPayload }
  | { type: "SET_EMERGENCY_STOP_RESULT"; payload: EmergencyStopResponsePayload | null };

export const STALE_HEARTBEAT_THRESHOLD_MS = 1500;
export const DEFAULT_DETECTION_STALE_SECONDS = 2.0;

export function isHeartbeatTimestampStale(
  lastHeartbeat: string | null,
  nowMs: number = Date.now(),
  thresholdMs: number = STALE_HEARTBEAT_THRESHOLD_MS,
): boolean {
  if (!lastHeartbeat) {
    return false;
  }
  const parsed = Date.parse(lastHeartbeat);
  if (Number.isNaN(parsed)) {
    return false;
  }
  return nowMs - parsed > thresholdMs;
}

export function isDetectionStreamTimestampStale(
  receivedAt: string | null,
  staleAfterSeconds: number = DEFAULT_DETECTION_STALE_SECONDS,
  nowMs: number = Date.now(),
): boolean {
  if (!receivedAt) {
    return false;
  }
  const parsed = Date.parse(receivedAt);
  if (Number.isNaN(parsed)) {
    return false;
  }
  return nowMs - parsed > staleAfterSeconds * 1000;
}

export function deriveFrameClassification(
  frame: string | null,
  explicitClassification?: DetectionFrameClassification | null,
  mapProjectionAllowed?: boolean,
): DetectionFrameClassification | null {
  if (explicitClassification) {
    return explicitClassification;
  }
  if (!frame) {
    return null;
  }
  if (frame === "oak_rgb_camera_optical_frame") {
    return "CAMERA_OPTICAL_RELATIVE";
  }
  if (frame === "oak_parent_frame") {
    return "CAMERA_PARENT_RELATIVE";
  }
  if (frame === "map") {
    return mapProjectionAllowed ? "CONFIRMED_MAP" : "UNCONFIRMED_MAP";
  }
  return "UNSUPPORTED_FRAME";
}

function buildDefaultSensorHealth(
  d: DetectionsPushPayload,
  computedStale: boolean,
): Record<string, SensorHealthPayload> {
  if (d.sensor_health && Object.keys(d.sensor_health).length > 0) {
    return d.sensor_health;
  }
  const camStatus: SensorHealthPayload["status"] =
    d.ros_connection_state === "DISABLED" && !d.has_received_frame
      ? "UNAVAILABLE"
      : d.ros_connection_state !== "CONNECTED"
        ? "DISCONNECTED"
        : computedStale
          ? "STALE"
          : d.telemetry_source === "rosbridge"
            ? "CONNECTED"
            : "SIMULATED";

  return {
    depth_camera: {
      sensor_id: "depth_camera",
      sensor_type: "depth_camera",
      enabled: d.ros_connection_state !== "DISABLED" || d.has_received_frame,
      status: camStatus,
      source_mode: d.telemetry_source === "rosbridge" ? "physical" : "simulated",
      topic: d.topic,
      message_type: d.message_type ?? "std_msgs/msg/String",
      coordinate_frame: d.frame ?? "oak_rgb_camera_optical_frame",
      vehicle_id: d.configured_vehicle_id ?? null,
      last_message_timestamp: d.stamp,
      last_ingested_at: d.received_at ? Date.parse(d.received_at) / 1000 : null,
      message_frequency_hz: d.has_received_frame && !computedStale ? 5.0 : 0.0,
      is_stale: computedStale,
      stale_after_sec: d.stale_after_seconds ?? DEFAULT_DETECTION_STALE_SECONDS,
      valid_message_count: d.has_received_frame ? 1 : 0,
      malformed_message_count: d.invalid_message_count ?? 0,
      duplicate_message_count: 0,
      out_of_order_count: d.out_of_order_count ?? 0,
      error_state: null,
      notes: "Verified /detections_json object stream. Raw depth images not claimed.",
    },
    lidar: {
      sensor_id: "lidar",
      sensor_type: "lidar",
      enabled: false,
      status: "UNAVAILABLE",
      source_mode: "simulated",
      topic: "",
      message_type: "",
      coordinate_frame: "",
      vehicle_id: null,
      last_message_timestamp: null,
      last_ingested_at: null,
      message_frequency_hz: 0.0,
      is_stale: false,
      stale_after_sec: 2.0,
      valid_message_count: 0,
      malformed_message_count: 0,
      duplicate_message_count: 0,
      out_of_order_count: 0,
      error_state: null,
      notes: "LiDAR topic, message type, frame, and extrinsics unconfirmed by hardware team; marked UNAVAILABLE.",
    },
  };
}

function buildDefaultTrackedObjects(
  d: DetectionsPushPayload,
  computedStale: boolean,
): TrackedObjectPayload[] {
  if (d.tracked_objects && d.tracked_objects.length > 0) {
    return d.tracked_objects;
  }
  const stampSec = d.stamp ?? Date.now() / 1000;
  const ingestedSec = d.received_at ? Date.parse(d.received_at) / 1000 : stampSec;
  return (d.objects ?? []).map((obj, idx) => ({
    object_id: `OBJ-${String(idx + 1).padStart(3, "0")}`,
    label: obj.class,
    position: {
      x: obj.position.x,
      y: obj.position.y,
      z: obj.position.z,
      range_m: obj.range_m,
      uncertainty_m: Number(Math.max(0.05, 0.08 * obj.range_m).toFixed(3)),
    },
    coordinate_frame: d.frame ?? "oak_rgb_camera_optical_frame",
    position_uncertainty_m: Number(Math.max(0.05, 0.08 * obj.range_m).toFixed(3)),
    classification_confidence: obj.score,
    first_observed_timestamp: stampSec,
    last_observed_timestamp: stampSec,
    last_ingested_at: ingestedSec,
    last_known_source: "depth_camera",
    vehicle_id: d.configured_vehicle_id ?? null,
    source_mode: d.telemetry_source === "rosbridge" ? "physical" : "simulated",
    contributing_observation_ids: [`OBS-CAM-${obj.id}`],
    contributing_sensor_types: ["depth_camera"],
    last_detector_object_id: obj.id,
    tracking_status: computedStale ? "STALE" : "ACTIVE",
    epistemic_status: computedStale ? "STALE" : "OBSERVED",
    currently_visible: !computedStale,
    corroborated_by_multiple_sensors: false,
    is_obstacle: ["chair", "bench", "box", "obstacle", "wall"].includes(
      obj.class.toLowerCase(),
    ),
    is_hazard: ["fire", "smoke", "hazard"].includes(obj.class.toLowerCase()),
  }));
}

export function mapDetectionsPayloadToSummary(
  d: DetectionsPushPayload,
  prev?: VehicleDetectionSummaryState,
  sequence?: number | null,
): VehicleDetectionSummaryState {
  const computedStale =
    d.is_stale ||
    (d.has_received_frame &&
      isDetectionStreamTimestampStale(d.received_at, d.stale_after_seconds));
  const mapAllowed = Boolean(d.map_projection_allowed);
  const frameClass = deriveFrameClassification(
    d.frame,
    d.frame_classification,
    mapAllowed,
  );
  return {
    vehicleId: d.vehicle_id,
    configuredVehicleId:
      d.configured_vehicle_id !== undefined
        ? d.configured_vehicle_id
        : (d.telemetry_source === "rosbridge" ? d.vehicle_id : null),
    telemetrySource: d.telemetry_source,
    actuationLocked: d.actuation_locked,
    rosConnectionState: d.ros_connection_state,
    topic: d.topic,
    messageType: d.message_type ?? "std_msgs/msg/String",
    stamp: d.stamp,
    sourceTimestamp: d.source_timestamp,
    receivedAt: d.received_at,
    clockOffsetSeconds: d.clock_offset_seconds ?? null,
    frame: d.frame,
    frameClassification: frameClass,
    mapProjectionAllowed: mapAllowed,
    objects: d.objects ?? [],
    isStale: computedStale,
    staleAfterSeconds: d.stale_after_seconds ?? DEFAULT_DETECTION_STALE_SECONDS,
    hasReceivedFrame: d.has_received_frame,
    outOfOrderCount: d.out_of_order_count ?? 0,
    invalidMessageCount: d.invalid_message_count ?? 0,
    lastSequence:
      typeof sequence === "number" ? sequence : (prev?.lastSequence ?? 0),
    trackedObjects: buildDefaultTrackedObjects(d, computedStale),
    sensorHealth: buildDefaultSensorHealth(d, computedStale),
    freeSpace:
      d.free_space ?? {
        is_verified_clear: false,
        free_space_radius_m: null,
        epistemic_status: "UNKNOWN",
        reason:
          "Cannot verify free space: 360° LiDAR geometric coverage is UNAVAILABLE.",
      },
    decisionTraces: d.decision_traces ?? prev?.decisionTraces ?? [],
  };
}

export const initialDashboardState: DashboardState = {
  backendReachable: false,
  wsConnected: false,
  wsReconnectAttempt: 0,
  wsLastError: null,
  hardwareLockout: true,
  ultronIsStub: true,
  selectedVehicleId: "SCOUT01",
  activeMissionId: null,
  missionObjective: null,
  missionStatus: "STANDBY",
  missionTasks: [],
  vehicles: {
    SCOUT01: {
      id: "SCOUT01",
      vehicleType: "ground",
      status: "IDLE",
      connection: "DISCONNECTED",
      mode: "simulated",
      battery: 100,
      position: { x: 0, y: 0, alt: 0 },
      lastHeartbeat: null,
      isStale: false,
      activeTaskId: null,
      lastSequence: 0,
    },
  },
  detections: {
    SCOUT01: {
      vehicleId: "SCOUT01",
      configuredVehicleId: null,
      telemetrySource: "mock",
      actuationLocked: true,
      rosConnectionState: "DISABLED",
      topic: "/detections_json",
      messageType: "std_msgs/msg/String",
      stamp: null,
      sourceTimestamp: null,
      receivedAt: null,
      clockOffsetSeconds: null,
      frame: null,
      frameClassification: null,
      mapProjectionAllowed: false,
      objects: [],
      isStale: false,
      staleAfterSeconds: DEFAULT_DETECTION_STALE_SECONDS,
      hasReceivedFrame: false,
      outOfOrderCount: 0,
      invalidMessageCount: 0,
      lastSequence: 0,
      trackedObjects: [],
      sensorHealth: {
        depth_camera: {
          sensor_id: "depth_camera",
          sensor_type: "depth_camera",
          enabled: false,
          status: "DISCONNECTED",
          source_mode: "simulated",
          topic: "/detections_json",
          message_type: "std_msgs/msg/String",
          coordinate_frame: "oak_rgb_camera_optical_frame",
          vehicle_id: null,
          last_message_timestamp: null,
          last_ingested_at: null,
          message_frequency_hz: 0.0,
          is_stale: false,
          stale_after_sec: 2.0,
          valid_message_count: 0,
          malformed_message_count: 0,
          duplicate_message_count: 0,
          out_of_order_count: 0,
          error_state: null,
          notes: "Depth camera object detection stream (/detections_json).",
        },
        lidar: {
          sensor_id: "lidar",
          sensor_type: "lidar",
          enabled: false,
          status: "UNAVAILABLE",
          source_mode: "simulated",
          topic: "",
          message_type: "",
          coordinate_frame: "",
          vehicle_id: null,
          last_message_timestamp: null,
          last_ingested_at: null,
          message_frequency_hz: 0.0,
          is_stale: false,
          stale_after_sec: 2.0,
          valid_message_count: 0,
          malformed_message_count: 0,
          duplicate_message_count: 0,
          out_of_order_count: 0,
          error_state: null,
          notes: "LiDAR topic, message type, frame, and extrinsics unconfirmed by hardware team; marked UNAVAILABLE.",
        },
      },
      freeSpace: {
        is_verified_clear: false,
        free_space_radius_m: null,
        epistemic_status: "UNKNOWN",
        reason: "Cannot verify free space: 360° LiDAR geometric coverage is UNAVAILABLE.",
      },
      decisionTraces: [],
    },
  },
  observations: [],
  events: [],
  reasoningTraces: [],
  floorPlan: null,
  lastEmergencyStopResult: null,
};

function mapVehicleReadToSummary(
  v: VehicleReadPayload,
  prev?: VehicleSummaryState,
): VehicleSummaryState {
  return {
    id: v.id,
    vehicleType: v.vehicle_type,
    status: v.status,
    connection: v.connection,
    mode: v.mode,
    battery: v.battery,
    position: { x: v.position_x, y: v.position_y, alt: v.position_alt },
    lastHeartbeat: v.last_heartbeat,
    isStale: v.is_stale ?? isHeartbeatTimestampStale(v.last_heartbeat),
    activeTaskId: v.active_task_id ?? prev?.activeTaskId ?? null,
    lastSequence: prev?.lastSequence ?? 0,
  };
}

function mergeChronologicalEvents(
  existing: EventReadPayload[],
  incoming: EventReadPayload[],
  maxLimit = 200,
): EventReadPayload[] {
  const byId = new Map<string, EventReadPayload>();
  for (const e of existing) {
    byId.set(e.id, e);
  }
  for (const e of incoming) {
    byId.set(e.id, e);
  }
  const merged = Array.from(byId.values());
  merged.sort((a, b) => {
    const dt = Date.parse(a.timestamp) - Date.parse(b.timestamp);
    return dt !== 0 ? dt : a.id.localeCompare(b.id);
  });
  return merged.slice(-maxLimit);
}

function mergeChronologicalTraces(
  existing: ReasoningTraceReadPayload[],
  incoming: ReasoningTraceReadPayload[],
  maxLimit = 100,
): ReasoningTraceReadPayload[] {
  const byId = new Map<string, ReasoningTraceReadPayload>();
  for (const rt of existing) {
    byId.set(rt.id, rt);
  }
  for (const rt of incoming) {
    byId.set(rt.id, rt);
  }
  const merged = Array.from(byId.values());
  merged.sort((a, b) => {
    const dt = Date.parse(a.timestamp) - Date.parse(b.timestamp);
    return dt !== 0 ? dt : a.id.localeCompare(b.id);
  });
  return merged.slice(-maxLimit);
}

export function dashboardReducer(
  state: DashboardState,
  action: DashboardAction,
): DashboardState {
  switch (action.type) {
    case "SET_BACKEND_HEALTH":
      return {
        ...state,
        backendReachable: action.payload.reachable,
        hardwareLockout: action.payload.hardwareLockout,
        ultronIsStub: action.payload.ultronIsStub,
      };

    case "SET_WS_CONNECTED":
      return {
        ...state,
        wsConnected: action.payload,
        wsReconnectAttempt: action.payload ? 0 : state.wsReconnectAttempt,
        wsLastError: action.payload ? null : state.wsLastError,
      };

    case "SET_WS_RECONNECTING":
      return {
        ...state,
        wsConnected: false,
        wsReconnectAttempt: action.payload.attempt,
        wsLastError: action.payload.error,
      };

    case "SELECT_VEHICLE":
      return {
        ...state,
        selectedVehicleId: action.payload,
      };

    case "SET_VEHICLE_MODE": {
      const { vehicleId, mode } = action.payload;
      if (state.hardwareLockout && mode === "real") {
        return state;
      }
      return {
        ...state,
        vehicles: {
          ...state.vehicles,
          [vehicleId]: {
            ...state.vehicles[vehicleId],
            mode,
          },
        },
      };
    }

    case "HYDRATE_SNAPSHOT":
      return { ...state, ...action.payload };

    case "APPLY_WS_SNAPSHOT": {
      const snap = action.payload;
      const nextVehicles = { ...state.vehicles };
      if (snap.vehicles.SCOUT01) {
        nextVehicles.SCOUT01 = mapVehicleReadToSummary(
          snap.vehicles.SCOUT01,
          state.vehicles.SCOUT01,
        );
      }

      const nextDetections = { ...state.detections };
      if (snap.detections?.SCOUT01) {
        nextDetections.SCOUT01 = mapDetectionsPayloadToSummary(
          snap.detections.SCOUT01,
          state.detections.SCOUT01,
        );
      }

      return {
        ...state,
        backendReachable: true,
        activeMissionId: snap.current_mission?.id ?? state.activeMissionId,
        missionObjective: snap.current_mission?.objective ?? state.missionObjective,
        missionStatus: snap.current_mission?.status ?? state.missionStatus,
        missionTasks: snap.current_mission?.tasks ?? state.missionTasks,
        vehicles: nextVehicles,
        detections: nextDetections,
        events: mergeChronologicalEvents([], snap.recent_events),
        reasoningTraces: mergeChronologicalTraces([], snap.recent_reasoning_traces),
      };
    }

    case "APPLY_WS_TELEMETRY": {
      const { telemetry, sequence } = action.payload;
      const prev = state.vehicles[telemetry.vehicle_id];
      if (!prev) {
        return state;
      }

      if (
        typeof sequence === "number" &&
        prev.lastSequence > 0 &&
        sequence < prev.lastSequence
      ) {
        return state;
      }
      if (
        prev.lastHeartbeat &&
        telemetry.last_heartbeat &&
        Date.parse(telemetry.last_heartbeat) < Date.parse(prev.lastHeartbeat)
      ) {
        return state;
      }

      const nextVehicle: VehicleSummaryState = {
        ...prev,
        status: telemetry.status,
        connection: telemetry.connection,
        mode: telemetry.mode,
        battery: telemetry.battery,
        position: {
          x: telemetry.position_x,
          y: telemetry.position_y,
          alt: telemetry.position_alt,
        },
        lastHeartbeat: telemetry.last_heartbeat,
        isStale: isHeartbeatTimestampStale(telemetry.last_heartbeat),
        lastSequence:
          typeof sequence === "number" ? sequence : prev.lastSequence,
      };

      return {
        ...state,
        vehicles: {
          ...state.vehicles,
          [telemetry.vehicle_id]: nextVehicle,
        },
      };
    }

    case "APPLY_WS_DETECTIONS": {
      const { detections, sequence } = action.payload;
      const prev = state.detections[detections.vehicle_id];
      if (!prev) {
        return state;
      }

      // Reject out-of-order detection frames by source timestamp (`stamp`)
      if (
        prev.stamp !== null &&
        detections.stamp !== null &&
        detections.stamp < prev.stamp
      ) {
        return {
          ...state,
          detections: {
            ...state.detections,
            [detections.vehicle_id]: {
              ...prev,
              outOfOrderCount: prev.outOfOrderCount + 1,
            },
          },
        };
      }

      const nextDetectionState = mapDetectionsPayloadToSummary(
        detections,
        prev,
        sequence,
      );

      return {
        ...state,
        detections: {
          ...state.detections,
          [detections.vehicle_id]: nextDetectionState,
        },
      };
    }

    case "SET_DETECTIONS_STALE": {
      const { vehicleId, isStale } = action.payload;
      const prev = state.detections[vehicleId];
      if (!prev || prev.isStale === isStale) {
        return state;
      }
      return {
        ...state,
        detections: {
          ...state.detections,
          [vehicleId]: {
            ...prev,
            isStale,
          },
        },
      };
    }

    case "APPLY_VEHICLE_STATUS": {
      const v = action.payload;
      const prev = state.vehicles[v.id];
      return {
        ...state,
        vehicles: {
          ...state.vehicles,
          [v.id]: mapVehicleReadToSummary(v, prev),
        },
      };
    }

    case "APPLY_WS_EVENT":
      return {
        ...state,
        events: mergeChronologicalEvents(state.events, [action.payload]),
      };

    case "SET_EVENTS":
      return {
        ...state,
        events: mergeChronologicalEvents([], action.payload),
      };

    case "APPLY_WS_REASONING_TRACE":
      return {
        ...state,
        reasoningTraces: mergeChronologicalTraces(state.reasoningTraces, [
          action.payload,
        ]),
      };

    case "APPLY_WS_MISSION_STATUS": {
      const m = action.payload;
      return {
        ...state,
        activeMissionId: m.mission_id,
        missionObjective: m.objective,
        missionStatus: m.status,
        missionTasks: m.tasks,
      };
    }

    case "SET_MISSION_DETAIL": {
      const m = action.payload;
      return {
        ...state,
        activeMissionId: m.id,
        missionObjective: m.objective,
        missionStatus: m.status,
        missionTasks: m.tasks,
      };
    }

    case "SET_OBSERVATIONS":
      return {
        ...state,
        observations: action.payload,
      };

    case "SET_FLOOR_PLAN":
      return {
        ...state,
        floorPlan: action.payload,
      };

    case "SET_EMERGENCY_STOP_RESULT":
      return {
        ...state,
        lastEmergencyStopResult: action.payload,
      };

    default:
      return state;
  }
}

interface DashboardContextValue {
  state: DashboardState;
  dispatch: React.Dispatch<DashboardAction>;
}

const DashboardContext = createContext<DashboardContextValue | undefined>(undefined);

export function DashboardProvider({
  children,
  initialState = initialDashboardState,
}: {
  children: ReactNode;
  initialState?: DashboardState;
}): React.JSX.Element {
  const [state, dispatch] = useReducer(dashboardReducer, initialState);
  return (
    <DashboardContext.Provider value={{ state, dispatch }}>
      {children}
    </DashboardContext.Provider>
  );
}

export function useDashboardStore(): DashboardContextValue {
  const context = useContext(DashboardContext);
  if (!context) {
    throw new Error("useDashboardStore must be used within a DashboardProvider");
  }
  return context;
}
