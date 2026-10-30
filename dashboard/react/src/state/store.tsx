import React, { createContext, useContext, useReducer, type ReactNode } from "react";
import type {
  EmergencyStopResponsePayload,
  EventReadPayload,
  FloorPlanConfigPayload,
  MissionDetailPayload,
  MissionStatus,
  ObservationReadPayload,
  ReasoningTraceReadPayload,
  TaskReadPayload,
  VehicleConnectionState,
  VehicleId,
  VehicleMode,
  VehicleReadPayload,
  VehicleStatus,
  VehicleType,
} from "../api/client";

export type {
  VehicleId,
  VehicleMode,
  VehicleType,
  VehicleConnectionState,
  VehicleStatus,
  MissionStatus,
};

/**
 * WebSocket message envelope types defined in Architecture §5.2 + initial snapshot.
 */
export type DashboardEnvelopeType =
  | "snapshot"
  | "telemetry"
  | "event"
  | "reasoning_trace"
  | "mission_status";

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
    HAWK01: {
      id: "HAWK01",
      vehicleType: "aerial",
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
      if (snap.vehicles.HAWK01) {
        nextVehicles.HAWK01 = mapVehicleReadToSummary(
          snap.vehicles.HAWK01,
          state.vehicles.HAWK01,
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

      // Guard against out-of-order WebSocket frames by sequence or heartbeat timestamp
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
