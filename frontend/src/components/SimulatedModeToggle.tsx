import React, { useState } from "react";
import {
  fetchScoutStatus,
  setVehicleSimulatedMode,
  type ScoutStatusPayload,
  type VehicleId,
} from "../api/client";
import { useDashboardStore } from "../state/store";

export function SimulatedModeToggle(): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [busyVehicle, setBusyVehicle] = useState<VehicleId | null>(null);
  const [statusNotice, setStatusNotice] = useState<string | null>(null);
  const [errorNotice, setErrorNotice] = useState<string | null>(null);
  const [scoutStatus, setScoutStatus] = useState<ScoutStatusPayload | null>(null);
  const [isSyncingScout, setIsSyncingScout] = useState(false);
  const [scoutError, setScoutError] = useState<string | null>(null);

  const vehicleList: VehicleId[] = ["SCOUT01"];

  const handleSetMode = async (
    vehicleId: VehicleId,
    simulatedEnabled: boolean,
  ): Promise<void> => {
    setBusyVehicle(vehicleId);
    setStatusNotice(null);
    setErrorNotice(null);
    try {
      const ack = await setVehicleSimulatedMode(vehicleId, simulatedEnabled);
      dispatch({
        type: "SET_VEHICLE_MODE",
        payload: { vehicleId: ack.vehicle_id, mode: ack.mode },
      });
      setStatusNotice(
        `${ack.vehicle_id} mode confirmed by backend: ${ack.mode.toUpperCase()}`,
      );
    } catch (err) {
      setErrorNotice(
        err instanceof Error
          ? err.message
          : `Failed to change mode for ${vehicleId}`,
      );
    } finally {
      setBusyVehicle(null);
    }
  };

  const handleSyncScoutStatus = async (): Promise<void> => {
    setIsSyncingScout(true);
    setScoutError(null);
    try {
      setScoutStatus(await fetchScoutStatus());
    } catch (err) {
      setScoutError(
        err instanceof Error ? err.message : "Failed to fetch SCOUT01 link status",
      );
    } finally {
      setIsSyncingScout(false);
    }
  };

  return (
    <section className="panel" aria-label="Simulated Mode Status">
      <div className="panel-header">
        <h2 className="panel-title">OPERATING MODE &amp; LOCKOUT</h2>
        <span
          className={`badge ${
            state.hardwareLockout ? "badge-warning" : "badge-nominal"
          }`}
          style={{ fontSize: "9px", padding: "1px 6px" }}
        >
          {state.hardwareLockout ? "LOCKED" : "UNLOCKED (REAL READY)"}
        </span>
      </div>

      <div className="panel-body">
        <div
          className="data-cell"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "4px",
            borderLeft: state.hardwareLockout
              ? "2px solid var(--status-warning)"
              : "2px solid var(--status-nominal)",
          }}
        >
          <div
            style={{
              fontFamily: "var(--font-data)",
              fontSize: "10px",
              fontWeight: 600,
              color: state.hardwareLockout ? "#D8A850" : "var(--status-nominal)",
            }}
          >
            Hardware Lockout:{" "}
            {state.hardwareLockout ? "ACTIVE (Simulated Only)" : "INACTIVE (Real Mode Active)"}
          </div>
          <div
            style={{
              fontSize: "10px",
              color: "var(--text-secondary)",
              fontFamily: "var(--font-data)",
              lineHeight: 1.4,
            }}
          >
            {state.hardwareLockout
              ? "Physical hardware control is locked until TRD v2.0 & Hardware/IoT failsafes are verified."
              : "Physical ESP-NOW serial control & WiFi ROS perception are ACTIVE on SCOUT01."}
          </div>
        </div>

        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "8px",
            padding: "6px 8px",
            background: "var(--sf-base)",
            borderRadius: "3px",
            border: "1px solid var(--border-subtle)",
          }}
        >
          <span className="panel-meta" role="status">
            {scoutStatus
              ? `BACKEND ROS: ${scoutStatus.mode.toUpperCase()} // ${
                  scoutStatus.connected ? "LINKED" : "NOT LINKED"
                }`
              : "ROS LINK STATUS: UNQUERIED"}
          </span>
          <button
            type="button"
            className="btn btn-secondary"
            style={{ padding: "4px 8px", fontSize: "10px", whiteSpace: "nowrap" }}
            disabled={isSyncingScout}
            onClick={() => void handleSyncScoutStatus()}
            aria-label="Sync SCOUT01 ROS status"
          >
            {isSyncingScout ? "SYNC..." : "SYNC ROS STATUS"}
          </button>
        </div>

        {scoutError && (
          <div className="c2-alert-box c2-alert-error" role="alert">
            {scoutError}
          </div>
        )}

        {vehicleList.map((vid) => {
          const v = state.vehicles[vid];
          const isBusy = busyVehicle === vid;
          return (
            <div
              key={vid}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "8px",
                padding: "8px",
                background: "var(--sf-base)",
                borderRadius: "3px",
                border: "1px solid var(--border-subtle)",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: "6px",
                }}
              >
                <span
                  style={{
                    fontFamily: "var(--font-data)",
                    fontWeight: 600,
                    fontSize: "11px",
                    color: "var(--text-primary)",
                  }}
                >
                  {vid}: {v.mode.toUpperCase()}
                </span>
                <span
                  style={{
                    fontFamily: "var(--font-data)",
                    fontSize: "9px",
                    color: "var(--text-muted)",
                  }}
                >
                  CONFIRMED: {v.mode.toUpperCase()}
                </span>
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "6px",
                }}
              >
                <button
                  type="button"
                  className={`btn ${
                    v.mode === "simulated" ? "btn-primary" : "btn-secondary"
                  }`}
                  style={{ padding: "5px 8px", fontSize: "10px", width: "100%" }}
                  disabled={isBusy}
                  onClick={() => void handleSetMode(vid, true)}
                  aria-label={`Set ${vid} to Simulated Mode`}
                >
                  SIMULATED
                </button>
                <button
                  type="button"
                  className={`btn ${
                    v.mode === "real" ? "btn-primary" : "btn-secondary"
                  }`}
                  style={{ padding: "5px 8px", fontSize: "10px", width: "100%" }}
                  disabled={isBusy || state.hardwareLockout}
                  onClick={() => void handleSetMode(vid, false)}
                  title={
                    state.hardwareLockout
                      ? "Real hardware mode is unavailable until external specifications and failsafes are verified."
                      : "Enable Real ESP-NOW Hardware Mode for SCOUT01"
                  }
                  aria-label={`Attempt Real Hardware Mode for ${vid}`}
                >
                  {state.hardwareLockout ? "REAL (LOCKED)" : "REAL (ESP-NOW)"}
                </button>
              </div>
            </div>
          );
        })}

        {statusNotice && (
          <div className="c2-alert-box c2-alert-info" role="status">
            {statusNotice}
          </div>
        )}

        {errorNotice && (
          <div className="c2-alert-box c2-alert-error" role="alert">
            {errorNotice}
          </div>
        )}
      </div>
    </section>
  );
}
