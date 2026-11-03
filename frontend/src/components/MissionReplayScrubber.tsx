import React, { useEffect, useState } from "react";
import {
  fetchMissionReplay,
  type MissionReplayTimelineEntryPayload,
} from "../api/client";
import { useDashboardStore } from "../state/store";
import { formatTacticalTimestamp } from "./VehicleStatusCard";

interface ReplayTaskItem {
  task_id?: string;
  id?: string;
  vehicle_id?: string;
  task_type?: string;
  type?: string;
  area?: string | null;
  target?: string | null;
  priority?: number;
  status?: string;
  execution_stage?: string;
  is_simulated?: boolean;
}

interface ReplayVehicleItem {
  vehicle_id?: string;
  mode?: string;
  status?: string;
  connection?: string;
  pose_valid?: boolean;
  position_x?: number;
  position_y?: number;
  battery?: number;
}

interface ReplayStateSummary {
  snapshot_id?: string;
  sequence?: number;
  timestamp_iso?: string;
  phase?: string;
  trigger_type?: string;
  trigger_reason?: string;
  mission_status?: string;
  mission_objective?: string;
  tasks?: ReplayTaskItem[];
  active_task_ids?: Record<string, string | null>;
  completed_task_ids?: string[];
  pending_task_ids?: string[];
  vehicles?: Record<string, ReplayVehicleItem>;
  tracked_objects?: Array<{
    object_id?: string;
    label?: string;
    coordinate_frame?: string;
    epistemic_status?: string;
    is_obstacle?: boolean;
    is_hazard?: boolean;
  }>;
  hazards?: Array<{
    id?: string;
    type?: string;
    coordinate_frame?: string;
    confidence?: number;
  }>;
  unknowns?: string[];
}

function renderStateBox(
  label: string,
  stateSummary: ReplayStateSummary | null | undefined,
  testId: string,
): React.JSX.Element {
  if (!stateSummary) {
    return (
      <div
        data-testid={testId}
        style={{
          flex: 1,
          border: "1px solid var(--border-subtle)",
          padding: "4px 6px",
          fontSize: "10px",
          fontFamily: "var(--font-data)",
          color: "var(--text-muted)",
        }}
      >
        <div style={{ fontWeight: 700, color: "var(--status-warning)" }}>
          {label}: UNAVAILABLE
        </div>
        <div>No historical snapshot recorded for this phase.</div>
      </div>
    );
  }

  const scout = stateSummary.vehicles?.SCOUT01;
  const tasks = stateSummary.tasks ?? [];
  const tracked = stateSummary.tracked_objects ?? [];
  const hazards = stateSummary.hazards ?? [];
  const unknowns = stateSummary.unknowns ?? [];

  return (
    <div
      data-testid={testId}
      style={{
        flex: 1,
        border: "1px solid var(--border-subtle)",
        padding: "4px 6px",
        fontSize: "10px",
        fontFamily: "var(--font-data)",
        color: "var(--text-primary)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontWeight: 700,
          color: "var(--accent-secondary)",
          marginBottom: "2px",
        }}
      >
        <span>
          {label} // {stateSummary.mission_status ?? "ACTIVE"}
        </span>
        <span>{stateSummary.snapshot_id ?? "SNAP"}</span>
      </div>

      {scout && (
        <div style={{ color: "var(--text-muted)" }}>
          SCOUT01: {scout.status ?? "IDLE"} ({scout.connection ?? "UNKNOWN"} /{" "}
          {(scout.mode ?? "simulated").toUpperCase()}) // POS (
          {(scout.position_x ?? 0).toFixed(2)}m,{" "}
          {(scout.position_y ?? 0).toFixed(2)}m) // BAT{" "}
          {(scout.battery ?? 100).toFixed(1)}%
        </div>
      )}

      <div style={{ marginTop: "2px" }}>
        TASKS ({tasks.length}):{" "}
        {tasks.length > 0
          ? tasks
              .map(
                (t) =>
                  `${t.task_id ?? t.id}:${t.task_type ?? t.type}[${t.status ?? "ASSIGNED"}]`,
              )
              .join(", ")
          : "NONE"}
      </div>

      <div style={{ marginTop: "2px", color: "var(--text-muted)" }}>
        OBJECTS ({tracked.length}):{" "}
        {tracked.length > 0
          ? tracked
              .map(
                (o) =>
                  `${o.object_id}:${o.label}(${o.epistemic_status ?? "OBSERVED"})`,
              )
              .join(", ")
          : "NONE"}{" "}
        | HAZARDS ({hazards.length}):{" "}
        {hazards.length > 0
          ? hazards.map((h) => `${h.id}:${h.type}`).join(", ")
          : "NONE"}
      </div>

      {unknowns.length > 0 && (
        <div style={{ marginTop: "2px", color: "var(--status-warning)" }}>
          UNKNOWNS ({unknowns.length}): {unknowns[0]}
        </div>
      )}
    </div>
  );
}

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
  const rawSnap = (selectedEntry?.world_model_snapshot ?? null) as Record<
    string,
    unknown
  > | null;
  const hasHistoricalSnapshot =
    rawSnap !== null &&
    rawSnap.historical_state_available === true &&
    rawSnap.is_stub !== true;

  const triggerMeta = (rawSnap?.trigger ?? null) as Record<
    string,
    unknown
  > | null;
  const decisionMeta = (rawSnap?.decision ?? null) as Record<
    string,
    unknown
  > | null;
  const beforeState = (rawSnap?.before_state ?? null) as
    | ReplayStateSummary
    | null;
  const afterState = (rawSnap?.after_state ?? null) as
    | ReplayStateSummary
    | null;

  return (
    <section className="panel" aria-label="Mission Replay Scrubber">
      <div className="panel-header">
        <h2 className="panel-title">MISSION REPLAY (READ-ONLY)</h2>
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
            style={{
              fontSize: "10px",
              padding: "4px 10px",
              whiteSpace: "nowrap",
            }}
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
          <div
            style={{
              color: "var(--text-muted)",
              fontSize: "11px",
              padding: "8px 0",
            }}
          >
            {loadedMissionId
              ? `No replay timeline entries recorded for mission ${loadedMissionId}.`
              : "Load mission ID to inspect chronological events and reasoning traces."}
          </div>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <label
                htmlFor="replay-timeline-slider"
                className="data-label"
                style={{ whiteSpace: "nowrap" }}
              >
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
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "2px",
                maxHeight: "95px",
                overflowY: "auto",
              }}
              role="listbox"
              aria-label="Replay Timeline Entries"
            >
              {entries.map((item, idx) => {
                const isSelected = idx === selectedIndex;
                const labelId =
                  item.entry_type === "event"
                    ? item.event?.id
                    : item.reasoning_trace?.id;
                const itemSnap = item.world_model_snapshot as Record<
                  string,
                  unknown
                > | null;
                const sev =
                  (itemSnap?.severity as string | undefined) ??
                  ((itemSnap?.trigger as Record<string, unknown> | undefined)
                    ?.severity as string | undefined) ??
                  "INFO";
                const summary =
                  item.entry_type === "event"
                    ? `[EVENT:${item.event?.category}/${sev}] ${item.event?.message}`
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
                    <span
                      style={{
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      #{idx + 1} [{formatTacticalTimestamp(item.timestamp)}]{" "}
                      {summary}
                    </span>
                    <span style={{ flexShrink: 0, marginLeft: "6px" }}>
                      {labelId}
                    </span>
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
                    alignItems: "center",
                    width: "100%",
                    marginBottom: "4px",
                  }}
                >
                  <span className="data-label">
                    ENTRY // {selectedEntry.entry_type.toUpperCase()}
                  </span>
                  <span
                    data-testid="replay-availability-badge"
                    style={{
                      fontFamily: "var(--font-data)",
                      fontSize: "10px",
                      color: hasHistoricalSnapshot
                        ? "var(--accent-secondary)"
                        : "var(--status-warning)",
                      fontWeight: 700,
                    }}
                  >
                    {hasHistoricalSnapshot
                      ? "HISTORICAL STATE RECORDED"
                      : "HISTORICAL STATE UNAVAILABLE"}
                  </span>
                  <span
                    style={{
                      fontFamily: "var(--font-data)",
                      fontSize: "10px",
                      color: "var(--text-muted)",
                    }}
                  >
                    {formatTacticalTimestamp(selectedEntry.timestamp)}
                  </span>
                </div>

                {selectedEntry.entry_type === "event" && selectedEntry.event && (
                  <div
                    style={{
                      fontFamily: "var(--font-data)",
                      fontSize: "11px",
                      color: "var(--text-primary)",
                    }}
                  >
                    <div>
                      [{selectedEntry.event.category}]{" "}
                      {selectedEntry.event.message}
                    </div>
                    {rawSnap && (
                      <div
                        style={{
                          fontSize: "10px",
                          color: "var(--text-muted)",
                          marginTop: "2px",
                        }}
                      >
                        SEVERITY: {String(rawSnap.severity ?? "INFO")} //
                        SOURCE: {String(rawSnap.trigger_source ?? "mission")} (
                        {String(rawSnap.source_mode ?? "simulated")}) //
                        EVIDENCE:{" "}
                        {Array.isArray(rawSnap.evidence_ids) &&
                        rawSnap.evidence_ids.length > 0
                          ? rawSnap.evidence_ids.join(", ")
                          : "NONE"}
                      </div>
                    )}
                  </div>
                )}

                {selectedEntry.entry_type === "reasoning_trace" &&
                  selectedEntry.reasoning_trace && (
                    <div style={{ fontSize: "11px" }}>
                      <div
                        style={{
                          fontFamily: "var(--font-data)",
                          color: "var(--marker-scout)",
                        }}
                      >
                        TRIGGER: {selectedEntry.reasoning_trace.trigger}
                      </div>
                      <div
                        style={{
                          color: "var(--text-primary)",
                          marginTop: "2px",
                        }}
                      >
                        {selectedEntry.reasoning_trace.rationale}
                      </div>
                      <div
                        style={{
                          fontFamily: "var(--font-data)",
                          color: "var(--text-muted)",
                          fontSize: "10px",
                          marginTop: "2px",
                        }}
                      >
                        TASKS:{" "}
                        {selectedEntry.reasoning_trace.resulting_task_ids.join(
                          ", ",
                        ) || "NONE"}
                      </div>

                      {triggerMeta && (
                        <div
                          style={{
                            fontFamily: "var(--font-data)",
                            fontSize: "10px",
                            color: "var(--text-muted)",
                            marginTop: "2px",
                          }}
                        >
                          ORIGIN EVENT:{" "}
                          {String(triggerMeta.event_id ?? "NONE")} // SEVERITY:{" "}
                          {String(triggerMeta.severity ?? "INFO")} // SOURCE:{" "}
                          {String(triggerMeta.source ?? "mission")} (
                          {String(triggerMeta.source_mode ?? "simulated")}) //
                          STANDING ORDER:{" "}
                          {String(triggerMeta.standing_order_id ?? "NONE")} //
                          TRIAGE:{" "}
                          {String(triggerMeta.triage_disposition ?? "N/A")} //
                          EVIDENCE:{" "}
                          {Array.isArray(triggerMeta.evidence_ids) &&
                          triggerMeta.evidence_ids.length > 0
                            ? (triggerMeta.evidence_ids as string[]).join(", ")
                            : "NONE"}
                        </div>
                      )}

                      {decisionMeta && (
                        <div
                          style={{
                            fontFamily: "var(--font-data)",
                            fontSize: "10px",
                            color: "var(--text-muted)",
                            marginTop: "2px",
                          }}
                        >
                          VALIDATION:{" "}
                          {decisionMeta.validation_passed === false
                            ? "REJECTED / DEGRADED"
                            : "PASSED (PLAN ACCEPTED)"}{" "}
                          // PLANNER SOURCE:{" "}
                          {String(decisionMeta.planner_source ?? "ULTRON")}
                        </div>
                      )}
                    </div>
                  )}

                {hasHistoricalSnapshot && (beforeState || afterState) && (
                  <div
                    style={{
                      display: "flex",
                      gap: "6px",
                      marginTop: "6px",
                      width: "100%",
                    }}
                  >
                    {renderStateBox(
                      "BEFORE",
                      beforeState,
                      "replay-before-state",
                    )}
                    {selectedEntry.entry_type === "reasoning_trace" &&
                      renderStateBox(
                        "AFTER",
                        afterState,
                        "replay-after-state",
                      )}
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
