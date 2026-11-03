import React from "react";
import { useDashboardStore } from "../state/store";
import { formatTacticalTimestamp } from "./VehicleStatusCard";

export function ReasoningTracePanel(): React.JSX.Element {
  const { state } = useDashboardStore();
  const decisionTraces = state.detections.SCOUT01.decisionTraces ?? [];
  const freeSpace = state.detections.SCOUT01.freeSpace;

  return (
    <section className="panel" aria-label="Reasoning Trace Panel">
      <div className="panel-header">
        <h2 className="panel-title">
          REASONING TRACE ({state.reasoningTraces.length})
        </h2>
        <span className="badge badge-warning" style={{ fontSize: "9px", padding: "1px 5px" }}>
          {state.ultronIsStub ? "STUB" : "ULTRON"}
        </span>
      </div>

      <div className="panel-body">
        {freeSpace && (
          <div
            data-testid="decision-trace-free-space-banner"
            style={{
              padding: "4px 6px",
              marginBottom: "4px",
              background: "var(--sf-base)",
              border: "1px solid var(--border-subtle)",
              fontFamily: "var(--font-data)",
              fontSize: "9px",
              color: freeSpace.is_verified_clear
                ? "var(--status-nominal)"
                : "var(--status-warning)",
            }}
          >
            FREE-SPACE STATUS [{freeSpace.epistemic_status}]: {freeSpace.reason}
          </div>
        )}

        {state.reasoningTraces.length === 0 && decisionTraces.length === 0 ? (
          <div style={{ color: "var(--text-muted)", fontSize: "11px", padding: "8px 0" }}>
            No reasoning traces recorded.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "4px" }} aria-label="Reasoning Trace Entries">
            {state.reasoningTraces.map((trace, idx) => {
              const isStubRationale =
                state.ultronIsStub ||
                trace.rationale.toUpperCase().includes("ULTRON_STUB");
              const missionDecisionTraces = decisionTraces.filter(
                (dt) => dt.mission_id === trace.mission_id,
              );
              const traceIdxInMission = state.reasoningTraces
                .slice(0, idx)
                .filter((t) => t.mission_id === trace.mission_id).length;
              const matchingDt =
                missionDecisionTraces[traceIdxInMission] ??
                missionDecisionTraces[missionDecisionTraces.length - 1];
              return (
                <div key={trace.id} className="log-entry log-entry-nominal">
                  <div className="log-meta" style={{ display: "flex", justifyContent: "space-between" }}>
                    <span>
                      {formatTacticalTimestamp(trace.timestamp)} // {trace.id}
                    </span>
                    <span style={{ color: "var(--marker-scout)" }}>
                      {isStubRationale ? "STUB" : "AGENT"} // {trace.mission_id}
                    </span>
                  </div>
                  <div
                    style={{
                      fontSize: "10px",
                      fontFamily: "var(--font-data)",
                      color: "var(--marker-scout)",
                      margin: "2px 0",
                    }}
                  >
                    TRIGGER: {trace.trigger}
                  </div>
                  <div className="log-text" style={{ fontSize: "11px", color: "var(--text-primary)" }}>
                    {trace.rationale}
                  </div>
                  {matchingDt && (
                    <div
                      data-testid={`decision-trace-evidence-${trace.id}`}
                      style={{
                        marginTop: "3px",
                        fontFamily: "var(--font-data)",
                        fontSize: "9px",
                        color: "var(--text-secondary)",
                      }}
                    >
                      <div>
                        SAFETY VALIDATOR:{" "}
                        {matchingDt.safety_validator_passed ? "PASSED" : "REJECTED"}
                        {matchingDt.replanning_triggered ? " // REPLAN TRIGGERED" : ""}
                      </div>
                      {matchingDt.environmental_facts_used.length > 0 && (
                        <div>
                          FACTS: {matchingDt.environmental_facts_used.join("; ")}
                        </div>
                      )}
                      {matchingDt.unknowns_and_uncertainties.length > 0 && (
                        <div style={{ color: "var(--status-warning)" }}>
                          UNKNOWNS: {matchingDt.unknowns_and_uncertainties.join("; ")}
                        </div>
                      )}
                    </div>
                  )}
                  {trace.resulting_task_ids.length > 0 && (
                    <div
                      style={{
                        marginTop: "3px",
                        fontFamily: "var(--font-data)",
                        fontSize: "10px",
                        color: "var(--text-muted)",
                      }}
                    >
                      TASKS: {trace.resulting_task_ids.join(", ")}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
