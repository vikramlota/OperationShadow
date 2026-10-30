import React, { useState } from "react";
import { completeMission, createMission, fetchMission } from "../api/client";
import { useDashboardStore } from "../state/store";

export function MissionPanel(): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [objectiveInput, setObjectiveInput] = useState<string>("");
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [isCompleting, setIsCompleting] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const handleSubmit = async (
    event: React.FormEvent<HTMLFormElement>,
  ): Promise<void> => {
    event.preventDefault();
    setErrorMsg(null);
    setSuccessMsg(null);

    const trimmed = objectiveInput.trim();
    if (!trimmed) {
      setErrorMsg("Mission objective cannot be empty.");
      return;
    }

    setIsSubmitting(true);
    try {
      const created = await createMission(trimmed);
      const detail = await fetchMission(created.mission_id);
      dispatch({ type: "SET_MISSION_DETAIL", payload: detail });
      setObjectiveInput("");
      setSuccessMsg(
        `Mission ${detail.id} dispatched (${detail.tasks.length} tasks assigned).`,
      );
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Mission submission failed.",
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleCompleteMission = async (): Promise<void> => {
    if (!state.activeMissionId) {
      return;
    }
    setErrorMsg(null);
    setSuccessMsg(null);
    setIsCompleting(true);
    try {
      const completed = await completeMission(state.activeMissionId);
      dispatch({ type: "SET_MISSION_DETAIL", payload: completed });
      setSuccessMsg(`Mission ${completed.id} marked COMPLETED.`);
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Mission completion failed.",
      );
    } finally {
      setIsCompleting(false);
    }
  };

  return (
    <section className="c2-panel" aria-label="Mission Panel">
      <div className="c2-panel-header">
        <h2 className="c2-panel-title">MISSION COMMAND &amp; TASKING</h2>
        <span className="c2-panel-meta">
          {state.activeMissionId
            ? `${state.activeMissionId} // ${state.missionStatus}`
            : "NO ACTIVE MISSION"}
        </span>
      </div>

      <div className="c2-panel-body">
        <form onSubmit={(e) => void handleSubmit(e)} noValidate>
          <label
            htmlFor="mission-objective-input"
            className="c2-kv-label"
            style={{ display: "block", marginBottom: "4px" }}
          >
            MISSION OBJECTIVE
          </label>
          <div style={{ display: "flex", gap: "6px" }}>
            <input
              id="mission-objective-input"
              type="text"
              className="c2-input"
              placeholder="e.g. Recon Sector Alpha and locate thermal anomaly"
              value={objectiveInput}
              onChange={(e) => setObjectiveInput(e.target.value)}
              disabled={isSubmitting}
            />
            <button
              type="submit"
              className="c2-btn"
              disabled={isSubmitting}
              aria-label="Submit Mission"
            >
              {isSubmitting ? "DISPATCHING..." : "DISPATCH"}
            </button>
          </div>
        </form>

        {errorMsg && (
          <div className="c2-alert-box c2-alert-error" role="alert">
            {errorMsg}
          </div>
        )}

        {successMsg && (
          <div className="c2-alert-box c2-alert-info" role="status">
            {successMsg}
          </div>
        )}

        <div className="c2-kv-grid">
          <div className="c2-kv-item">
            <span className="c2-kv-label">MISSION ID</span>
            <span className="c2-kv-value">
              {state.activeMissionId ?? "NONE"}
            </span>
          </div>
          <div className="c2-kv-item">
            <span className="c2-kv-label">STATUS</span>
            <span className="c2-kv-value">{state.missionStatus}</span>
          </div>
        </div>

        {state.activeMissionId && state.missionStatus === "ACTIVE" && (
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button
              type="button"
              className="c2-btn"
              disabled={isCompleting}
              onClick={() => void handleCompleteMission()}
              aria-label="Complete Simulated Mission"
            >
              {isCompleting ? "COMPLETING..." : "COMPLETE SIMULATED MISSION"}
            </button>
          </div>
        )}

        {state.missionObjective && (
          <div className="c2-kv-item" style={{ flexDirection: "column", gap: "2px" }}>
            <span className="c2-kv-label">ACTIVE OBJECTIVE</span>
            <span style={{ color: "var(--text-primary)", fontSize: "12px" }}>
              {state.missionObjective}
            </span>
          </div>
        )}

        <div>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              marginBottom: "4px",
            }}
          >
            <span className="c2-kv-label">
              ASSIGNED TASKS ({state.missionTasks.length})
            </span>
            {state.ultronIsStub && (
              <span className="c2-badge c2-badge-warning">
                PLANNER: ULTRON STUB (SIMULATED)
              </span>
            )}
          </div>

          {state.missionTasks.length === 0 ? (
            <div className="c2-log-viewport" style={{ color: "var(--text-muted)" }}>
              No tasks assigned. Submit a mission objective to generate tasks.
            </div>
          ) : (
            <div className="c2-log-viewport" aria-label="Mission Tasks List">
              {state.missionTasks.map((task) => (
                <div key={task.id} className="c2-log-entry">
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontFamily: "var(--font-mono)",
                      fontSize: "11px",
                    }}
                  >
                    <strong>
                      {task.id} [P{task.priority}] → {task.vehicle_id ?? "UNASSIGNED"}
                    </strong>
                    <span>{task.status}</span>
                  </div>
                  <div
                    style={{
                      color: "var(--text-secondary)",
                      fontSize: "11px",
                      fontFamily: "var(--font-mono)",
                    }}
                  >
                    TYPE: {task.type}
                    {task.area ? ` // AREA: ${task.area}` : ""}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
