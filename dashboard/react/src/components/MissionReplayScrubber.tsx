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
    <section className="c2-panel" aria-label="Mission Replay Scrubber">
      <div className="c2-panel-header">
        <h2 className="c2-panel-title">
          POST-HOC MISSION REPLAY SCRUBBER (READ-ONLY)
        </h2>
        <span className="c2-panel-meta">
          {loadedMissionId
            ? `${loadedMissionId} // ${entries.length} ENTRIES`
            : "STANDBY"}
        </span>
      </div>

      <div className="c2-panel-body">
        <form
          onSubmit={(e) => void handleLoadReplay(e)}
          style={{ display: "flex", gap: "6px" }}
        >
          <input
            type="text"
            className="c2-input"
            aria-label="Replay Mission ID"
            placeholder="Mission ID (e.g. M001)"
            value={missionIdInput}
            onChange={(e) => setMissionIdInput(e.target.value)}
          />
          <button
            type="submit"
            className="c2-btn"
            disabled={loading}
            aria-label="Load Mission Replay"
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
          <div className="c2-log-viewport" style={{ color: "var(--text-muted)" }}>
            {loadedMissionId
              ? `No replay timeline entries recorded for mission ${loadedMissionId}.`
              : "Load a completed or active mission ID to inspect chronological events and reasoning traces."}
          </div>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <label htmlFor="replay-timeline-slider" className="c2-kv-label">
                TIMELINE STEP ({selectedIndex + 1}/{entries.length})
              </label>
              <input
                id="replay-timeline-slider"
                type="range"
                min={0}
                max={Math.max(0, entries.length - 1)}
                value={selectedIndex}
                onChange={(e) => setSelectedIndex(Number(e.target.value))}
                style={{ flex: 1 }}
              />
            </div>

            <div
              className="c2-log-viewport"
              style={{ maxHeight: "130px" }}
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
                    className={`c2-btn ${
                      isSelected ? "c2-btn-active" : "c2-btn-secondary"
                    }`}
                    style={{
                      justifyContent: "space-between",
                      textAlign: "left",
                      width: "100%",
                    }}
                  >
                    <span>
                      #{idx + 1} [{formatTacticalTimestamp(item.timestamp)}]{" "}
                      {summary}
                    </span>
                    <span>{labelId}</span>
                  </button>
                );
              })}
            </div>

            {selectedEntry && (
              <div
                className="c2-kv-item"
                style={{ flexDirection: "column", gap: "4px" }}
                data-testid="replay-selected-detail"
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    width: "100%",
                  }}
                >
                  <span className="c2-kv-label">
                    SELECTED ENTRY // {selectedEntry.entry_type.toUpperCase()}
                  </span>
                  <span className="c2-mono" style={{ fontSize: "11px" }}>
                    {formatTacticalTimestamp(selectedEntry.timestamp)}
                  </span>
                </div>

                {selectedEntry.entry_type === "event" && selectedEntry.event && (
                  <div className="c2-mono" style={{ fontSize: "12px" }}>
                    [{selectedEntry.event.category}] {selectedEntry.event.message}
                  </div>
                )}

                {selectedEntry.entry_type === "reasoning_trace" &&
                  selectedEntry.reasoning_trace && (
                    <div style={{ fontSize: "12px" }}>
                      <div className="c2-mono" style={{ color: "var(--accent-sage)" }}>
                        TRIGGER: {selectedEntry.reasoning_trace.trigger}
                      </div>
                      <div>{selectedEntry.reasoning_trace.rationale}</div>
                      <div
                        className="c2-mono"
                        style={{ color: "var(--text-secondary)", fontSize: "11px" }}
                      >
                        TASKS:{" "}
                        {selectedEntry.reasoning_trace.resulting_task_ids.join(
                          ", ",
                        ) || "NONE"}
                      </div>
                    </div>
                  )}

                <div
                  className="c2-mono"
                  style={{
                    fontSize: "11px",
                    color: "var(--text-secondary)",
                    borderTop: "1px solid var(--border-subtle)",
                    paddingTop: "4px",
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
