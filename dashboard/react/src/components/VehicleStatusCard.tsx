import React, { useState } from "react";
import {
  fetchVehicleObservations,
  fetchVehicleStatus,
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

function getBatteryColor(battery: number): string {
  if (battery < 20) {
    return "var(--status-critical-bright)";
  }
  if (battery <= 40) {
    return "var(--status-warning)";
  }
  return "var(--accent-military)";
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
      ? "c2-badge c2-badge-nominal"
      : vehicle.connection === "UNKNOWN" || vehicle.isStale
        ? "c2-badge c2-badge-warning"
        : "c2-badge c2-badge-critical";

  const statusBadgeClass =
    vehicle.status === "EMERGENCY_STOPPED" || vehicle.status === "FAULT"
      ? "c2-badge c2-badge-critical"
      : vehicle.status === "UNKNOWN"
        ? "c2-badge c2-badge-warning"
        : "c2-badge c2-badge-nominal";

  const handleRefresh = async (): Promise<void> => {
    setIsRefreshing(true);
    setSyncError(null);
    try {
      const [statusPayload, obsPayload] = await Promise.all([
        fetchVehicleStatus(vehicle.id),
        fetchVehicleObservations(
          vehicle.id,
          state.activeMissionId ?? undefined,
        ),
      ]);
      dispatch({ type: "APPLY_VEHICLE_STATUS", payload: statusPayload });
      dispatch({ type: "SET_OBSERVATIONS", payload: obsPayload });
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
      className={`c2-panel ${isSelected ? "c2-panel-selected" : ""}`}
      aria-label={`Vehicle ${vehicle.id}`}
    >
      <div className="c2-panel-header">
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <h3 className="c2-panel-title">{vehicle.id}</h3>
          <span className="c2-badge">
            {vehicle.vehicleType === "ground" ? "GROUND ROVER" : "AERIAL DRONE"}
          </span>
        </div>
        <div style={{ display: "flex", gap: "4px", alignItems: "center" }}>
          <span className="c2-badge c2-badge-warning">
            MODE: {vehicle.mode.toUpperCase()}
          </span>
          <span className={connectionBadgeClass}>
            LINK: {vehicle.connection}
            {vehicle.isStale ? " (STALE)" : ""}
          </span>
        </div>
      </div>

      <div className="c2-panel-body">
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "6px",
          }}
        >
          <span className={statusBadgeClass}>STATUS: {vehicle.status}</span>
          <div style={{ display: "flex", gap: "4px" }}>
            <button
              type="button"
              className="c2-btn c2-btn-secondary"
              onClick={() =>
                dispatch({ type: "SELECT_VEHICLE", payload: vehicle.id })
              }
              aria-pressed={isSelected}
            >
              {isSelected ? "SELECTED" : "FOCUS"}
            </button>
            <button
              type="button"
              className="c2-btn c2-btn-secondary"
              onClick={() => void handleRefresh()}
              disabled={isRefreshing}
              aria-label={`Sync ${vehicle.id} telemetry`}
            >
              {isRefreshing ? "SYNC..." : "SYNC"}
            </button>
          </div>
        </div>

        {/* Battery Readout & Segmented Bar */}
        <div>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontSize: "11px",
              marginBottom: "3px",
            }}
          >
            <span className="c2-kv-label">BATTERY LEVEL</span>
            <span className="c2-mono">
              {telemetryUnavailable
                ? "UNAVAILABLE (OFFLINE)"
                : `${vehicle.battery.toFixed(1)}%${telemetryUnverified ? " [LAST KNOWN]" : ""}`}
            </span>
          </div>
          <div className="c2-battery-track" aria-label={`${vehicle.id} battery bar`}>
            <div
              className="c2-battery-fill"
              style={{
                width: `${Math.max(0, Math.min(100, vehicle.battery))}%`,
                backgroundColor: getBatteryColor(vehicle.battery),
              }}
            />
          </div>
        </div>

        {/* Key-Value Monospace Telemetry Grid */}
        <div className="c2-kv-grid">
          <div className="c2-kv-item">
            <span className="c2-kv-label">POS X</span>
            <span className="c2-kv-value">
              {telemetryUnavailable
                ? "--"
                : `${vehicle.position.x.toFixed(2)}m`}
            </span>
          </div>
          <div className="c2-kv-item">
            <span className="c2-kv-label">POS Y</span>
            <span className="c2-kv-value">
              {telemetryUnavailable
                ? "--"
                : `${vehicle.position.y.toFixed(2)}m`}
            </span>
          </div>
          <div className="c2-kv-item">
            <span className="c2-kv-label">ALTITUDE</span>
            <span className="c2-kv-value">
              {telemetryUnavailable
                ? "--"
                : `${vehicle.position.alt.toFixed(2)}m`}
            </span>
          </div>
          <div className="c2-kv-item">
            <span className="c2-kv-label">HEARTBEAT</span>
            <span className="c2-kv-value">
              {formatTacticalTimestamp(vehicle.lastHeartbeat)}
            </span>
          </div>
        </div>

        <div className="c2-kv-item">
          <span className="c2-kv-label">ACTIVE TASK</span>
          <span className="c2-kv-value">{currentTaskLabel}</span>
        </div>

        {warningText && (
          <div
            className={`c2-alert-box ${
              vehicle.status === "EMERGENCY_STOPPED" ||
              vehicle.status === "FAULT"
                ? "c2-alert-error"
                : "c2-alert-warning"
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
      </div>
    </article>
  );
}
