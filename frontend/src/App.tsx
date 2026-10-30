import React, { useEffect } from "react";
import { fetchHealth } from "./api/client";
import { CameraFeed } from "./components/CameraFeed";
import { EmergencyStopButton } from "./components/EmergencyStopButton";
import { EventLog } from "./components/EventLog";
import { FloorMap } from "./components/FloorMap";
import { MissionPanel } from "./components/MissionPanel";
import { MissionReplayScrubber } from "./components/MissionReplayScrubber";
import { ReasoningTracePanel } from "./components/ReasoningTracePanel";
import { SimulatedModeToggle } from "./components/SimulatedModeToggle";
import { VehicleStatusCard } from "./components/VehicleStatusCard";
import { useDashboardSocket } from "./hooks/useDashboardSocket";
import { useMissionState } from "./hooks/useMissionState";
import { useDashboardStore } from "./state/store";

export interface AppProps {
  enableWebSocket?: boolean;
}

export default function App({
  enableWebSocket = true,
}: AppProps): React.JSX.Element {
  const state = useMissionState();
  const { dispatch } = useDashboardStore();
  const wsStatus = useDashboardSocket({ enabled: enableWebSocket });

  useEffect(() => {
    let cancelled = false;
    fetchHealth()
      .then((health) => {
        if (!cancelled) {
          dispatch({
            type: "SET_BACKEND_HEALTH",
            payload: {
              reachable: health.status === "ok",
              hardwareLockout: health.hardware_lockout,
              ultronIsStub: health.ultron_is_stub,
            },
          });
        }
      })
      .catch(() => {
        if (!cancelled) {
          dispatch({
            type: "SET_BACKEND_HEALTH",
            payload: {
              reachable: false,
              hardwareLockout: state.hardwareLockout,
              ultronIsStub: state.ultronIsStub,
            },
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [dispatch, state.hardwareLockout, state.ultronIsStub]);

  const scout = state.vehicles.SCOUT01;
  const allSimulated = scout.mode === "simulated";

  return (
    <main className="app-shell">
      {/* 1. Header Bar: Brand, High-Signal Status Badges & E-Stop Fast-Path */}
      <header className="header" aria-label="Global Operations Bar">
        <div className="header-brand">
          <div className="header-brand-mark" />
          <h1 className="header-brand-name">
            Operation Shadow — Mission Control
          </h1>
        </div>

        <div className="header-status">
          <span className="badge">
            <span
              className={`badge-dot ${
                state.missionStatus === "ACTIVE"
                  ? "badge-dot-nominal"
                  : "badge-dot-warning"
              }`}
            />
            MISSION: {state.activeMissionId ?? "NONE"} // Mission Status: {state.missionStatus}
          </span>

          <span
            className={`badge ${
              state.backendReachable ? "badge-nominal" : "badge-warning"
            }`}
          >
            API: {state.backendReachable ? "ONLINE" : "STANDBY"}
          </span>

          <span
            className={`badge ${
              wsStatus.connected ? "badge-nominal" : "badge-warning"
            }`}
          >
            <span
              className={`badge-dot ${
                wsStatus.connected ? "badge-dot-nominal" : "badge-dot-warning"
              }`}
            />
            WS: {wsStatus.connected ? "LINKED (LIVE PUSH)" : "DISCONNECTED"}
          </span>

          <span className="badge badge-warning">
            OPERATING MODE:{" "}
            {allSimulated ? "SIMULATED (MOCK ADAPTERS)" : "MIXED / REAL"}
          </span>

          <span className="badge" style={{ color: "var(--text-muted)" }}>
            LOCKOUT: {state.hardwareLockout ? "ENGAGED" : "DISENGAGED"}
          </span>
        </div>

        <div className="header-estop">
          <EmergencyStopButton />
        </div>
      </header>

      {/* 2. Left Rail: Mission Command & Operating Mode Safety */}
      <aside className="left-rail" aria-label="Command and Controls Rail">
        <MissionPanel />
        <SimulatedModeToggle />
      </aside>

      {/* 3. Center Column: Dominant Tactical Indoor Floor Map */}
      <FloorMap />

      {/* 4. Right Rail: Vehicle Inspector & Direct Optical Feed */}
      <aside className="right-rail" aria-label="Vehicle Inspector and Optical Feeds">
        <VehicleStatusCard vehicle={scout} />
        <CameraFeed />
      </aside>

      {/* 5. Bottom Dock: Mission Replay, Event Log & Reasoning Trace */}
      <footer className="ops-dock" aria-label="Operational Logs and Replay Dock">
        <MissionReplayScrubber />
        <EventLog />
        <ReasoningTracePanel />
      </footer>
    </main>
  );
}
