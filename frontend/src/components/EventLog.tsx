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
    <section className="panel" aria-label="Event Log">
      <div className="panel-header">
        <h2 className="panel-title">
          EVENT LOG ({filteredEvents.length})
        </h2>
        <div style={{ display: "flex", gap: "4px", alignItems: "center" }}>
          <select
            aria-label="Filter Event Category"
            className="input"
            style={{ width: "auto", padding: "2px 6px", fontSize: "10px", height: "24px" }}
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
            className="btn btn-secondary"
            style={{ padding: "2px 6px", fontSize: "10px", height: "24px" }}
            onClick={() => void handleSyncEvents()}
            disabled={loading}
            aria-label="Refresh Event Log"
          >
            {loading ? "..." : "SYNC"}
          </button>
        </div>
      </div>

      <div className="panel-body">
        {errorMsg && (
          <div className="c2-alert-box c2-alert-error" role="alert">
            {errorMsg}
          </div>
        )}

        {filteredEvents.length === 0 ? (
          <div style={{ color: "var(--text-muted)", fontSize: "11px", padding: "8px 0" }}>
            No operational events recorded yet.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "3px" }} role="log" aria-label="Operational Event Entries">
            {filteredEvents.map((evt) => {
              const cat = evt.category.toUpperCase();
              const entryClass =
                cat === "SAFETY" || cat === "SECURITY"
                  ? "log-entry log-entry-critical"
                  : cat === "TELEMETRY"
                    ? "log-entry log-entry-warning"
                    : "log-entry log-entry-nominal";
              return (
                <div key={evt.id} className={entryClass}>
                  <div className="log-meta" style={{ display: "flex", justifyContent: "space-between" }}>
                    <span>
                      {formatTacticalTimestamp(evt.timestamp)} // [{cat}]
                    </span>
                    <span>
                      {evt.mission_id} // {evt.id}
                    </span>
                  </div>
                  <div className="log-text" style={{ fontSize: "11px", color: "var(--text-primary)" }}>
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
