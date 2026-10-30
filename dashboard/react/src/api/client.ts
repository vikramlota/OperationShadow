/**
 * Typed REST API client and shared backend contracts for Operation Shadow.
 * Consumes the exact FastAPI contracts implemented in Milestones 1–4.
 */

export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export const WS_DASHBOARD_URL: string =
  import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws/dashboard";

export const DEFAULT_SCOUT_STREAM_URL: string =
  import.meta.env.VITE_SCOUT01_STREAM_URL ?? "";

export const DEFAULT_HAWK_STREAM_URL: string =
  import.meta.env.VITE_HAWK01_STREAM_URL ?? "";

export type VehicleId = "SCOUT01" | "HAWK01";
export type EmergencyStopTarget = "ALL" | "SCOUT01" | "HAWK01";
export type VehicleMode = "simulated" | "real";
export type VehicleType = "ground" | "aerial";
export type VehicleConnectionState = "CONNECTED" | "DISCONNECTED" | "UNKNOWN";
export type VehicleStatus =
  | "IDLE"
  | "ACTIVE"
  | "EXECUTING"
  | "UNKNOWN"
  | "EMERGENCY_STOPPED"
  | "FAULT";

export type MissionStatus =
  | "PENDING"
  | "PLANNING"
  | "ACTIVE"
  | "COMPLETED"
  | "ABORTED"
  | "FAILED";

export type TaskStatus =
  | "PENDING"
  | "ASSIGNED"
  | "IN_PROGRESS"
  | "COMPLETED"
  | "CANCELLED"
  | "FAILED";

export interface ApiErrorPayload {
  error_code?: string;
  message?: string;
  details?: Array<{ loc?: Array<string | number>; message: string }> | Record<string, unknown>;
}

export class ApiClientError extends Error {
  public readonly status: number;
  public readonly errorCode: string;

  constructor(message: string, status: number, errorCode = "API_ERROR") {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.errorCode = errorCode;
  }
}

async function requestJson<TResponse>(
  path: string,
  init?: RequestInit,
): Promise<TResponse> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    let errorMessage = `Request to ${path} failed with HTTP ${response.status}`;
    let errorCode = `HTTP_${response.status}`;
    try {
      const errBody = (await response.json()) as ApiErrorPayload;
      if (errBody.error_code) {
        errorCode = errBody.error_code;
      }
      if (Array.isArray(errBody.details) && errBody.details.length > 0) {
        errorMessage = errBody.details.map((d) => d.message).join("; ");
      } else if (errBody.message) {
        errorMessage = errBody.message;
      }
    } catch {
      // Keep default HTTP status message if body is not JSON
    }
    throw new ApiClientError(errorMessage, response.status, errorCode);
  }

  return (await response.json()) as TResponse;
}

// ---------------------------------------------------------------------------
// Backend REST Contracts
// ---------------------------------------------------------------------------

export interface HealthCheckPayload {
  status: string;
  database: string;
  hardware_lockout: boolean;
  default_vehicle_mode: string;
  ultron_is_stub: boolean;
  vehicles: string[];
}

export interface TaskReadPayload {
  id: string;
  mission_id: string;
  vehicle_id: VehicleId | null;
  type: string;
  area: string | null;
  target: string | null;
  priority: number;
  status: TaskStatus;
}

export interface MissionDetailPayload {
  id: string;
  objective: string;
  status: MissionStatus;
  created_at: string;
  completed_at: string | null;
  tasks: TaskReadPayload[];
}

export interface MissionCreateResponsePayload {
  mission_id: string;
}

export interface VehicleReadPayload {
  id: VehicleId;
  vehicle_type: VehicleType;
  status: VehicleStatus;
  battery: number;
  position_x: number;
  position_y: number;
  position_alt: number;
  connection: VehicleConnectionState;
  mode: VehicleMode;
  last_heartbeat: string | null;
  is_stale?: boolean;
  active_task_id?: string | null;
}

export interface VehicleSensorsPayload {
  vehicle_id: VehicleId;
  status: VehicleStatus;
  connection: VehicleConnectionState;
  mode: VehicleMode;
  battery: number;
  position_x: number;
  position_y: number;
  position_alt: number;
  last_heartbeat: string | null;
  is_stale: boolean;
  sequence: number;
  raw_sensors: Record<string, unknown>;
}

export interface ObservationReadPayload {
  id: string;
  mission_id: string;
  vehicle_id: VehicleId;
  obs_type: string;
  confidence: number;
  location_x: number;
  location_y: number;
  timestamp: string;
  raw: Record<string, unknown> | null;
}

export interface EventReadPayload {
  id: string;
  mission_id: string;
  timestamp: string;
  category: string;
  message: string;
}

export interface ReasoningTraceReadPayload {
  id: string;
  mission_id: string;
  timestamp: string;
  trigger: string;
  rationale: string;
  resulting_task_ids: string[];
}

export interface MissionReplayTimelineEntryPayload {
  entry_type: "event" | "reasoning_trace";
  timestamp: string;
  event: EventReadPayload | null;
  reasoning_trace: ReasoningTraceReadPayload | null;
  world_model_snapshot: Record<string, unknown> | null;
}

export interface MissionReplayResponsePayload {
  mission_id: string;
  entries: MissionReplayTimelineEntryPayload[];
}

export interface FloorPlanRoomPayload {
  id: string;
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface FloorPlanWaypointPayload {
  id: string;
  label: string;
  x: number;
  y: number;
}

export interface FloorPlanGeofencePayload {
  id: string;
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
  max_altitude_m: number;
}

export interface FloorPlanConfigPayload {
  is_provisional_simulation_config: boolean;
  coordinate_system: string;
  width_m: number;
  height_m: number;
  rooms: FloorPlanRoomPayload[];
  waypoints: FloorPlanWaypointPayload[];
  hawk_geofence: FloorPlanGeofencePayload;
}

export interface VehicleSimulateAckPayload {
  vehicle_id: VehicleId;
  mode: VehicleMode;
  enabled: boolean;
  status: VehicleStatus;
  connection: VehicleConnectionState;
}

export interface VehicleEmergencyStopOutcomePayload {
  vehicle_id: VehicleId;
  mode: VehicleMode;
  is_simulated: boolean;
  real_hardware_available: boolean;
  connection_at_dispatch: VehicleConnectionState;
  dispatch_attempted: boolean;
  dispatch_succeeded: boolean;
  vehicle_acknowledged: boolean;
  vehicle_confirmed_stopped: boolean;
  resulting_status: VehicleStatus;
  was_already_stopped: boolean;
  dispatched_at: string;
  acknowledged_at: string | null;
  reason: string;
}

export interface EmergencyStopResponsePayload {
  request_accepted: boolean;
  target: EmergencyStopTarget;
  overall_outcome:
    | "ALL_CONFIRMED_STOPPED"
    | "PARTIAL_STOPPED"
    | "NONE_CONFIRMED_STOPPED";
  all_confirmed_stopped: boolean;
  any_dispatch_failed: boolean;
  hardware_lockout_active: boolean;
  real_hardware_estop_available: boolean;
  fast_path_used: boolean;
  async_audit_log_scheduled: boolean;
  dispatch_elapsed_ms: number;
  safety_notice: string;
  vehicle_outcomes: Partial<Record<VehicleId, VehicleEmergencyStopOutcomePayload>>;
}

// ---------------------------------------------------------------------------
// API Client Functions
// ---------------------------------------------------------------------------

export async function fetchHealth(): Promise<HealthCheckPayload> {
  return requestJson<HealthCheckPayload>("/health");
}

export async function createMission(
  objective: string,
): Promise<MissionCreateResponsePayload> {
  return requestJson<MissionCreateResponsePayload>("/mission", {
    method: "POST",
    body: JSON.stringify({ objective }),
  });
}

export async function fetchMission(
  missionId: string,
): Promise<MissionDetailPayload> {
  return requestJson<MissionDetailPayload>(
    `/mission/${encodeURIComponent(missionId)}`,
  );
}

export async function fetchMissionReplay(
  missionId: string,
): Promise<MissionReplayResponsePayload> {
  return requestJson<MissionReplayResponsePayload>(
    `/mission/${encodeURIComponent(missionId)}/replay`,
  );
}

export async function completeMission(
  missionId: string,
): Promise<MissionDetailPayload> {
  return requestJson<MissionDetailPayload>(
    `/mission/${encodeURIComponent(missionId)}/complete`,
    {
      method: "POST",
    },
  );
}

export async function fetchVehicleStatus(
  vehicleId: VehicleId,
): Promise<VehicleReadPayload> {
  return requestJson<VehicleReadPayload>(`/vehicle/${vehicleId}/status`);
}

export async function fetchVehicleSensors(
  vehicleId: VehicleId,
): Promise<VehicleSensorsPayload> {
  return requestJson<VehicleSensorsPayload>(`/vehicle/${vehicleId}/sensors`);
}

export async function fetchVehicleObservations(
  vehicleId: VehicleId,
  missionId?: string,
): Promise<ObservationReadPayload[]> {
  const query = missionId ? `?mission_id=${encodeURIComponent(missionId)}` : "";
  return requestJson<ObservationReadPayload[]>(
    `/vehicle/${vehicleId}/observations${query}`,
  );
}

export async function setVehicleSimulatedMode(
  vehicleId: VehicleId,
  enabled: boolean,
): Promise<VehicleSimulateAckPayload> {
  return requestJson<VehicleSimulateAckPayload>(
    `/vehicle/${vehicleId}/simulate`,
    {
      method: "POST",
      body: JSON.stringify({ enabled }),
    },
  );
}

export async function fetchEvents(
  missionId?: string,
): Promise<EventReadPayload[]> {
  const query = missionId ? `?mission_id=${encodeURIComponent(missionId)}` : "";
  return requestJson<EventReadPayload[]>(`/events${query}`);
}

export async function fetchFloorMap(): Promise<FloorPlanConfigPayload> {
  return requestJson<FloorPlanConfigPayload>("/map");
}

export async function triggerEmergencyStop(
  vehicleId: EmergencyStopTarget,
): Promise<EmergencyStopResponsePayload> {
  return requestJson<EmergencyStopResponsePayload>("/emergency-stop", {
    method: "POST",
    body: JSON.stringify({ vehicle_id: vehicleId }),
  });
}
