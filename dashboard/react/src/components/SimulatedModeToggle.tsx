import React, { useState } from "react";
import { setVehicleSimulatedMode, type VehicleId } from "../api/client";
import { useDashboardStore } from "../state/store";

export function SimulatedModeToggle(): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [busyVehicle, setBusyVehicle] = useState<VehicleId | null>(null);
  const [statusNotice, setStatusNotice] = useState<string | null>(null);
  const [errorNotice, setErrorNotice] = useState<string | null>(null);

  const handleModeChange = async (
    vehicleId: VehicleId,
    enableSimulated: boolean,
  ): Promise<void> => {
    setBusyVehicle(vehicleId);
    setStatusNotice(null);
    setErrorNotice(null);
    try {
      const ack = await setVehicleSimulatedMode(vehicleId, enableSimulated);
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

  return (
    <section className="c2-panel" aria-label="Simulated Mode Status">
      <div className="c2-panel-header">
        <h2 className="c2-panel-title">OPERATING MODE &amp; SAFETY LOCKOUT</h2>
        <span
          className={`c2-badge ${
            state.hardwareLockout ? "c2-badge-warning" : "c2-badge-critical"
          }`}
        >
          Hardware Lockout:{" "}
          {state.hardwareLockout ? "ACTIVE (Simulated Only)" : "INACTIVE"}
        </span>
      </div>

      <div className="c2-panel-body">
        <div
          style={{
            fontSize: "11px",
            color: "var(--text-secondary)",
            fontFamily: "var(--font-mono)",
          }}
        >
          Physical hardware control is locked until TRD v2.0 &amp; Hardware/IoT
          failsafes are verified.
        </div>

        {(["SCOUT01", "HAWK01"] as const).map((vid) => {
          const v = state.vehicles[vid];
          const isBusy = busyVehicle === vid;
          return (
            <div
              key={vid}
              className="c2-kv-item"
              style={{ alignItems: "center" }}
            >
              <div>
                <div className="c2-mono" style={{ fontWeight: 700 }}>
                  {vid}: {v.mode.toUpperCase()}
                </div>
                <div className="c2-kv-label">
                  BACKEND CONFIRMED MODE: {v.mode.toUpperCase()}
                </div>
              </div>
              <div style={{ display: "flex", gap: "4px" }}>
                <button
                  type="button"
                  className={`c2-btn ${
                    v.mode === "simulated" ? "c2-btn-active" : "c2-btn-secondary"
                  }`}
                  disabled={isBusy}
                  onClick={() => void handleModeChange(vid, true)}
                  aria-label={`Set ${vid} to Simulated Mode`}
                >
                  SIMULATED
                </button>
                <button
                  type="button"
                  className={`c2-btn ${
                    v.mode === "real" ? "c2-btn-active" : "c2-btn-secondary"
                  }`}
                  disabled={isBusy}
                  onClick={() => void handleModeChange(vid, false)}
                  aria-label={`Attempt Real Hardware Mode for ${vid}`}
                >
                  REAL (LOCKED)
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
