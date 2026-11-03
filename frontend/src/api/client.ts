/**
 * Typed REST API client and shared backend contracts for Operation Shadow.
 * Consumes the exact FastAPI contracts implemented in Milestones 1–6 + ROS 2 Humble Perception
 * + Evidence-Grounded Multi-Sensor ULTRON Environmental Intelligence.
 */

export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export const WS_DASHBOARD_URL: string =
  import.meta.env.VITE_WS_URL ?? "ws://localhost:8000/ws/dashboard";

export const WS_SCOUT_CONTROL_URL: string =
  import.meta.env.VITE_WS_CONTROL_URL ?? "ws://localhost:8000/ws/scout/control";

export const DEFAULT_SCOUT_STREAM_URL: string =
  import.meta.env.VITE_SCOUT01_STREAM_URL ??
  "http://192.0.2.41:8081/stream.mjpg";

export const ROSBRIDGE_URL: string =
  import.meta.env.VITE_ROSBRIDGE_URL ?? "ws://192.0.2.41:9090";

export type VehicleId = "SCOUT01";
export type EmergencyStopTarget = "ALL" | "SCOUT01";
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

export type TelemetrySource = "mock" | "rosbridge";
export type RosConnectionState =
  | "DISABLED"
  | "DISCONNECTED"
  | "CONNECTING"
  | "CONNECTED"
  | "RECONNECTING"
  | "ERROR";
export type DetectionCoordinateFrame =
  | "oak_rgb_camera_optical_frame"
  | "oak_parent_frame"
  | "map"
  | (string & {});

export type DetectionFrameClassification =
  | "CAMERA_OPTICAL_RELATIVE"
  | "CAMERA_PARENT_RELATIVE"
  | "CONFIRMED_MAP"
  | "UNCONFIRMED_MAP"
  | "UNSUPPORTED_FRAME";

export type EpistemicStatus =
  | "OBSERVED"
  | "CORROBORATED"
  | "INFERRED"
  | "UNKNOWN"
  | "STALE"
  | "CONFLICTED";

export type SensorHealthStatus =
  | "CONNECTED"
  | "DISCONNECTED"
  | "UNAVAILABLE"
  | "STALE"
  | "ERROR"
  | "SIMULATED";

export type MissionStatus =
  | "PENDING"
  | "PLANNING"
  | "ACTIVE"
  | "IN_PROGRESS"
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

export interface ScoutStatusPayload {
  scout_id: string;
  rosbridge_url: string;
  enabled: boolean;
  connected: boolean;
  mode: "read_only" | "disabled";
}

export interface MapUploadResponsePayload {
  status: string;
  vehicle_id: string;
  saved_files: string[];
}

export interface ScoutControlStatusPayload {
  mode: "simulation_only";
  transmitter_connected: false;
  control_client_connected: boolean;
  current_command: "W" | "A" | "S" | "D" | "X";
  last_update: string | null;
  physical_control_available: false;
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
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
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
  ros_enabled?: boolean;
  ros_connection_state?: RosConnectionState;
  ros_vehicle_id?: string | null;
}

export interface DetectionPosition3DPayload {
  x: number;
  y: number;
  z: number;
  range_m?: number | null;
  uncertainty_m?: number;
}

export interface DetectionObjectPayload {
  id: number;
  class: string;
  score: number;
  position: DetectionPosition3DPayload;
  range_m: number;
}

export interface TrackedObjectPayload {
  object_id: string;
  label: string;
  position: DetectionPosition3DPayload;
  coordinate_frame: string;
  position_uncertainty_m: number;
  classification_confidence: number;
  first_observed_timestamp: number;
  last_observed_timestamp: number;
  last_ingested_at: number;
  last_known_source: string;
  vehicle_id?: string | null;
  source_mode: "simulated" | "physical";
  contributing_observation_ids: string[];
  contributing_sensor_types: string[];
  last_detector_object_id?: number | null;
  tracking_status: "ACTIVE" | "REMEMBERED" | "STALE" | "CONFLICTED" | "LOST";
  epistemic_status: EpistemicStatus;
  currently_visible: boolean;
  corroborated_by_multiple_sensors: boolean;
  is_obstacle?: boolean;
  is_hazard?: boolean;
  fusion_timestamp?: number | null;
  association_method?: string | null;
  association_score?: number | null;
  conflict_details?: string | null;
}

export interface SensorHealthPayload {
  sensor_id: string;
  sensor_type: "depth_camera" | "lidar" | "telemetry" | "synthetic" | string;
  enabled: boolean;
  status: SensorHealthStatus;
  source_mode: "simulated" | "physical";
  topic: string;
  message_type: string;
  coordinate_frame: string;
  vehicle_id?: string | null;
  last_message_timestamp: number | null;
  last_ingested_at: number | null;
  message_frequency_hz: number;
  is_stale: boolean;
  stale_after_sec: number;
  valid_message_count: number;
  malformed_message_count: number;
  duplicate_message_count: number;
  out_of_order_count: number;
  error_state: string | null;
  notes?: string | null;
}

export interface FreeSpacePayload {
  is_verified_clear: boolean;
  free_space_radius_m: number | null;
  epistemic_status: EpistemicStatus;
  reason: string;
  supporting_sensors?: string[];
  warnings?: string[];
}

export interface EvidenceReferencePayload {
  observation_id: string;
  object_id?: string | null;
  sensor: string;
  sensor_type: string;
  timestamp: number;
  coordinate_frame: string;
  confidence?: number | null;
  epistemic_status: EpistemicStatus;
  source_mode: "simulated" | "physical";
}

export interface DecisionTracePayload {
  trace_id: string;
  mission_id: string;
  timestamp: number;
  trigger: string;
  environmental_facts_used: string[];
  inferences_made: string[];
  unknowns_and_uncertainties: string[];
  sensor_sources: string[];
  evidence_references?: EvidenceReferencePayload[];
  decision_summary: string;
  rationale: string;
  safety_validator_passed: boolean;
  safety_validator_modified_or_rejected: boolean;
  validation_errors: string[];
  replanning_triggered: boolean;
  resulting_task_ids: string[];
  confidence_level: "high" | "medium" | "low" | "unavailable";
  is_simulated: boolean;
}

export interface GroundedEnvironmentalResponsePayload {
  answer: string;
  facts: string[];
  inferences: string[];
  unknowns: string[];
  warnings: string[];
  evidence: EvidenceReferencePayload[];
  confidence: "high" | "medium" | "low" | "unavailable";
  source_mode: "simulated" | "physical";
}

export interface DetectionsPushPayload {
  vehicle_id: VehicleId;
  configured_vehicle_id?: string | null;
  telemetry_source: TelemetrySource;
  actuation_locked: boolean;
  ros_connection_state: RosConnectionState;
  topic: string;
  message_type?: string;
  stamp: number | null;
  source_timestamp: string | null;
  received_at: string | null;
  clock_offset_seconds?: number | null;
  frame: DetectionCoordinateFrame | null;
  frame_classification?: DetectionFrameClassification | null;
  map_projection_allowed?: boolean;
  objects: DetectionObjectPayload[];
  is_stale: boolean;
  stale_after_seconds: number;
  has_received_frame: boolean;
  out_of_order_count: number;
  invalid_message_count: number;
  tracked_objects?: TrackedObjectPayload[];
  sensor_health?: Record<string, SensorHealthPayload>;
  free_space?: FreeSpacePayload | null;
  decision_traces?: DecisionTracePayload[];
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

export interface MissionReportOverviewPayload {
  mission_id: string;
  objective: string;
  created_at: string;
  completed_at: string | null;
  duration_seconds: number | null;
  final_status: string;
  vehicle_mode: string;
  is_simulated_execution: boolean;
  hardware_lockout_active: boolean;
  execution_provenance: string;
}

export interface MissionReportTaskRecordPayload {
  task_id: string;
  vehicle_id: string;
  task_type: string;
  area: string | null;
  target: string | null;
  priority: number;
  status: string;
  highest_evidence_stage: string;
  is_simulated: boolean;
  source_trace_ids: string[];
  provenance_note: string;
}

export interface MissionReportTaskSummaryPayload {
  total_tasks: number;
  completed_tasks: number;
  pending_tasks: number;
  assigned_tasks: number;
  in_progress_tasks: number;
  failed_tasks: number;
  cancelled_tasks: number;
  tasks: MissionReportTaskRecordPayload[];
}

export interface MissionReportObjectFindingPayload {
  object_id: string;
  label: string;
  coordinate_frame: string;
  position_x: number;
  position_y: number;
  position_z: number;
  range_m: number | null;
  map_position_x: number | null;
  map_position_y: number | null;
  confidence: number;
  epistemic_status: string;
  tracking_status: string;
  source_mode: string;
  is_obstacle: boolean;
  is_hazard: boolean;
  matches_objective_target: boolean;
  evidence_ids: string[];
  coordinate_frame_note: string;
}

export interface MissionReportHazardFindingPayload {
  hazard_id: string;
  hazard_type: string;
  coordinate_frame: string;
  position_x: number;
  position_y: number;
  confidence: number;
  epistemic_status: string;
  source_mode: string;
  source_vehicle: string;
  evidence_ids: string[];
  handled: boolean;
  coordinate_frame_note: string;
}

export interface MissionReportObservationFindingPayload {
  observation_id: string;
  vehicle_id: string;
  sensor_type: string;
  obs_type: string;
  confidence: number;
  coordinate_frame: string;
  position_x: number;
  position_y: number;
  position_z: number;
  range_m: number | null;
  timestamp: string;
  source_mode: string;
  epistemic_status: string;
  is_stale: boolean;
}

export interface MissionReportEnvironmentalFindingsPayload {
  detected_objects: MissionReportObjectFindingPayload[];
  identified_targets: MissionReportObjectFindingPayload[];
  reported_hazards: MissionReportHazardFindingPayload[];
  obstacles: MissionReportObjectFindingPayload[];
  sensor_observations: MissionReportObservationFindingPayload[];
  evidence_references: string[];
  coordinate_frame_limitations: string[];
  coverage_note: string;
}

export interface MissionReportEventRecordPayload {
  event_id: string;
  timestamp: string;
  category: string;
  event_type: string | null;
  severity: string;
  source: string;
  source_mode: string;
  message: string;
  evidence_ids: string[];
}

export interface MissionReportStandingOrderRecordPayload {
  standing_order_id: string;
  source_event_id: string | null;
  trace_id: string | null;
  timestamp: string;
  rationale: string;
  resulting_task_ids: string[];
  evidence_ids: string[];
}

export interface MissionReportTriageRecordPayload {
  trace_id: string;
  event_id: string | null;
  event_type: string | null;
  disposition: string;
  severity: string;
  triage_source: string;
  rationale: string;
  evidence_ids: string[];
  timestamp: string;
}

export interface MissionReportDecisionRecordPayload {
  trace_id: string;
  timestamp: string;
  trigger: string;
  rationale: string;
  source_event_id: string | null;
  standing_order_id: string | null;
  triage_disposition: string | null;
  planner_source: string | null;
  validation_passed: boolean;
  validation_errors: string[];
  resulting_task_ids: string[];
  evidence_ids: string[];
  before_snapshot_id: string | null;
  after_snapshot_id: string | null;
}

export interface MissionReportEscalationRecordPayload {
  record_id: string;
  timestamp: string;
  reason: string;
  source_event_id: string | null;
  standing_order_id: string | null;
}

export interface MissionReportDecisionHistoryPayload {
  important_events: MissionReportEventRecordPayload[];
  standing_orders_applied: MissionReportStandingOrderRecordPayload[];
  llm_triage_outcomes: MissionReportTriageRecordPayload[];
  replanning_operations: MissionReportDecisionRecordPayload[];
  rejected_decisions: MissionReportDecisionRecordPayload[];
  accepted_plans: MissionReportDecisionRecordPayload[];
  human_escalation_events: MissionReportEscalationRecordPayload[];
}

export interface MissionReportBatteryRecordPayload {
  min_battery: number;
  max_battery: number;
  final_battery: number;
  sample_count: number;
  critical_low_battery_observed: boolean;
}

export interface MissionReportVehicleSummaryPayload {
  vehicle_id: string;
  vehicle_type: string;
  mode: string;
  final_status: string;
  final_connection: string;
  connection_history: string[];
  battery_observations: MissionReportBatteryRecordPayload;
  task_participation_ids: string[];
  communication_failures: string[];
  operational_limitations: string[];
}

export interface MissionReportSafetyEventPayload {
  record_id: string;
  timestamp: string;
  category:
    | "EMERGENCY_STOP"
    | "VALIDATION_FAILURE"
    | "CRITICAL_FAULT"
    | "SAFETY_WARNING";
  description: string;
  stop_dispatched: boolean | null;
  vehicle_acknowledged: boolean | null;
  physical_stop_confirmed: boolean | null;
  is_simulated: boolean;
}

export interface MissionReportSafetySummaryPayload {
  emergency_stop_events: MissionReportSafetyEventPayload[];
  safety_validation_failures: MissionReportSafetyEventPayload[];
  critical_faults: MissionReportSafetyEventPayload[];
  stop_dispatch_acknowledged_status: string;
  unresolved_safety_issues: string[];
}

export interface MissionReportUnresolvedItemsPayload {
  incomplete_tasks: string[];
  missing_observations: string[];
  stale_telemetry_or_observations: string[];
  conflicting_evidence: string[];
  unverified_sensor_signals: string[];
  unknown_outcomes: string[];
}

export interface MissionReportPayload {
  mission_id: string;
  generated_at: string;
  report_deterministic: boolean;
  llm_used_for_report: boolean;
  historical_snapshots_count: number;
  historical_state_available: boolean;
  summary_text: string;
  overview: MissionReportOverviewPayload;
  task_summary: MissionReportTaskSummaryPayload;
  environmental_findings: MissionReportEnvironmentalFindingsPayload;
  decision_history: MissionReportDecisionHistoryPayload;
  vehicle_summary: MissionReportVehicleSummaryPayload;
  safety_summary: MissionReportSafetySummaryPayload;
  unresolved_items: MissionReportUnresolvedItemsPayload;
  markdown_export: string;
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

export async function fetchScoutStatus(): Promise<ScoutStatusPayload> {
  return requestJson<ScoutStatusPayload>("/scout/status");
}

export async function uploadMapFiles(
  vehicleId: VehicleId,
  files: File[],
): Promise<MapUploadResponsePayload> {
  const body = new FormData();
  for (const file of files) {
    body.append("files", file, file.name);
  }
  return requestJson<MapUploadResponsePayload>(
    `/upload-map/${encodeURIComponent(vehicleId)}`,
    { method: "POST", body },
  );
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

export async function fetchMissionReport(
  missionId: string,
): Promise<MissionReportPayload> {
  return requestJson<MissionReportPayload>(
    `/mission/${encodeURIComponent(missionId)}/report`,
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

export async function executeMission(
  missionId: string,
): Promise<MissionDetailPayload> {
  return requestJson<MissionDetailPayload>(
    `/mission/${encodeURIComponent(missionId)}/execute`,
    {
      method: "POST",
    },
  );
}

export async function queryEnvironmentalIntelligence(
  query: string,
): Promise<GroundedEnvironmentalResponsePayload> {
  return requestJson<GroundedEnvironmentalResponsePayload>(
    "/mission/intelligence/query",
    {
      method: "POST",
      body: JSON.stringify({ query }),
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

export async function fetchVehicleDetections(
  vehicleId: VehicleId,
): Promise<DetectionsPushPayload> {
  return requestJson<DetectionsPushPayload>(`/vehicle/${vehicleId}/detections`);
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
