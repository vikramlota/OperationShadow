import React from "react";
import { useDashboardStore } from "../state/store";
import { formatTacticalTimestamp } from "./VehicleStatusCard";

export function ReasoningTracePanel(): React.JSX.Element {
  const { state } = useDashboardStore();

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
        {state.reasoningTraces.length === 0 ? (
          <div style={{ color: "var(--text-muted)", fontSize: "11px", padding: "8px 0" }}>
            No reasoning traces recorded.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "4px" }} aria-label="Reasoning Trace Entries">
            {state.reasoningTraces.map((trace) => {
              const isStubRationale =
                state.ultronIsStub ||
                trace.rationale.toUpperCase().includes("ULTRON_STUB");
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
