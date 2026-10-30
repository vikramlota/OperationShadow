import React from "react";
import { useDashboardStore } from "../state/store";
import { formatTacticalTimestamp } from "./VehicleStatusCard";

export function ReasoningTracePanel(): React.JSX.Element {
  const { state } = useDashboardStore();

  return (
    <section className="c2-panel" aria-label="Reasoning Trace Panel">
      <div className="c2-panel-header">
        <h2 className="c2-panel-title">
          ULTRON REASONING TRACE ({state.reasoningTraces.length})
        </h2>
        <span className="c2-badge c2-badge-warning">
          {state.ultronIsStub ? "SOURCE: SIMULATED STUB" : "SOURCE: ULTRON"}
        </span>
      </div>

      <div className="c2-panel-body">
        {state.reasoningTraces.length === 0 ? (
          <div className="c2-log-viewport" style={{ color: "var(--text-muted)" }}>
            No reasoning traces recorded. Submit a mission or await agent trigger.
          </div>
        ) : (
          <div className="c2-log-viewport" aria-label="Reasoning Trace Entries">
            {state.reasoningTraces.map((trace) => {
              const isStubRationale =
                state.ultronIsStub ||
                trace.rationale.toUpperCase().includes("ULTRON_STUB");
              return (
                <div key={trace.id} className="c2-log-entry">
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontFamily: "var(--font-mono)",
                      fontSize: "11px",
                      color: "var(--text-secondary)",
                    }}
                  >
                    <span>
                      [{formatTacticalTimestamp(trace.timestamp)}] {trace.id}
                    </span>
                    <span className="c2-badge">
                      {isStubRationale ? "SIMULATED STUB" : "AGENT"} //{" "}
                      {trace.mission_id}
                    </span>
                  </div>
                  <div
                    style={{
                      fontSize: "11px",
                      fontFamily: "var(--font-mono)",
                      color: "var(--accent-sage)",
                      marginTop: "3px",
                    }}
                  >
                    TRIGGER: {trace.trigger}
                  </div>
                  <div style={{ color: "var(--text-primary)", marginTop: "2px" }}>
                    {trace.rationale}
                  </div>
                  {trace.resulting_task_ids.length > 0 && (
                    <div
                      style={{
                        marginTop: "3px",
                        fontFamily: "var(--font-mono)",
                        fontSize: "11px",
                        color: "var(--text-secondary)",
                      }}
                    >
                      RESULTING TASKS: {trace.resulting_task_ids.join(", ")}
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
