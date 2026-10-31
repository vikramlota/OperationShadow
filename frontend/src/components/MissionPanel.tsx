import React, { useState } from "react";
import {
  completeMission,
  createMission,
  executeMission,
  fetchMission,
} from "../api/client";
import { useDashboardStore } from "../state/store";

const HACKATHON_DEMO_OBJECTIVE =
  "Simulated SCOUT01: perform one short sweep of Sector Alpha, identify visible obstacles, and report findings.";

export function MissionPanel(): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [objectiveInput, setObjectiveInput] = useState<string>("");
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [isExecuting, setIsExecuting] = useState<boolean>(false);
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

  const handleExecuteMission = async (): Promise<void> => {
    if (!state.activeMissionId) {
      return;
    }
    setErrorMsg(null);
    setSuccessMsg(null);
    setIsExecuting(true);
    try {
      const executed = await executeMission(state.activeMissionId);
      dispatch({ type: "SET_MISSION_DETAIL", payload: executed });
      setSuccessMsg(
        `Mission ${executed.id} executed on SCOUT01 and marked ${executed.status}.`,
      );
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Mission execution on SCOUT01 failed.",
      );
    } finally {
      setIsExecuting(false);
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
    <section className="panel" aria-label="Mission Panel">
      <div className="panel-header">
        <h2 className="panel-title">MISSION COMMAND &amp; TASKING</h2>
        <span className="panel-meta">
          {state.activeMissionId
            ? `${state.activeMissionId} // ${state.missionStatus}`
            : "NO ACTIVE MISSION"}
        </span>
      </div>

      <div className="panel-body">
        <form onSubmit={(e) => void handleSubmit(e)} noValidate>
          <label
            htmlFor="mission-objective-input"
            className="data-label"
            style={{ display: "block", marginBottom: "4px" }}
          >
            MISSION OBJECTIVE
          </label>
          <button
            type="button"
            className="btn btn-secondary"
            style={{ fontSize: "9px", padding: "3px 7px", marginBottom: "5px" }}
            onClick={() => {
              setObjectiveInput(HACKATHON_DEMO_OBJECTIVE);
              setErrorMsg(null);
              setSuccessMsg(null);
            }}
          >
            LOAD HACKATHON DEMO TASK
          </button>
          <div style={{ display: "flex", gap: "4px" }}>
            <input
              id="mission-objective-input"
              type="text"
              className="input"
              placeholder="e.g. Inspect building, find red_package, and report hazards"
              value={objectiveInput}
              onChange={(e) => setObjectiveInput(e.target.value)}
              disabled={isSubmitting}
            />
            <button
              type="submit"
              className="btn btn-primary"
              disabled={isSubmitting}
              aria-label="Submit Mission"
              style={{ whiteSpace: "nowrap", padding: "5px 12px" }}
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

        <div className="data-grid">
          <div className="data-cell">
            <div className="data-label">MISSION ID</div>
            <div className="data-value">
              {state.activeMissionId ?? "NONE"}
            </div>
          </div>
          <div className="data-cell">
            <div className="data-label">STATUS</div>
            <div className="data-value" style={{ color: state.missionStatus === "ACTIVE" ? "var(--marker-scout)" : undefined }}>
              {state.missionStatus}
            </div>
          </div>
        </div>

        {state.activeMissionId && state.missionStatus === "ACTIVE" && (
          <>
            <div
              className="panel-meta"
              role="note"
              aria-label="ULTRON planning and safety validation run on dispatch; simulated only"
            >
              {state.hardwareLockout
                ? "ULTRON PLANNING + SAFETY VALIDATION RUN ON DISPATCH // SIMULATED ONLY"
                : "ULTRON PLANNING + SAFETY VALIDATION // LIVE SERIAL EXECUTION ON SCOUT01"}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "6px", flexWrap: "wrap" }}>
              <button
                type="button"
                className="btn btn-primary"
                style={{ fontSize: "10px", padding: "4px 8px" }}
                disabled={isExecuting || isCompleting}
                onClick={() => void handleExecuteMission()}
                aria-label="Execute Mission on SCOUT01"
              >
                {isExecuting ? "EXECUTING ON ROVER..." : "EXECUTE ON SCOUT01"}
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                style={{ fontSize: "10px", padding: "4px 8px" }}
                disabled={isCompleting || isExecuting}
                onClick={() => void handleCompleteMission()}
                aria-label="Complete Simulated Mission"
              >
                {isCompleting ? "COMPLETING..." : "COMPLETE SIMULATED MISSION"}
              </button>
            </div>
          </>
        )}

        {state.activeMissionId && state.missionStatus === "COMPLETED" && (
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              padding: "6px 10px",
              background: "var(--sf-base)",
              borderRadius: "3px",
              border: "1px solid var(--border-subtle)",
              borderLeft: "3px solid var(--status-nominal)",
            }}
          >
            <span
              style={{
                color: "var(--status-nominal)",
                fontFamily: "var(--font-data)",
                fontSize: "11px",
                fontWeight: 600,
              }}
            >
              MISSION COMPLETED // READY FOR NEXT OBJECTIVE
            </span>
            <button
              type="button"
              className="btn btn-secondary"
              style={{ fontSize: "10px", padding: "3px 8px" }}
              onClick={() => {
                const el = document.getElementById("mission-objective-input");
                if (el) {
                  el.focus();
                }
              }}
            >
              + NEW OBJECTIVE
            </button>
          </div>
        )}

        {state.missionObjective && (
          <div className="data-cell" style={{ background: "var(--sf-base)" }}>
            <div className="data-label">ACTIVE OBJECTIVE</div>
            <div style={{ color: "var(--text-primary)", fontSize: "11px", marginTop: "2px" }}>
              {state.missionObjective}
            </div>
          </div>
        )}

        <div>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "4px",
            }}
          >
            <span className="data-label">
              ASSIGNED TASKS ({state.missionTasks.length})
            </span>
            {state.ultronIsStub && (
              <span className="badge badge-warning" style={{ fontSize: "9px", padding: "1px 5px" }}>
                ULTRON STUB
              </span>
            )}
          </div>

          {state.missionTasks.length === 0 ? (
            <div style={{ color: "var(--text-muted)", fontSize: "11px", padding: "8px 0" }}>
              No tasks assigned. Submit a mission objective to generate tasks.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: "3px" }} aria-label="Mission Tasks List">
              {state.missionTasks.map((task) => (
                <div key={task.id} className="task-entry task-active">
                  <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
                    <span style={{ fontWeight: 600 }}>
                      {task.id} [P{task.priority}] → {task.vehicle_id ?? "UNASSIGNED"}
                    </span>
                    <span style={{ color: "var(--text-secondary)", fontSize: "10px" }}>
                      TYPE: {task.type}
                      {task.area ? ` // AREA: ${task.area}` : ""}
                    </span>
                  </div>
                  <span
                    className="task-status"
                    style={{
                      background:
                        task.status === "IN_PROGRESS"
                          ? "rgba(217, 155, 38, 0.2)"
                          : "var(--status-nominal-muted)",
                      color: task.status === "IN_PROGRESS" ? "#E8B04B" : "#7AAA8A",
                    }}
                  >
                    {task.status}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
