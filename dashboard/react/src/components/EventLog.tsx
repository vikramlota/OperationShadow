import React, { useState } from "react";
import { fetchEvents } from "../api/client";
import { useDashboardStore } from "../state/store";
import { formatTacticalTimestamp } from "./VehicleStatusCard";

export function EventLog(): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [categoryFilter, setCategoryFilter] = useState<string>("ALL");
  const [loading, setLoading] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const categories = Array.from(
    new Set(["ALL", ...state.events.map((e) => e.category.toUpperCase())]),
  );

  const filteredEvents =
    categoryFilter === "ALL"
      ? state.events
      : state.events.filter(
          (e) => e.category.toUpperCase() === categoryFilter,
        );

  const handleSyncEvents = async (): Promise<void> => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const records = await fetchEvents(state.activeMissionId ?? undefined);
      dispatch({ type: "SET_EVENTS", payload: records });
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to fetch event log",
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="c2-panel" aria-label="Event Log">
      <div className="c2-panel-header">
        <h2 className="c2-panel-title">
          CHRONOLOGICAL EVENT LOG ({filteredEvents.length})
        </h2>
        <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
          <select
            aria-label="Filter Event Category"
            className="c2-select"
            style={{ width: "auto", padding: "2px 6px", fontSize: "11px" }}
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value)}
          >
            {categories.map((cat) => (
              <option key={cat} value={cat}>
                {cat}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="c2-btn c2-btn-secondary"
            onClick={() => void handleSyncEvents()}
            disabled={loading}
            aria-label="Refresh Event Log"
          >
            {loading ? "..." : "SYNC"}
          </button>
        </div>
      </div>

      <div className="c2-panel-body">
        {errorMsg && (
          <div className="c2-alert-box c2-alert-error" role="alert">
            {errorMsg}
          </div>
        )}

        {filteredEvents.length === 0 ? (
          <div className="c2-log-viewport" style={{ color: "var(--text-muted)" }}>
            No operational events recorded yet.
          </div>
        ) : (
          <div className="c2-log-viewport" role="log" aria-label="Operational Event Entries">
            {filteredEvents.map((evt) => {
              const cat = evt.category.toUpperCase();
              const entryClass =
                cat === "SAFETY" || cat === "SECURITY"
                  ? "c2-log-entry c2-log-entry-critical"
                  : cat === "TELEMETRY"
                    ? "c2-log-entry c2-log-entry-warning"
                    : "c2-log-entry";
              return (
                <div key={evt.id} className={entryClass}>
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
                      [{formatTacticalTimestamp(evt.timestamp)}] [{cat}]
                    </span>
                    <span>
                      {evt.mission_id} // {evt.id}
                    </span>
                  </div>
                  <div style={{ color: "var(--text-primary)", marginTop: "2px" }}>
                    {evt.message}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
