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
  const hawk = state.vehicles.HAWK01;
  const allSimulated =
    scout.mode === "simulated" && hawk.mode === "simulated";

  const globalWarnings: string[] = [];
  if (allSimulated) {
    globalWarnings.push(
      "SIMULATION MODE ACTIVE — SCOUT01 AND HAWK01 ARE RUNNING ON MOCK ADAPTERS (NO PHYSICAL VEHICLES CONNECTED)",
    );
  }
  if (!wsStatus.connected && wsStatus.lastError) {
    globalWarnings.push(`WS STATUS: ${wsStatus.lastError}`);
  }
  for (const v of [scout, hawk]) {
    if (v.status === "EMERGENCY_STOPPED") {
      globalWarnings.push(`${v.id} IS IN EMERGENCY_STOPPED STATE`);
    } else if (v.connection === "UNKNOWN" || v.status === "UNKNOWN") {
      globalWarnings.push(`${v.id} HEARTBEAT TIMEOUT — STATE UNKNOWN`);
    } else if (v.isStale) {
      globalWarnings.push(`${v.id} TELEMETRY IS STALE`);
    }
  }

  return (
    <main className="c2-shell">
      {/* A. Global Operations Bar */}
      <header className="c2-topbar" aria-label="Global Operations Bar">
        <div className="c2-topbar-left">
          <div>
            <h1 className="c2-brand-title">
              Operation Shadow — Mission Control
            </h1>
            <div className="c2-brand-subtitle">
              INDOOR MULTI-VEHICLE TACTICAL C2 WORKSTATION // SCOUT01 &amp; HAWK01
            </div>
          </div>
        </div>

        <div className="c2-topbar-center">
          <span className="c2-badge">
            MISSION: {state.activeMissionId ?? "NONE"} // Mission Status:{" "}
            {state.missionStatus}
          </span>
          <span
            className={`c2-badge ${
              state.backendReachable ? "c2-badge-nominal" : "c2-badge-warning"
            }`}
          >
            API: {state.backendReachable ? "ONLINE" : "STANDBY"}
          </span>
          <span
            className={`c2-badge ${
              wsStatus.connected ? "c2-badge-nominal" : "c2-badge-warning"
            }`}
          >
            WS: {wsStatus.connected ? "LINKED (LIVE PUSH)" : "DISCONNECTED"}
          </span>
          <span className="c2-badge c2-badge-warning">
            OPERATING MODE:{" "}
            {allSimulated ? "SIMULATED (MOCK ADAPTERS)" : "MIXED / REAL"}
          </span>
        </div>

        <div className="c2-topbar-right">
          <EmergencyStopButton />
        </div>
      </header>

      {/* Global Degraded / Simulation Notice Banner */}
      <div
        className="c2-global-banner"
        role="status"
        aria-label="Global Operational Warnings"
      >
        <span>{globalWarnings.join("  |  ")}</span>
        <span>
          LOCKOUT: {state.hardwareLockout ? "ENGAGED" : "DISENGAGED"}
        </span>
      </div>

      {/* 3-Column Tactical Workstation Grid */}
      <div className="c2-workstation-grid">
        {/* Left Rail: Mission Control, Vehicle Cards & Mode Control */}
        <div className="c2-column" aria-label="Command and Vehicle Status Rail">
          <MissionPanel />
          <section
            aria-label="Vehicles"
            style={{ display: "flex", flexDirection: "column", gap: "6px" }}
          >
            <VehicleStatusCard vehicle={scout} />
            <VehicleStatusCard vehicle={hawk} />
          </section>
          <SimulatedModeToggle />
        </div>

        {/* Center Focal Viewport: Tactical Indoor Floor Map & Replay Scrubber */}
        <div className="c2-column" aria-label="Tactical Map and Replay Viewport">
          <FloorMap />
          <MissionReplayScrubber />
        </div>

        {/* Right Rail: Direct MJPEG Camera Feed, Reasoning Trace & Event Log */}
        <div
          className="c2-column"
          aria-label="Optical Feeds and Operational Logs Rail"
        >
          <CameraFeed />
          <ReasoningTracePanel />
          <EventLog />
        </div>
      </div>
    </main>
  );
}
