import React, { useState } from "react";
import {
  triggerEmergencyStop,
  type EmergencyStopTarget,
} from "../api/client";
import { useDashboardStore } from "../state/store";

export function EmergencyStopButton(): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [armedTarget, setArmedTarget] = useState<EmergencyStopTarget | null>(
    null,
  );
  const [isDispatching, setIsDispatching] = useState<boolean>(false);
  const [requestError, setRequestError] = useState<string | null>(null);

  const handleArmTarget = (target: EmergencyStopTarget): void => {
    if (isDispatching) {
      return;
    }
    setArmedTarget(target);
    setRequestError(null);
  };

  const handleConfirmEmergencyStop = async (): Promise<void> => {
    if (!armedTarget || isDispatching) {
      return;
    }
    const targetToStop = armedTarget;
    setIsDispatching(true);
    setRequestError(null);

    try {
      const response = await triggerEmergencyStop(targetToStop);
      dispatch({ type: "SET_EMERGENCY_STOP_RESULT", payload: response });
      setArmedTarget(null);
    } catch (err) {
      setRequestError(
        err instanceof Error
          ? err.message
          : "Emergency stop HTTP request failed.",
      );
    } finally {
      setIsDispatching(false);
    }
  };

  const lastResult = state.lastEmergencyStopResult;

  return (
    <div className="c2-estop-container" aria-label="Emergency Stop Control">
      <div className="c2-estop-controls">
        <span
          className="c2-mono"
          style={{
            color: "#ff9494",
            fontWeight: 700,
            fontSize: "11px",
            letterSpacing: "0.06em",
          }}
        >
          FAST-PATH E-STOP:
        </span>

        {(["ALL", "SCOUT01", "HAWK01"] as const).map((target) => (
          <button
            key={target}
            type="button"
            className="c2-estop-btn"
            disabled={isDispatching}
            onClick={() => handleArmTarget(target)}
            aria-label={
              target === "ALL"
                ? "Emergency Stop All Vehicles"
                : `Emergency Stop ${target}`
            }
          >
            E-STOP {target}
          </button>
        ))}

        {armedTarget && (
          <div
            style={{ display: "flex", alignItems: "center", gap: "6px" }}
            role="group"
            aria-label="Confirm Emergency Stop"
          >
            <button
              type="button"
              className="c2-estop-btn"
              disabled={isDispatching}
              onClick={() => void handleConfirmEmergencyStop()}
              aria-label={`Confirm Emergency Stop ${armedTarget}`}
              style={{
                backgroundColor: "#ff1f1f",
                borderColor: "#ffffff",
              }}
            >
              {isDispatching
                ? "DISPATCHING..."
                : `CONFIRM E-STOP [${armedTarget}]`}
            </button>
            <button
              type="button"
              className="c2-btn c2-btn-secondary"
              disabled={isDispatching}
              onClick={() => setArmedTarget(null)}
              aria-label="Cancel Emergency Stop"
            >
              CANCEL
            </button>
          </div>
        )}
      </div>

      {requestError && (
        <div className="c2-alert-box c2-alert-error" role="alert">
          E-STOP REQUEST FAILED: {requestError}
        </div>
      )}

      {lastResult && (
        <div
          className={`c2-alert-box ${
            lastResult.overall_outcome === "ALL_CONFIRMED_STOPPED"
              ? "c2-alert-info"
              : "c2-alert-error"
          }`}
          role="status"
          data-testid="estop-outcome-summary"
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              flexWrap: "wrap",
              gap: "8px",
              fontWeight: 700,
            }}
          >
            <span>
              TARGET: {lastResult.target} // HTTP REQUEST ACCEPTED:{" "}
              {lastResult.request_accepted ? "YES" : "NO"} // OVERALL:{" "}
              {lastResult.overall_outcome}
            </span>
            <span>
              FAST PATH: {lastResult.dispatch_elapsed_ms.toFixed(2)}ms //{" "}
              {lastResult.real_hardware_estop_available
                ? "REAL HARDWARE"
                : "SIMULATED ONLY (REAL HARDWARE LOCKED)"}
            </span>
          </div>

          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "4px",
              marginTop: "6px",
            }}
          >
            {Object.values(lastResult.vehicle_outcomes).map((vo) => {
              if (!vo) {
                return null;
              }
              return (
                <div
                  key={vo.vehicle_id}
                  data-testid={`estop-vehicle-outcome-${vo.vehicle_id}`}
                  style={{
                    padding: "4px 6px",
                    backgroundColor: "var(--bg-recessed)",
                    border: "1px solid var(--border-subtle)",
                    borderRadius: "2px",
                  }}
                >
                  <div>
                    <strong>{vo.vehicle_id}</strong> [
                    {vo.is_simulated ? "SIMULATED" : "REAL"}] — DISPATCH:{" "}
                    {vo.dispatch_attempted
                      ? vo.dispatch_succeeded
                        ? "SUCCEEDED"
                        : "FAILED"
                      : "NOT ATTEMPTED"}{" "}
                    | VEHICLE ACK: {vo.vehicle_acknowledged ? "YES" : "NO"} |{" "}
                    <strong>
                      CONFIRMED STOPPED:{" "}
                      {vo.vehicle_confirmed_stopped ? "YES" : "NO (UNCONFIRMED)"}
                    </strong>{" "}
                    | STATUS: {vo.resulting_status}
                  </div>
                  <div style={{ color: "var(--text-secondary)", fontSize: "10px" }}>
                    {vo.reason}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
