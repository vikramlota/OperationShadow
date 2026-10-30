import React, { useEffect, useState } from "react";
import {
  fetchMissionReplay,
  type MissionReplayTimelineEntryPayload,
} from "../api/client";
import { useDashboardStore } from "../state/store";
import { formatTacticalTimestamp } from "./VehicleStatusCard";

export function MissionReplayScrubber(): React.JSX.Element {
  const { state } = useDashboardStore();
  const [missionIdInput, setMissionIdInput] = useState<string>(
    state.activeMissionId ?? "",
  );
  const [loadedMissionId, setLoadedMissionId] = useState<string | null>(null);
  const [entries, setEntries] = useState<MissionReplayTimelineEntryPayload[]>(
    [],
  );
  const [selectedIndex, setSelectedIndex] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (state.activeMissionId && !missionIdInput) {
      setMissionIdInput(state.activeMissionId);
    }
  }, [state.activeMissionId, missionIdInput]);

  const handleLoadReplay = async (
    event?: React.FormEvent<HTMLFormElement>,
  ): Promise<void> => {
    if (event) {
      event.preventDefault();
    }
    const targetId = (missionIdInput || state.activeMissionId || "").trim();
    if (!targetId) {
      setErrorMsg("Enter or select a mission ID to load replay timeline.");
      return;
    }

    setLoading(true);
    setErrorMsg(null);
    try {
      const response = await fetchMissionReplay(targetId);
      setLoadedMissionId(response.mission_id);
      setEntries(response.entries);
      setSelectedIndex(0);
    } catch (err) {
      setEntries([]);
      setLoadedMissionId(null);
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to load mission replay.",
      );
    } finally {
      setLoading(false);
    }
  };

  const selectedEntry = entries[selectedIndex] ?? null;

  return (
    <section className="panel" aria-label="Mission Replay Scrubber">
      <div className="panel-header">
        <h2 className="panel-title">
          MISSION REPLAY (READ-ONLY)
        </h2>
        <span className="panel-meta">
          {loadedMissionId
            ? `${loadedMissionId} // ${entries.length} ENTRIES`
            : "STANDBY"}
        </span>
      </div>

      <div className="panel-body">
        <form
          onSubmit={(e) => void handleLoadReplay(e)}
          style={{ display: "flex", gap: "4px" }}
        >
          <input
            type="text"
            className="input"
            aria-label="Replay Mission ID"
            placeholder="Mission ID (e.g. M001)"
            value={missionIdInput}
            onChange={(e) => setMissionIdInput(e.target.value)}
            style={{ fontSize: "11px", padding: "4px 8px" }}
          />
          <button
            type="submit"
            className="btn btn-secondary"
            disabled={loading}
            aria-label="Load Mission Replay"
            style={{ fontSize: "10px", padding: "4px 10px", whiteSpace: "nowrap" }}
          >
            {loading ? "LOADING..." : "LOAD REPLAY"}
          </button>
        </form>

        {errorMsg && (
          <div className="c2-alert-box c2-alert-error" role="alert">
            {errorMsg}
          </div>
        )}

        {entries.length === 0 ? (
          <div style={{ color: "var(--text-muted)", fontSize: "11px", padding: "8px 0" }}>
            {loadedMissionId
              ? `No replay timeline entries recorded for mission ${loadedMissionId}.`
              : "Load mission ID to inspect chronological events and reasoning traces."}
          </div>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <label htmlFor="replay-timeline-slider" className="data-label" style={{ whiteSpace: "nowrap" }}>
                STEP ({selectedIndex + 1}/{entries.length})
              </label>
              <input
                id="replay-timeline-slider"
                type="range"
                min={0}
                max={Math.max(0, entries.length - 1)}
                value={selectedIndex}
                onChange={(e) => setSelectedIndex(Number(e.target.value))}
                style={{ flex: 1, accentColor: "var(--accent-primary)" }}
              />
            </div>

            <div
              style={{ display: "flex", flexDirection: "column", gap: "2px", maxHeight: "80px", overflowY: "auto" }}
              role="listbox"
              aria-label="Replay Timeline Entries"
            >
              {entries.map((item, idx) => {
                const isSelected = idx === selectedIndex;
                const labelId =
                  item.entry_type === "event"
                    ? item.event?.id
                    : item.reasoning_trace?.id;
                const summary =
                  item.entry_type === "event"
                    ? `[EVENT:${item.event?.category}] ${item.event?.message}`
                    : `[REASONING] ${item.reasoning_trace?.trigger}`;
                return (
                  <button
                    key={`${item.entry_type}-${labelId ?? idx}`}
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    onClick={() => setSelectedIndex(idx)}
                    className={`btn ${
                      isSelected ? "btn-primary" : "btn-secondary"
                    }`}
                    style={{
                      justifyContent: "space-between",
                      textAlign: "left",
                      width: "100%",
                      fontSize: "10px",
                      padding: "3px 6px",
                      fontFamily: "var(--font-data)",
                    }}
                  >
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      #{idx + 1} [{formatTacticalTimestamp(item.timestamp)}]{" "}
                      {summary}
                    </span>
                    <span style={{ flexShrink: 0, marginLeft: "6px" }}>{labelId}</span>
                  </button>
                );
              })}
            </div>

            {selectedEntry && (
              <div
                className="data-cell"
                style={{ background: "var(--sf-base)" }}
                data-testid="replay-selected-detail"
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    width: "100%",
                    marginBottom: "3px",
                  }}
                >
                  <span className="data-label">
                    ENTRY // {selectedEntry.entry_type.toUpperCase()}
                  </span>
                  <span style={{ fontFamily: "var(--font-data)", fontSize: "10px", color: "var(--text-muted)" }}>
                    {formatTacticalTimestamp(selectedEntry.timestamp)}
                  </span>
                </div>

                {selectedEntry.entry_type === "event" && selectedEntry.event && (
                  <div style={{ fontFamily: "var(--font-data)", fontSize: "11px", color: "var(--text-primary)" }}>
                    [{selectedEntry.event.category}] {selectedEntry.event.message}
                  </div>
                )}

                {selectedEntry.entry_type === "reasoning_trace" &&
                  selectedEntry.reasoning_trace && (
                    <div style={{ fontSize: "11px" }}>
                      <div style={{ fontFamily: "var(--font-data)", color: "var(--marker-scout)" }}>
                        TRIGGER: {selectedEntry.reasoning_trace.trigger}
                      </div>
                      <div style={{ color: "var(--text-primary)", marginTop: "2px" }}>
                        {selectedEntry.reasoning_trace.rationale}
                      </div>
                      <div
                        style={{ fontFamily: "var(--font-data)", color: "var(--text-muted)", fontSize: "10px", marginTop: "2px" }}
                      >
                        TASKS:{" "}
                        {selectedEntry.reasoning_trace.resulting_task_ids.join(
                          ", ",
                        ) || "NONE"}
                      </div>
                    </div>
                  )}

                <div
                  style={{
                    fontFamily: "var(--font-data)",
                    fontSize: "10px",
                    color: "var(--text-muted)",
                    borderTop: "1px solid var(--border-subtle)",
                    paddingTop: "3px",
                    marginTop: "4px",
                    width: "100%",
                  }}
                >
                  {selectedEntry.world_model_snapshot
                    ? `BACKEND SNAPSHOT METADATA: ${JSON.stringify(selectedEntry.world_model_snapshot)}`
                    : "WORLD MODEL SNAPSHOT: Not supplied by backend for this entry (no full before/after reconstruction fabricated)."}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
