import React, { useState } from "react";
import {
  fetchVehicleObservations,
  fetchVehicleSensors,
  fetchVehicleStatus,
  type VehicleSensorsPayload,
} from "../api/client";
import {
  useDashboardStore,
  type VehicleSummaryState,
} from "../state/store";

interface VehicleStatusCardProps {
  vehicle: VehicleSummaryState;
}

export function formatTacticalTimestamp(iso: string | null): string {
  if (!iso) {
    return "NO HEARTBEAT";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toISOString().slice(11, 23) + "Z";
}

function getVehicleDiagnosticWarning(vehicle: VehicleSummaryState): string | null {
  if (vehicle.status === "EMERGENCY_STOPPED") {
    return "CRITICAL: VEHICLE EMERGENCY STOPPED";
  }
  if (vehicle.status === "FAULT") {
    return "CRITICAL: VEHICLE FAULT REPORTED";
  }
  if (vehicle.connection === "UNKNOWN" || vehicle.status === "UNKNOWN") {
    return "WARNING: HEARTBEAT TIMEOUT — STATE UNKNOWN";
  }
  if (vehicle.connection === "DISCONNECTED") {
    return "LINK OFFLINE — NO LIVE TELEMETRY";
  }
  if (vehicle.isStale) {
    return "WARNING: TELEMETRY STALE (> THRESHOLD)";
  }
  if (vehicle.battery < 20) {
    return `WARNING: LOW BATTERY (${vehicle.battery.toFixed(1)}%)`;
  }
  return null;
}

export function VehicleStatusCard({
  vehicle,
}: VehicleStatusCardProps): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [syncError, setSyncError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
  const [sensorSnapshot, setSensorSnapshot] =
    useState<VehicleSensorsPayload | null>(null);

  const isSelected = state.selectedVehicleId === vehicle.id;
  const assignedTask =
    state.missionTasks.find(
      (t) =>
        t.vehicle_id === vehicle.id &&
        (t.status === "IN_PROGRESS" || t.status === "ASSIGNED"),
    ) ?? null;

  const currentTaskLabel =
    vehicle.activeTaskId ??
    (assignedTask ? `${assignedTask.id} (${assignedTask.type})` : "NONE");

  const warningText = getVehicleDiagnosticWarning(vehicle);
  const telemetryUnavailable =
    vehicle.connection === "DISCONNECTED" && vehicle.lastHeartbeat === null;
  const telemetryUnverified =
    vehicle.connection === "UNKNOWN" ||
    vehicle.connection === "DISCONNECTED" ||
    vehicle.isStale;

  const connectionBadgeClass =
    vehicle.connection === "CONNECTED" && !vehicle.isStale
      ? "badge badge-nominal"
      : vehicle.connection === "UNKNOWN" || vehicle.isStale
        ? "badge badge-warning"
        : "badge badge-critical";

  const statusBadgeClass =
    vehicle.status === "EMERGENCY_STOPPED" || vehicle.status === "FAULT"
      ? "badge badge-critical"
      : vehicle.status === "UNKNOWN"
        ? "badge badge-warning"
        : "badge badge-nominal";

  const handleRefresh = async (): Promise<void> => {
    setIsRefreshing(true);
    setSyncError(null);
    try {
      const [statusPayload, sensorsPayload, obsPayload] = await Promise.all([
        fetchVehicleStatus(vehicle.id),
        fetchVehicleSensors(vehicle.id),
        fetchVehicleObservations(
          vehicle.id,
          state.activeMissionId ?? undefined,
        ),
      ]);
      dispatch({ type: "APPLY_VEHICLE_STATUS", payload: statusPayload });
      dispatch({ type: "SET_OBSERVATIONS", payload: obsPayload });
      setSensorSnapshot(sensorsPayload);
    } catch (err) {
      setSyncError(
        err instanceof Error ? err.message : "Failed to query vehicle status",
      );
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <article
      className="panel"
      style={{
        borderLeft: isSelected ? "3px solid var(--accent-primary)" : undefined,
      }}
      aria-label={`Vehicle ${vehicle.id}`}
    >
      <div className="panel-header">
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <h3 className="panel-title">{vehicle.id}</h3>
          <span className="badge" style={{ fontSize: "9px", padding: "1px 5px" }}>
            {vehicle.vehicleType === "ground" ? "ROVER" : "DRONE"}
          </span>
        </div>
        <div style={{ display: "flex", gap: "4px", alignItems: "center" }}>
          <span className="badge badge-warning" style={{ fontSize: "10px", padding: "1px 6px" }}>
            {vehicle.mode.toUpperCase()}
          </span>
          <span className={connectionBadgeClass} style={{ fontSize: "10px", padding: "1px 6px" }}>
            {vehicle.connection}
            {vehicle.isStale ? " (STALE)" : ""}
          </span>
        </div>
      </div>

      <div className="panel-body">
        {/* Status bar and actions */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "6px",
          }}
        >
          <span className={statusBadgeClass} style={{ fontSize: "10px" }}>
            STATUS: {vehicle.status}
          </span>
          <div style={{ display: "flex", gap: "4px" }}>
            <button
              type="button"
              className={`btn ${isSelected ? "btn-primary" : "btn-secondary"}`}
              style={{ padding: "3px 8px", fontSize: "10px" }}
              onClick={() =>
                dispatch({ type: "SELECT_VEHICLE", payload: vehicle.id })
              }
              aria-pressed={isSelected}
            >
              {isSelected ? "FOCUSED" : "FOCUS"}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              style={{ padding: "3px 8px", fontSize: "10px" }}
              onClick={() => void handleRefresh()}
              disabled={isRefreshing}
              aria-label={`Sync ${vehicle.id} telemetry`}
            >
              {isRefreshing ? "SYNC..." : "SYNC"}
            </button>
          </div>
        </div>

        {/* Battery Module Readout */}
        <div style={{ background: "var(--sf-base)", padding: "8px 10px", borderRadius: "3px", border: "1px solid var(--border-subtle)" }}>
          <div className="battery-large-container">
            <span className="data-label">BATTERY LEVEL</span>
            <span
              className="battery-large-readout"
              style={{
                color:
                  vehicle.battery < 20
                    ? "var(--status-critical-bright)"
                    : vehicle.battery <= 40
                      ? "var(--status-warning)"
                      : "var(--text-primary)",
              }}
            >
              {telemetryUnavailable
                ? "OFFLINE"
                : `${vehicle.battery.toFixed(1)}%${telemetryUnverified ? " [LAST KNOWN]" : ""}`}
            </span>
          </div>

          {/* 16-Segment Battery Display */}
          <div className="battery-segments" aria-label={`${vehicle.id} battery bar`}>
            {Array.from({ length: 16 }).map((_, idx) => {
              const activeCount = Math.round((vehicle.battery / 100) * 16);
              const isActive = idx < activeCount;
              const segClass = isActive
                ? vehicle.battery < 20
                  ? "critical"
                  : vehicle.battery <= 40
                    ? "warning"
                    : "filled"
                : "";
              return (
                <div
                  key={idx}
                  className={`battery-segment ${segClass}`}
                />
              );
            })}
          </div>
        </div>

        {/* Monospace Telemetry Grid */}
        <div className="data-grid">
          <div className="data-cell">
            <div className="data-label">POS X</div>
            <div className="data-value">
              {telemetryUnavailable ? "--" : `${vehicle.position.x.toFixed(2)}m`}
            </div>
          </div>
          <div className="data-cell">
            <div className="data-label">POS Y</div>
            <div className="data-value">
              {telemetryUnavailable ? "--" : `${vehicle.position.y.toFixed(2)}m`}
            </div>
          </div>
          <div className="data-cell">
            <div className="data-label">ALTITUDE</div>
            <div className="data-value data-value-accent">
              {telemetryUnavailable ? "--" : `${vehicle.position.alt.toFixed(2)}m`}
            </div>
          </div>
          <div className="data-cell">
            <div className="data-label">HEARTBEAT</div>
            <div className="data-value" style={{ fontSize: "11px" }}>
              {formatTacticalTimestamp(vehicle.lastHeartbeat)}
            </div>
          </div>
        </div>

        <div className="data-row">
          <span className="data-row-label">ACTIVE TASK</span>
          <span className="data-row-value">{currentTaskLabel}</span>
        </div>

        {warningText && (
          <div
            className={`c2-alert-box ${
              vehicle.status === "EMERGENCY_STOPPED" ||
              vehicle.status === "FAULT"
                ? "c2-alert-error"
                : "c2-alert-warn"
            }`}
            role="status"
          >
            {warningText}
          </div>
        )}

        {syncError && (
          <div className="c2-alert-box c2-alert-error" role="alert">
            {syncError}
          </div>
        )}
        {sensorSnapshot && (
          <details className="data-cell">
            <summary>
              SENSOR SNAPSHOT // SEQ {sensorSnapshot.sequence}
              {sensorSnapshot.is_stale ? " // STALE" : ""}
            </summary>
            <pre
              style={{
                maxHeight: "90px",
                overflow: "auto",
                whiteSpace: "pre-wrap",
                overflowWrap: "anywhere",
                fontSize: "9px",
                color: "var(--text-secondary)",
              }}
            >
              {JSON.stringify(sensorSnapshot.raw_sensors, null, 2)}
            </pre>
          </details>
        )}
      </div>
    </article>
  );
}
