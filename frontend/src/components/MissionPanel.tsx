import React, { useState } from "react";
import {
  completeMission,
  createMission,
  executeMission,
  fetchMission,
  fetchMissionReport,
  MissionReportPayload,
} from "../api/client";
import { useDashboardStore } from "../state/store";

const HACKATHON_DEMO_OBJECTIVE =
  "Navigate to the destination, detect obstacles, avoid them and continue towards the destination.";

export function MissionPanel(): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [objectiveInput, setObjectiveInput] = useState<string>("");
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [isExecuting, setIsExecuting] = useState<boolean>(false);
  const [isCompleting, setIsCompleting] = useState<boolean>(false);
  const [awaitingApproval, setAwaitingApproval] = useState<boolean>(false);
  const [planRationale, setPlanRationale] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  // Phase 3 After-Action Mission Report state
  const [reportMissionIdInput, setReportMissionIdInput] = useState<string>("");
  const [missionReport, setMissionReport] = useState<MissionReportPayload | null>(null);
  const [isLoadingReport, setIsLoadingReport] = useState<boolean>(false);
  const [reportErrorMsg, setReportErrorMsg] = useState<string | null>(null);
  const [exportFormat, setExportFormat] = useState<"NONE" | "MARKDOWN" | "JSON">("NONE");

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
      setAwaitingApproval(true);
      setPlanRationale(created.rationale ?? null);
      setSuccessMsg(
        `Mission ${detail.id} dispatched (${detail.tasks.length} tasks assigned) — awaiting operator approval. Click START MISSION to execute on SCOUT01.`,
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
    setAwaitingApproval(false);
    setIsExecuting(true);
    try {
      const executed = await executeMission(state.activeMissionId);
      dispatch({ type: "SET_MISSION_DETAIL", payload: executed });
      if (executed.status === "FAILED" || executed.status === "ABORTED") {
        setErrorMsg(
          `Mission ${executed.id} halted on SCOUT01 (${executed.status}) — check safety/obstacle telemetry.`,
        );
      } else {
        setSuccessMsg(
          `Mission ${executed.id} executed on SCOUT01 and marked ${executed.status}.`,
        );
      }
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
    setAwaitingApproval(false);
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

  const handleLoadReport = async (overrideMissionId?: string): Promise<void> => {
    const targetMissionId = (
      overrideMissionId ??
      (reportMissionIdInput.trim() || state.activeMissionId || "")
    ).trim();
    if (!targetMissionId) {
      setReportErrorMsg("Enter or select a Mission ID to generate an After-Action Report.");
      return;
    }
    setReportErrorMsg(null);
    setIsLoadingReport(true);
    try {
      const report = await fetchMissionReport(targetMissionId);
      setMissionReport(report);
      setExportFormat("NONE");
    } catch (err) {
      setMissionReport(null);
      setReportErrorMsg(
        err instanceof Error ? err.message : "Failed to load After-Action Mission Report.",
      );
    } finally {
      setIsLoadingReport(false);
    }
  };

  const unresolvedCount = missionReport
    ? missionReport.unresolved_items.incomplete_tasks.length +
      missionReport.unresolved_items.missing_observations.length +
      missionReport.unresolved_items.stale_telemetry_or_observations.length +
      missionReport.unresolved_items.conflicting_evidence.length +
      missionReport.unresolved_items.unverified_sensor_signals.length +
      missionReport.unresolved_items.unknown_outcomes.length
    : 0;

  const hasAssignedTasksOnly =
    state.missionTasks.length > 0 &&
    state.missionTasks.every((t) => t.status === "ASSIGNED");

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
        <form
          onSubmit={(e) => void handleSubmit(e)}
          noValidate
          style={{ display: "flex", flexDirection: "column", gap: "6px" }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: "8px",
            }}
          >
            <label
              htmlFor="mission-objective-input"
              className="data-label"
              style={{ marginBottom: 0 }}
            >
              MISSION OBJECTIVE
            </label>
            <button
              type="button"
              className="btn btn-secondary"
              style={{ fontSize: "9px", padding: "3px 8px", whiteSpace: "nowrap" }}
              onClick={() => {
                setObjectiveInput(HACKATHON_DEMO_OBJECTIVE);
                setErrorMsg(null);
                setSuccessMsg(null);
              }}
            >
              LOAD HACKATHON DEMO TASK
            </button>
          </div>

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr auto",
              gap: "6px",
              alignItems: "stretch",
            }}
          >
            <input
              id="mission-objective-input"
              type="text"
              className="input"
              placeholder="e.g. Navigate to the destination, detect obstacles, avoid them..."
              value={objectiveInput}
              onChange={(e) => setObjectiveInput(e.target.value)}
              disabled={isSubmitting || isExecuting}
            />
            <button
              type="submit"
              className="btn btn-primary"
              disabled={isSubmitting || isExecuting}
              aria-label="Submit Mission"
              style={{ whiteSpace: "nowrap", padding: "6px 12px" }}
            >
              {isSubmitting ? "PLANNING..." : "DISPATCH"}
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
            <div
              className="data-value"
              style={{
                color:
                  state.missionStatus === "ACTIVE"
                    ? "var(--marker-scout)"
                    : state.missionStatus === "FAILED"
                      ? "#E04040"
                      : undefined,
              }}
            >
              {isExecuting
                ? "EXECUTING ON SCOUT01"
                : awaitingApproval && state.missionStatus === "ACTIVE" && hasAssignedTasksOnly
                  ? "AWAITING APPROVAL"
                  : state.missionStatus}
            </div>
          </div>
        </div>

        {state.activeMissionId && state.missionStatus === "ACTIVE" && (
          <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
            <div
              className="data-cell"
              role="note"
              aria-label="ULTRON planning and safety validation run on dispatch; simulated only"
              style={{
                fontFamily: "var(--font-data)",
                fontSize: "10px",
                color: "var(--text-secondary)",
                lineHeight: 1.4,
                borderLeft:
                  awaitingApproval && hasAssignedTasksOnly
                    ? "3px solid var(--marker-scout)"
                    : undefined,
              }}
            >
              {state.hardwareLockout
                ? "ULTRON PLANNING + SAFETY VALIDATION RUN ON DISPATCH // SIMULATED ONLY"
                : awaitingApproval && hasAssignedTasksOnly
                  ? "ULTRON PLAN READY // REVIEW TASKS BELOW & APPROVE TO START MISSION ON SCOUT01"
                  : "ULTRON PLANNING + SAFETY VALIDATION // SUPERVISED SERIAL DISPATCH ON SCOUT01"}
              {planRationale && (
                <div
                  data-testid="mission-plan-rationale"
                  style={{
                    marginTop: "4px",
                    color: "var(--text-primary)",
                    fontSize: "10px",
                  }}
                >
                  <strong>PLAN RATIONALE:</strong> {planRationale}
                </div>
              )}
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: state.hardwareLockout ? "1fr 1fr" : "1fr",
                gap: "6px",
              }}
            >
              <button
                type="button"
                className="btn btn-primary"
                style={{ fontSize: "10px", padding: "6px 8px", width: "100%" }}
                disabled={isExecuting || isCompleting}
                onClick={() => void handleExecuteMission()}
                aria-label="Execute Mission on SCOUT01"
              >
                {isExecuting
                  ? "EXECUTING MISSION ON SCOUT01..."
                  : "START MISSION (EXECUTE ON SCOUT01)"}
              </button>
              {state.hardwareLockout && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  style={{ fontSize: "10px", padding: "6px 8px", width: "100%" }}
                  disabled={isCompleting || isExecuting}
                  onClick={() => void handleCompleteMission()}
                  aria-label="Complete Simulated Mission"
                >
                  {isCompleting ? "COMPLETING..." : "COMPLETE SIMULATED"}
                </button>
              )}
            </div>
          </div>
        )}

        {state.activeMissionId && state.missionStatus === "COMPLETED" && (
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: "8px",
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
                fontSize: "10px",
                fontWeight: 600,
              }}
            >
              MISSION COMPLETED // READY FOR NEXT
            </span>
            <div style={{ display: "flex", gap: "4px" }}>
              <button
                type="button"
                className="btn btn-primary"
                style={{ fontSize: "10px", padding: "3px 8px", whiteSpace: "nowrap" }}
                onClick={() => void handleLoadReport(state.activeMissionId ?? undefined)}
                aria-label="View After-Action Report"
              >
                VIEW REPORT
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                style={{ fontSize: "10px", padding: "3px 8px", whiteSpace: "nowrap" }}
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
          </div>
        )}

        {state.missionObjective && (
          <div className="data-cell" style={{ background: "var(--sf-base)" }}>
            <div className="data-label">ACTIVE OBJECTIVE</div>
            <div
              style={{
                color: "var(--text-primary)",
                fontSize: "11px",
                marginTop: "2px",
                lineHeight: 1.4,
              }}
            >
              {state.missionObjective}
            </div>
          </div>
        )}

        <div
          style={{
            background: "var(--sf-base)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "3px",
            padding: "8px",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: "6px",
            }}
          >
            <span className="data-label" style={{ marginBottom: 0 }}>
              ASSIGNED TASKS ({state.missionTasks.length})
            </span>
            {state.ultronIsStub && (
              <span
                className="badge badge-warning"
                style={{ fontSize: "9px", padding: "1px 5px" }}
              >
                ULTRON STUB
              </span>
            )}
          </div>

          {state.missionTasks.length === 0 ? (
            <div
              style={{
                color: "var(--text-muted)",
                fontSize: "11px",
                padding: "4px 0",
              }}
            >
              No tasks assigned. Submit a mission objective to generate tasks.
            </div>
          ) : (
            <div
              style={{ display: "flex", flexDirection: "column", gap: "4px" }}
              aria-label="Mission Tasks List"
            >
              {state.missionTasks.map((task) => (
                <div key={task.id} className="task-entry task-active">
                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: "2px",
                      minWidth: 0,
                    }}
                  >
                    <span style={{ fontWeight: 600 }}>
                      {task.id} [P{task.priority}] → {task.vehicle_id ?? "UNASSIGNED"}
                    </span>
                    <span
                      style={{
                        color: "var(--text-secondary)",
                        fontSize: "10px",
                        overflowWrap: "anywhere",
                      }}
                    >
                      TYPE: {task.type}
                      {task.area ? ` // AREA: ${task.area}` : ""}
                    </span>
                  </div>
                  <span
                    className="task-status"
                    style={{
                      background:
                        task.status === "FAILED"
                          ? "rgba(224, 64, 64, 0.2)"
                          : task.status === "IN_PROGRESS" ||
                              task.status === "CANCELLED"
                            ? "rgba(217, 155, 38, 0.2)"
                            : "var(--status-nominal-muted)",
                      color:
                        task.status === "FAILED"
                          ? "#E04040"
                          : task.status === "IN_PROGRESS" ||
                              task.status === "CANCELLED"
                            ? "#E8B04B"
                            : "#7AAA8A",
                    }}
                  >
                    {task.status}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Phase 3 After-Action Mission Report Section */}
        <div
          data-testid="after-action-report-section"
          style={{
            background: "var(--sf-base)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "3px",
            padding: "8px",
            display: "flex",
            flexDirection: "column",
            gap: "6px",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: "6px",
              flexWrap: "wrap",
            }}
          >
            <span className="data-label" style={{ marginBottom: 0 }}>
              AFTER-ACTION MISSION REPORT
            </span>
            <div style={{ display: "flex", gap: "4px", alignItems: "center" }}>
              <input
                type="text"
                className="input"
                aria-label="Report Mission ID"
                placeholder={state.activeMissionId ?? "M001"}
                value={reportMissionIdInput}
                onChange={(e) => setReportMissionIdInput(e.target.value)}
                style={{ width: "86px", padding: "3px 6px", fontSize: "10px" }}
              />
              <button
                type="button"
                className="btn btn-secondary"
                style={{ fontSize: "9px", padding: "3px 8px", whiteSpace: "nowrap" }}
                disabled={isLoadingReport}
                onClick={() => void handleLoadReport()}
                aria-label="Load Mission Report"
              >
                {isLoadingReport ? "LOADING..." : "GENERATE REPORT"}
              </button>
            </div>
          </div>

          {reportErrorMsg && (
            <div
              className="c2-alert-box c2-alert-error"
              role="alert"
              data-testid="mission-report-error"
            >
              {reportErrorMsg}
            </div>
          )}

          {missionReport && (
            <div
              data-testid="mission-report-view"
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "6px",
                fontSize: "10px",
                fontFamily: "var(--font-data)",
              }}
            >
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  flexWrap: "wrap",
                  gap: "4px",
                }}
              >
                <span style={{ fontWeight: 700, color: "var(--text-primary)" }}>
                  REPORT // {missionReport.mission_id} ({missionReport.overview.final_status})
                </span>
                <span
                  data-testid="mission-report-exec-badge"
                  className="badge badge-warning"
                  style={{ fontSize: "9px", padding: "1px 5px" }}
                >
                  {missionReport.overview.is_simulated_execution
                    ? "SIMULATED EXECUTION ONLY"
                    : "PHYSICAL EXECUTION"}
                </span>
              </div>

              <div
                data-testid="mission-report-objective"
                style={{ color: "var(--text-secondary)", lineHeight: 1.35 }}
              >
                <strong>OBJECTIVE:</strong> {missionReport.overview.objective}
                <br />
                <strong>PROVENANCE:</strong> {missionReport.overview.execution_provenance}
              </div>

              <div
                data-testid="mission-report-task-stats"
                style={{
                  padding: "4px 6px",
                  background: "rgba(255, 255, 255, 0.02)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "2px",
                }}
              >
                <strong>TASK SUMMARY:</strong> Total {missionReport.task_summary.total_tasks} |{" "}
                Completed {missionReport.task_summary.completed_tasks} | Pending{" "}
                {missionReport.task_summary.pending_tasks} | Cancelled{" "}
                {missionReport.task_summary.cancelled_tasks} | Failed{" "}
                {missionReport.task_summary.failed_tasks}
              </div>

              <div
                data-testid="mission-report-env-findings"
                style={{
                  padding: "4px 6px",
                  background: "rgba(255, 255, 255, 0.02)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "2px",
                }}
              >
                <strong>ENVIRONMENTAL FINDINGS:</strong> Objects{" "}
                {missionReport.environmental_findings.detected_objects.length} | Hazards{" "}
                {missionReport.environmental_findings.reported_hazards.length} | Obstacles{" "}
                {missionReport.environmental_findings.obstacles.length} | Observations{" "}
                {missionReport.environmental_findings.sensor_observations.length}
                <div style={{ color: "var(--text-muted)", marginTop: "2px" }}>
                  {missionReport.environmental_findings.coverage_note}
                </div>
              </div>

              <div
                data-testid="mission-report-decisions"
                style={{
                  padding: "4px 6px",
                  background: "rgba(255, 255, 255, 0.02)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "2px",
                }}
              >
                <strong>ULTRON DECISIONS:</strong> Standing Orders{" "}
                {missionReport.decision_history.standing_orders_applied.length} | LLM Triage{" "}
                {missionReport.decision_history.llm_triage_outcomes.length} | Replans{" "}
                {missionReport.decision_history.replanning_operations.length} | Rejected{" "}
                {missionReport.decision_history.rejected_decisions.length}
              </div>

              <div
                data-testid="mission-report-safety"
                style={{
                  padding: "4px 6px",
                  background: "rgba(255, 255, 255, 0.02)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "2px",
                }}
              >
                <strong>SAFETY SUMMARY:</strong> E-Stops{" "}
                {missionReport.safety_summary.emergency_stop_events.length} | Validation
                Rejections {missionReport.safety_summary.safety_validation_failures.length} |
                Critical Faults {missionReport.safety_summary.critical_faults.length}
                <div style={{ color: "var(--text-muted)", marginTop: "2px" }}>
                  {missionReport.safety_summary.stop_dispatch_acknowledged_status}
                </div>
              </div>

              <div
                data-testid="mission-report-unresolved"
                style={{
                  padding: "4px 6px",
                  background: "rgba(255, 255, 255, 0.02)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "2px",
                }}
              >
                <strong>UNRESOLVED ITEMS ({unresolvedCount}):</strong>{" "}
                {unresolvedCount === 0
                  ? "No unresolved tasks, conflicts, or stale observations."
                  : [
                      ...missionReport.unresolved_items.incomplete_tasks,
                      ...missionReport.unresolved_items.missing_observations,
                      ...missionReport.unresolved_items.stale_telemetry_or_observations,
                      ...missionReport.unresolved_items.conflicting_evidence,
                      ...missionReport.unresolved_items.unverified_sensor_signals,
                      ...missionReport.unresolved_items.unknown_outcomes,
                    ].join(" ; ")}
              </div>

              <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                <button
                  type="button"
                  className="btn btn-secondary"
                  style={{ fontSize: "9px", padding: "3px 8px" }}
                  onClick={() =>
                    setExportFormat((prev) =>
                      prev === "MARKDOWN" ? "NONE" : "MARKDOWN",
                    )
                  }
                  aria-label="Export Report Markdown"
                >
                  {exportFormat === "MARKDOWN" ? "HIDE MARKDOWN" : "EXPORT MARKDOWN"}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary"
                  style={{ fontSize: "9px", padding: "3px 8px" }}
                  onClick={() =>
                    setExportFormat((prev) => (prev === "JSON" ? "NONE" : "JSON"))
                  }
                  aria-label="Export Report JSON"
                >
                  {exportFormat === "JSON" ? "HIDE JSON" : "EXPORT JSON"}
                </button>
              </div>

              {exportFormat !== "NONE" && (
                <pre
                  data-testid="mission-report-export-content"
                  style={{
                    margin: 0,
                    padding: "6px",
                    maxHeight: "180px",
                    overflow: "auto",
                    background: "rgba(0, 0, 0, 0.35)",
                    border: "1px solid var(--border-subtle)",
                    borderRadius: "2px",
                    fontSize: "9px",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                    color: "var(--text-primary)",
                  }}
                >
                  {exportFormat === "MARKDOWN"
                    ? missionReport.markdown_export
                    : JSON.stringify(missionReport, null, 2)}
                </pre>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
