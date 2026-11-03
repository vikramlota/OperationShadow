import React, { useEffect, useState } from "react";
import {
  queryEnvironmentalIntelligence,
  type GroundedEnvironmentalResponsePayload,
} from "../api/client";
import {
  isDetectionStreamTimestampStale,
  useDashboardStore,
  type DetectionObjectPayload,
  type TrackedObjectPayload,
  type VehicleDetectionSummaryState,
  type VehicleId,
} from "../state/store";

interface PerceptionPanelProps {
  vehicleId?: VehicleId;
}

export type PerceptionStreamStatus =
  | "DISABLED"
  | "DISCONNECTED"
  | "AWAITING_MESSAGES"
  | "STALE"
  | "UNSUPPORTED_FRAME"
  | "LIVE_EMPTY"
  | "LIVE_OBJECTS";

const KNOWN_SUPPORTED_FRAMES = new Set([
  "oak_rgb_camera_optical_frame",
  "oak_parent_frame",
  "map",
]);

export function derivePerceptionStreamStatus(
  det: VehicleDetectionSummaryState,
): PerceptionStreamStatus {
  if (det.rosConnectionState === "DISABLED" && !det.hasReceivedFrame) {
    return "DISABLED";
  }
  if (
    det.rosConnectionState === "DISCONNECTED" ||
    det.rosConnectionState === "CONNECTING" ||
    det.rosConnectionState === "RECONNECTING" ||
    det.rosConnectionState === "ERROR"
  ) {
    return "DISCONNECTED";
  }
  if (!det.hasReceivedFrame) {
    return "AWAITING_MESSAGES";
  }
  if (det.isStale) {
    return "STALE";
  }
  if (
    det.frameClassification === "UNSUPPORTED_FRAME" ||
    (det.frame !== null && !KNOWN_SUPPORTED_FRAMES.has(det.frame))
  ) {
    return "UNSUPPORTED_FRAME";
  }
  if (det.objects.length === 0) {
    return "LIVE_EMPTY";
  }
  return "LIVE_OBJECTS";
}

function formatEpochStamp(stamp: number | null): string {
  if (stamp === null) {
    return "NONE";
  }
  return `${stamp.toFixed(3)}s`;
}

function formatIsoTimestamp(iso: string | null): string {
  if (!iso) {
    return "NONE";
  }
  return iso;
}

function renderObjectRow(
  obj: DetectionObjectPayload,
  frame: string,
  testIdPrefix: string,
  isMapProjected: boolean,
  trackedObj: TrackedObjectPayload | undefined,
  isSelected: boolean,
  onSelect: () => void,
): React.JSX.Element {
  const confidencePct = `${Math.round(obj.score * 100)}%`;
  const epistemicStatus = trackedObj?.epistemic_status ?? "OBSERVED";
  const uncertaintyM =
    trackedObj?.position_uncertainty_m ??
    Number(Math.max(0.05, 0.08 * obj.range_m).toFixed(2));
  const trackedId = trackedObj?.object_id ?? `OBJ-${String(obj.id + 1).padStart(3, "0")}`;

  return (
    <div
      key={`${frame}-${obj.id}-${obj.class}`}
      data-testid={`${testIdPrefix}-object-${obj.id}`}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      style={{
        padding: "6px 8px",
        background: isSelected ? "rgba(90, 138, 106, 0.14)" : "var(--sf-base)",
        border: isSelected
          ? "1px solid var(--status-nominal)"
          : "1px solid var(--border-subtle)",
        borderLeft: isMapProjected
          ? "2px solid var(--status-nominal)"
          : "2px solid var(--status-warning)",
        borderRadius: "2px",
        fontFamily: "var(--font-data)",
        fontSize: "11px",
        cursor: "pointer",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          fontWeight: 600,
          color: "var(--text-primary)",
        }}
      >
        <span>
          #{obj.id} {obj.class.toUpperCase()} [FRAME-LOCAL ID // {trackedId}]
        </span>
        <div style={{ display: "flex", gap: "4px", alignItems: "center" }}>
          <span
            className={`badge ${
              epistemicStatus === "CORROBORATED"
                ? "badge-nominal"
                : epistemicStatus === "CONFLICTED" || epistemicStatus === "STALE"
                  ? "badge-warning"
                  : ""
            }`}
            style={{ fontSize: "8px", padding: "1px 4px" }}
          >
            {epistemicStatus}
          </span>
          <span className="badge badge-nominal" style={{ fontSize: "9px", padding: "1px 5px" }}>
            CONF: {confidencePct}
          </span>
        </div>
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          color: "var(--text-secondary)",
          fontSize: "10px",
          marginTop: "3px",
        }}
      >
        <span>
          POS: X:{obj.position.x.toFixed(2)}m Y:{obj.position.y.toFixed(2)}m Z:
          {obj.position.z.toFixed(2)}m (±{uncertaintyM.toFixed(2)}m)
        </span>
        <span>RANGE: {obj.range_m.toFixed(2)}m</span>
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          color: "var(--text-muted)",
          fontSize: "9px",
          marginTop: "2px",
        }}
      >
        <span>FRAME: {frame}</span>
        <span>CLICK TO INSPECT EVIDENCE</span>
      </div>
    </div>
  );
}

export function PerceptionPanel({
  vehicleId = "SCOUT01",
}: PerceptionPanelProps): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const det = state.detections[vehicleId];
  const [selectedObjectIndex, setSelectedObjectIndex] = useState<number>(0);
  const [envQueryInput, setEnvQueryInput] = useState<string>("");
  const [envQueryResult, setEnvQueryResult] =
    useState<GroundedEnvironmentalResponsePayload | null>(null);
  const [isQueryingEnv, setIsQueryingEnv] = useState<boolean>(false);
  const [envQueryError, setEnvQueryError] = useState<string | null>(null);

  // Periodically check client-side detection freshness threshold so the UI
  // transitions to DETECTION STREAM STALE if frames stop arriving.
  useEffect(() => {
    if (!det.hasReceivedFrame || !det.receivedAt) {
      return;
    }
    const timer = setInterval(() => {
      const nowStale = isDetectionStreamTimestampStale(
        det.receivedAt,
        det.staleAfterSeconds,
      );
      if (nowStale !== det.isStale) {
        dispatch({
          type: "SET_DETECTIONS_STALE",
          payload: { vehicleId, isStale: nowStale },
        });
      }
    }, 250);
    return () => clearInterval(timer);
  }, [
    det.hasReceivedFrame,
    det.receivedAt,
    det.staleAfterSeconds,
    det.isStale,
    vehicleId,
    dispatch,
  ]);

  const streamStatus = derivePerceptionStreamStatus(det);
  const isLiveRosSource =
    det.telemetrySource === "rosbridge" ||
    det.rosConnectionState === "CONNECTED" ||
    det.hasReceivedFrame;

  const connBadgeClass =
    det.rosConnectionState === "CONNECTED" && !det.isStale
      ? "badge badge-nominal"
      : det.rosConnectionState === "DISABLED"
        ? "badge"
        : "badge badge-warning";

  const freshnessLabel = !det.hasReceivedFrame
    ? "STANDBY"
    : det.isStale
      ? "STALE"
      : "FRESH (LIVE)";

  const vehicleIdentityLabel =
    det.configuredVehicleId ??
    (det.telemetrySource === "rosbridge" ? det.vehicleId : "UNCONFIRMED");

  const camHealth = det.sensorHealth["depth_camera"];
  const lidarHealth = det.sensorHealth["lidar"];
  const trackedList = det.trackedObjects ?? [];
  const safeSelectedIndex =
    trackedList.length > 0
      ? Math.min(selectedObjectIndex, trackedList.length - 1)
      : 0;
  const selectedTracked: TrackedObjectPayload | undefined =
    trackedList[safeSelectedIndex];

  const handleEnvQuerySubmit = async (
    e: React.FormEvent<HTMLFormElement>,
  ): Promise<void> => {
    e.preventDefault();
    const trimmed = envQueryInput.trim();
    if (!trimmed) {
      return;
    }
    setIsQueryingEnv(true);
    setEnvQueryError(null);
    try {
      const result = await queryEnvironmentalIntelligence(trimmed);
      setEnvQueryResult(result);
    } catch (err) {
      setEnvQueryError(
        err instanceof Error ? err.message : "Environmental query failed.",
      );
    } finally {
      setIsQueryingEnv(false);
    }
  };

  return (
    <section
      className="panel"
      aria-label={`Perception Detections ${vehicleId}`}
      data-testid={`perception-panel-${vehicleId}`}
    >
      <div className="panel-header">
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <h3 className="panel-title">ROS PERCEPTION // {vehicleId}</h3>
          <span className="panel-meta">{det.topic}</span>
        </div>
        <span className={connBadgeClass} style={{ fontSize: "9px", padding: "1px 6px" }}>
          ROS: {det.rosConnectionState}
        </span>
      </div>

      <div className="panel-body">
        {/* Separation of Telemetry Source vs Physical Actuation Lockout */}
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "4px",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <span
            className={`badge ${isLiveRosSource ? "badge-nominal" : ""}`}
            style={{ fontSize: "9px", padding: "2px 6px" }}
            data-testid="perception-source-badge"
          >
            {isLiveRosSource ? "LIVE ROS PERCEPTION" : "MOCK PERCEPTION STANDBY"}
          </span>
          <span
            className="badge badge-warning"
            style={{ fontSize: "9px", padding: "2px 6px" }}
            data-testid="perception-actuation-badge"
          >
            {det.actuationLocked || state.hardwareLockout
              ? "PHYSICAL ACTUATION LOCKED"
              : "ACTUATION PERMITTED"}
          </span>
        </div>

        {/* Multi-Sensor Status Overview (Depth Camera + LiDAR Readiness) */}
        <div
          className="data-grid"
          data-testid="sensor-status-strip"
          style={{ marginTop: "4px" }}
        >
          <div className="data-cell">
            <div className="data-label">DEPTH CAMERA (/detections_json)</div>
            <div
              className="data-value"
              style={{ fontSize: "10px" }}
              data-testid="sensor-status-depth-camera"
            >
              {camHealth ? `${camHealth.status} // ${camHealth.source_mode.toUpperCase()}` : "DISCONNECTED"}
              {camHealth && camHealth.message_frequency_hz > 0
                ? ` (${camHealth.message_frequency_hz.toFixed(1)}Hz)`
                : ""}
            </div>
          </div>
          <div className="data-cell">
            <div className="data-label">LIDAR SENSOR STATUS</div>
            <div
              className="data-value"
              style={{
                fontSize: "10px",
                color:
                  lidarHealth?.status === "CONNECTED" || lidarHealth?.status === "SIMULATED"
                    ? "var(--status-nominal)"
                    : "var(--status-warning)",
              }}
              data-testid="sensor-status-lidar"
            >
              {lidarHealth ? lidarHealth.status : "UNAVAILABLE"} (CORROBORATION:{" "}
              {lidarHealth?.status === "CONNECTED" || lidarHealth?.status === "SIMULATED"
                ? "READY"
                : "UNAVAILABLE"}
              )
            </div>
          </div>
        </div>

        {/* Metadata Readout: Vehicle Identity, Freshness, Coordinate Frame, Source & Receive Timestamps */}
        <div className="data-grid">
          <div className="data-cell">
            <div className="data-label">VEHICLE ID</div>
            <div className="data-value" data-testid="perception-vehicle-id-value">
              {vehicleIdentityLabel}
            </div>
          </div>
          <div className="data-cell">
            <div className="data-label">FRESHNESS</div>
            <div className="data-value" data-testid="perception-freshness-value">
              {freshnessLabel}
            </div>
          </div>
          <div className="data-cell">
            <div className="data-label">COORD FRAME</div>
            <div className="data-value" data-testid="perception-frame-value">
              {det.frame ? det.frame : "NONE"}
            </div>
          </div>
          <div className="data-cell">
            <div className="data-label">SOURCE STAMP</div>
            <div className="data-value" style={{ fontSize: "11px" }} data-testid="perception-stamp-value">
              {formatEpochStamp(det.stamp)}
            </div>
          </div>
          <div className="data-cell" style={{ gridColumn: "1 / -1" }}>
            <div className="data-label">RECEIVED AT (LOCAL UTC)</div>
            <div
              className="data-value"
              style={{ fontSize: "10px" }}
              data-testid="perception-received-at-value"
            >
              {formatIsoTimestamp(det.receivedAt)}
            </div>
          </div>
        </div>

        {/* 6 Distinct Operational Stream States (Section 8) */}
        {streamStatus === "DISABLED" && (
          <div
            className="c2-alert-box"
            role="status"
            data-testid="perception-state-disabled"
          >
            ROSBRIDGE DISABLED // Set SHADOW_ROS_ENABLED=true to subscribe to{" "}
            {det.topic}.
          </div>
        )}

        {streamStatus === "DISCONNECTED" && (
          <div
            className="c2-alert-box c2-alert-error"
            role="status"
            data-testid="perception-state-disconnected"
          >
            ROSBRIDGE DISCONNECTED ({det.rosConnectionState}) // No live rover
            detection link.
          </div>
        )}

        {streamStatus === "AWAITING_MESSAGES" && (
          <div
            className="c2-alert-box c2-alert-info"
            role="status"
            data-testid="perception-state-awaiting"
          >
            ROSBRIDGE CONNECTED // AWAITING FIRST DETECTION MESSAGE ON{" "}
            {det.topic}
          </div>
        )}

        {streamStatus === "STALE" && (
          <div
            className="c2-alert-box c2-alert-warn"
            role="status"
            data-testid="perception-state-stale"
          >
            DETECTION STREAM STALE (&gt; {det.staleAfterSeconds.toFixed(1)}s) //
            Last frame at {formatEpochStamp(det.stamp)} is no longer current.
          </div>
        )}

        {streamStatus === "UNSUPPORTED_FRAME" && (
          <div
            className="c2-alert-box c2-alert-warn"
            role="status"
            data-testid="perception-state-unsupported-frame"
          >
            UNSUPPORTED COORDINATE FRAME [{det.frame}] // Unrecognized frame is
            preserved verbatim and blocked from global floor-map projection.
            {det.objects.length > 0 && (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: "4px",
                  marginTop: "6px",
                }}
              >
                {det.objects.map((obj, idx) =>
                  renderObjectRow(
                    obj,
                    det.frame ?? "unknown",
                    "perception-unsupported",
                    false,
                    trackedList[idx],
                    selectedObjectIndex === idx,
                    () => setSelectedObjectIndex(idx),
                  ),
                )}
              </div>
            )}
          </div>
        )}

        {streamStatus === "LIVE_EMPTY" && (
          <div
            className="c2-alert-box c2-alert-info"
            role="status"
            data-testid="perception-state-empty"
          >
            LIVE DETECTION STREAM // 0 OBJECTS DETECTED IN CURRENT FRAME (
            {det.frame})
          </div>
        )}

        {/* Active Detections Inspector Separated by Coordinate Frame */}
        {streamStatus === "LIVE_OBJECTS" &&
          det.frame === "oak_rgb_camera_optical_frame" && (
            <div
              data-testid="perception-camera-optical-frame-list"
              style={{ display: "flex", flexDirection: "column", gap: "4px" }}
            >
              <div
                style={{
                  fontFamily: "var(--font-data)",
                  fontSize: "10px",
                  color: "var(--status-warning)",
                  fontWeight: 600,
                }}
              >
                CAMERA-RELATIVE DETECTIONS [oak_rgb_camera_optical_frame] (
                {det.objects.length}) — NOT PLOTTED ON GLOBAL FLOOR MAP
              </div>
              {det.objects.map((obj, idx) =>
                renderObjectRow(
                  obj,
                  "oak_rgb_camera_optical_frame",
                  "perception-camera-optical",
                  false,
                  trackedList[idx],
                  selectedObjectIndex === idx,
                  () => setSelectedObjectIndex(idx),
                ),
              )}
            </div>
          )}

        {streamStatus === "LIVE_OBJECTS" &&
          det.frame === "oak_parent_frame" && (
            <div
              data-testid="perception-camera-frame-list"
              style={{ display: "flex", flexDirection: "column", gap: "4px" }}
            >
              <div
                style={{
                  fontFamily: "var(--font-data)",
                  fontSize: "10px",
                  color: "var(--status-warning)",
                  fontWeight: 600,
                }}
              >
                FRAME-RELATIVE DETECTIONS [oak_parent_frame] (
                {det.objects.length}) — NOT PLOTTED ON GLOBAL FLOOR MAP
              </div>
              {det.objects.map((obj, idx) =>
                renderObjectRow(
                  obj,
                  "oak_parent_frame",
                  "perception-camera",
                  false,
                  trackedList[idx],
                  selectedObjectIndex === idx,
                  () => setSelectedObjectIndex(idx),
                ),
              )}
            </div>
          )}

        {streamStatus === "LIVE_OBJECTS" && det.frame === "map" && (
          <div
            data-testid="perception-map-frame-list"
            style={{ display: "flex", flexDirection: "column", gap: "4px" }}
          >
            <div
              style={{
                fontFamily: "var(--font-data)",
                fontSize: "10px",
                color: det.mapProjectionAllowed
                  ? "var(--status-nominal)"
                  : "var(--status-warning)",
                fontWeight: 600,
              }}
            >
              {det.mapProjectionAllowed
                ? `CONFIRMED MAP-FRAME DETECTIONS (${det.objects.length}) — PLOTTED ON TACTICAL FLOOR MAP`
                : `UNCONFIRMED MAP-FRAME DETECTIONS [map] (${det.objects.length}) — TF/EXTRINSICS UNVALIDATED (NOT PLOTTED ON FLOOR MAP)`}
            </div>
            {det.objects.map((obj, idx) =>
              renderObjectRow(
                obj,
                "map",
                "perception-map",
                det.mapProjectionAllowed,
                trackedList[idx],
                selectedObjectIndex === idx,
                () => setSelectedObjectIndex(idx),
              ),
            )}
          </div>
        )}

        {/* Evidence Inspector for Selected Tracked Object (§9) */}
        {selectedTracked && (
          <div
            data-testid="evidence-inspector"
            style={{
              marginTop: "6px",
              padding: "6px 8px",
              background: "var(--sf-base)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "2px",
              fontFamily: "var(--font-data)",
              fontSize: "10px",
            }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontWeight: 600,
                color: "var(--text-primary)",
                marginBottom: "4px",
              }}
            >
              <span>
                EVIDENCE INSPECTOR // {selectedTracked.object_id} (
                {selectedTracked.label.toUpperCase()})
              </span>
              <span data-testid="evidence-inspector-status">
                STATUS: {selectedTracked.epistemic_status}
              </span>
            </div>
            <div style={{ color: "var(--text-secondary)", lineHeight: 1.4 }}>
              <div>
                SOURCE OBSERVATIONS:{" "}
                {selectedTracked.contributing_observation_ids.join(", ")}
              </div>
              <div>
                SENSORS: {selectedTracked.contributing_sensor_types.join(", ")} (
                {selectedTracked.source_mode.toUpperCase()})
              </div>
              <div>
                CORROBORATION:{" "}
                {selectedTracked.corroborated_by_multiple_sensors
                  ? "CORROBORATED (CAMERA + LIDAR)"
                  : "SINGLE-SENSOR ONLY (LIDAR CORROBORATION UNAVAILABLE)"}
              </div>
              <div>
                UNCERTAINTY: ±{selectedTracked.position_uncertainty_m.toFixed(2)}m //
                TRACKING: {selectedTracked.tracking_status}
              </div>
              {selectedTracked.conflict_details && (
                <div style={{ color: "var(--status-critical)", marginTop: "2px" }}>
                  CONFLICT: {selectedTracked.conflict_details}
                </div>
              )}
            </div>
          </div>
        )}

        {/* ULTRON Evidence-Grounded Environmental Query Box (§6, §7, §9) */}
        <form
          onSubmit={(e) => void handleEnvQuerySubmit(e)}
          data-testid="ultron-env-query-box"
          style={{ marginTop: "6px" }}
        >
          <label
            htmlFor="ultron-env-query-input"
            className="data-label"
            style={{ display: "block", marginBottom: "3px" }}
          >
            QUERY ULTRON ENVIRONMENTAL INTELLIGENCE
          </label>
          <div style={{ display: "flex", gap: "4px" }}>
            <input
              id="ultron-env-query-input"
              type="text"
              className="input"
              placeholder="e.g. What objects are detected? Is the path clear?"
              value={envQueryInput}
              onChange={(e) => setEnvQueryInput(e.target.value)}
              disabled={isQueryingEnv}
              style={{ fontSize: "10px", padding: "3px 6px" }}
            />
            <button
              type="submit"
              className="btn btn-secondary"
              disabled={isQueryingEnv}
              style={{ fontSize: "9px", padding: "3px 8px", whiteSpace: "nowrap" }}
            >
              {isQueryingEnv ? "QUERYING..." : "ASK ULTRON"}
            </button>
          </div>
          {envQueryError && (
            <div className="c2-alert-box c2-alert-error" style={{ marginTop: "4px" }}>
              {envQueryError}
            </div>
          )}
          {envQueryResult && (
            <div
              data-testid="ultron-env-query-result"
              style={{
                marginTop: "4px",
                padding: "6px",
                background: "var(--sf-base)",
                border: "1px solid var(--border-subtle)",
                fontFamily: "var(--font-data)",
                fontSize: "10px",
              }}
            >
              <div style={{ fontWeight: 600, color: "var(--text-primary)" }}>
                ANSWER [{envQueryResult.confidence.toUpperCase()} //{" "}
                {envQueryResult.source_mode.toUpperCase()}]: {envQueryResult.answer}
              </div>
              {envQueryResult.unknowns.length > 0 && (
                <div style={{ color: "var(--status-warning)", marginTop: "3px" }}>
                  UNKNOWNS: {envQueryResult.unknowns.join(" | ")}
                </div>
              )}
              {envQueryResult.evidence.length > 0 && (
                <div style={{ color: "var(--text-muted)", marginTop: "2px" }}>
                  EVIDENCE IDS:{" "}
                  {envQueryResult.evidence.map((ev) => ev.observation_id).join(", ")}
                </div>
              )}
            </div>
          )}
        </form>
      </div>
    </section>
  );
}
