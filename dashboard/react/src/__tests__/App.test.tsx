import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as apiClient from "../api/client";
import {
  MAP_PADDING,
  MAP_VIEWBOX_HEIGHT,
  MAP_VIEWBOX_WIDTH,
  worldRectToSvg,
  worldToSvgX,
  worldToSvgY,
} from "../components/FloorMap";
import {
  DashboardProvider,
  dashboardReducer,
  initialDashboardState,
} from "../state/store";

const DEFAULT_FLOOR_MAP: apiClient.FloorPlanConfigPayload = {
  is_provisional_simulation_config: true,
  coordinate_system: "local_cartesian_meters",
  width_m: 20,
  height_m: 15,
  rooms: [
    {
      id: "SECTOR_A",
      name: "Sector Alpha — Entry Corridor",
      x: 0,
      y: 0,
      width: 10,
      height: 7.5,
    },
  ],
  waypoints: [{ id: "WP_BASE", label: "Base Station", x: 1.5, y: 1.5 }],
  hawk_geofence: {
    id: "GEOFENCE_HAWK01",
    label: "HAWK01 Indoor Flight Envelope (Provisional)",
    x: 1,
    y: 1,
    width: 18,
    height: 13,
    max_altitude_m: 3,
  },
};

class MockWebSocket {
  public static instances: MockWebSocket[] = [];
  public url: string;
  public onopen: (() => void) | null = null;
  public onmessage: ((event: { data: string }) => void) | null = null;
  public onerror: (() => void) | null = null;
  public onclose: (() => void) | null = null;
  public closed = false;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  public close(): void {
    this.closed = true;
  }

  public triggerOpen(): void {
    this.onopen?.();
  }

  public triggerMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  public triggerClose(): void {
    this.onclose?.();
  }
}

describe("Operation Shadow Milestone 5 & 6 Tactical Command Dashboard", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.spyOn(apiClient, "fetchHealth").mockResolvedValue({
      status: "ok",
      database: "ok",
      hardware_lockout: true,
      default_vehicle_mode: "simulated",
      ultron_is_stub: true,
      vehicles: ["HAWK01", "SCOUT01"],
    });
    vi.spyOn(apiClient, "fetchFloorMap").mockResolvedValue(DEFAULT_FLOOR_MAP);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  // 1. Initial dashboard rendering & coordinate math
  it("renders the tactical C2 shell, global operations bar, provisional floor map, and simulated vehicles", async () => {
    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    expect(
      screen.getByRole("heading", {
        name: /Operation Shadow — Mission Control/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("article", { name: "Vehicle SCOUT01" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("article", { name: "Vehicle HAWK01" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Hardware Lockout:\s*ACTIVE \(Simulated Only\)/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/PROVISIONAL SIMULATION LAYOUT/i),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText(/API: ONLINE/i)).toBeInTheDocument();
    });

    // Verify deterministic SVG coordinate transformation helpers
    expect(worldToSvgX(0, 20)).toBe(MAP_PADDING);
    expect(worldToSvgX(20, 20)).toBe(MAP_VIEWBOX_WIDTH - MAP_PADDING);
    expect(worldToSvgY(0, 15)).toBe(MAP_PADDING);
    expect(worldToSvgY(15, 15)).toBe(MAP_VIEWBOX_HEIGHT - MAP_PADDING);
    const rect = worldRectToSvg(0, 0, 10, 7.5, 20, 15);
    expect(rect.x).toBe(MAP_PADDING);
    expect(rect.y).toBe(MAP_PADDING);
    expect(rect.width).toBe((MAP_VIEWBOX_WIDTH - MAP_PADDING * 2) / 2);
  });

  // 2. Mission submission and error handling
  it("validates empty mission objectives, submits valid missions, and renders backend errors", async () => {
    const createSpy = vi
      .spyOn(apiClient, "createMission")
      .mockResolvedValueOnce({ mission_id: "M001" })
      .mockRejectedValueOnce(
        new apiClient.ApiClientError("Backend mission fault", 500, "HTTP_500"),
      );
    vi.spyOn(apiClient, "fetchMission").mockResolvedValueOnce({
      id: "M001",
      objective: "Sweep Sector Alpha",
      status: "ACTIVE",
      created_at: "2026-10-28T10:00:00Z",
      completed_at: null,
      tasks: [
        {
          id: "M001-T01",
          mission_id: "M001",
          vehicle_id: "SCOUT01",
          type: "SIMULATED_GROUND_RECON",
          area: "SECTOR_A",
          target: "Sweep Sector Alpha",
          priority: 1,
          status: "ASSIGNED",
        },
      ],
    });

    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    const dispatchBtn = screen.getByRole("button", { name: "Submit Mission" });

    // Empty input triggers validation error without calling API
    fireEvent.click(dispatchBtn);
    expect(
      screen.getByText(/Mission objective cannot be empty/i),
    ).toBeInTheDocument();
    expect(createSpy).not.toHaveBeenCalled();

    // Valid objective submission
    const input = screen.getByLabelText(/MISSION OBJECTIVE/i);
    fireEvent.change(input, { target: { value: "Sweep Sector Alpha" } });
    fireEvent.click(dispatchBtn);

    await waitFor(() => {
      expect(
        screen.getByText(/Mission M001 dispatched \(1 tasks assigned\)/i),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/M001-T01 \[P1\] → SCOUT01/i)).toBeInTheDocument();

    // Backend failure on second submission
    fireEvent.change(input, { target: { value: "Second mission" } });
    fireEvent.click(dispatchBtn);
    await waitFor(() => {
      expect(screen.getByText(/Backend mission fault/i)).toBeInTheDocument();
    });
  });

  // 3, 4, 5, 6 & 8. WebSocket connect, snapshot, live telemetry, stale/unknown states, event log, and reconnect resync
  it("hydrates from WebSocket snapshot, applies live telemetry and events, shows stale/UNKNOWN states, and resyncs on reconnect", async () => {
    render(
      <DashboardProvider>
        <App enableWebSocket={true} />
      </DashboardProvider>,
    );

    expect(MockWebSocket.instances.length).toBe(1);
    const ws1 = MockWebSocket.instances[0];

    act(() => {
      ws1.triggerOpen();
      ws1.triggerMessage({
        type: "snapshot",
        protocol_version: "1.0",
        sequence: 1,
        payload: {
          protocol_version: "1.0",
          generated_at: new Date().toISOString(),
          current_mission: {
            id: "M002",
            objective: "Recon Bravo Bay",
            status: "ACTIVE",
            created_at: new Date().toISOString(),
            completed_at: null,
            tasks: [],
          },
          vehicles: {
            SCOUT01: {
              id: "SCOUT01",
              vehicle_type: "ground",
              status: "ACTIVE",
              battery: 84.5,
              position_x: 5.0,
              position_y: 3.75,
              position_alt: 0.0,
              connection: "CONNECTED",
              mode: "simulated",
              last_heartbeat: new Date().toISOString(),
            },
            HAWK01: {
              id: "HAWK01",
              vehicle_type: "aerial",
              status: "UNKNOWN",
              battery: 72.0,
              position_x: 10.0,
              position_y: 7.5,
              position_alt: 1.8,
              connection: "UNKNOWN",
              mode: "simulated",
              last_heartbeat: "2026-10-20T00:00:00Z",
            },
          },
          recent_events: [
            {
              id: "EVT-1",
              mission_id: "M002",
              timestamp: "2026-10-28T10:01:00Z",
              category: "MISSION",
              message: "Mission M002 active",
            },
          ],
          recent_reasoning_traces: [
            {
              id: "RT-1",
              mission_id: "M002",
              timestamp: "2026-10-28T10:01:01Z",
              trigger: "Operator submitted M002",
              rationale: "[ULTRON_STUB] Simulated task split",
              resulting_task_ids: ["M002-T01"],
            },
          ],
        },
      });
    });

    // Verify WS connected badge, vehicle status cards, unknown state warning, event log, and reasoning trace
    expect(screen.getByText(/WS: LINKED \(LIVE PUSH\)/i)).toBeInTheDocument();
    const scoutCard = screen.getByRole("article", { name: "Vehicle SCOUT01" });
    expect(within(scoutCard).getByText("84.5%")).toBeInTheDocument();
    expect(within(scoutCard).getByText("5.00m")).toBeInTheDocument();

    const hawkCard = screen.getByRole("article", { name: "Vehicle HAWK01" });
    expect(
      within(hawkCard).getByText(/WARNING: HEARTBEAT TIMEOUT — STATE UNKNOWN/i),
    ).toBeInTheDocument();
    expect(within(hawkCard).getByText(/72.0% \[LAST KNOWN\]/i)).toBeInTheDocument();

    expect(screen.getByText("Mission M002 active")).toBeInTheDocument();
    expect(
      screen.getByText(/\[ULTRON_STUB\] Simulated task split/i),
    ).toBeInTheDocument();

    // Push live telemetry update for SCOUT01 and verify out-of-order lower sequence is ignored
    const freshHb = new Date().toISOString();
    act(() => {
      ws1.triggerMessage({
        type: "telemetry",
        sequence: 10,
        payload: {
          vehicle_id: "SCOUT01",
          status: "EXECUTING",
          connection: "CONNECTED",
          mode: "simulated",
          battery: 81.2,
          position_x: 7.25,
          position_y: 4.5,
          position_alt: 0.0,
          last_heartbeat: freshHb,
        },
      });
      // Out-of-order older sequence 5 must be ignored
      ws1.triggerMessage({
        type: "telemetry",
        sequence: 5,
        payload: {
          vehicle_id: "SCOUT01",
          status: "IDLE",
          connection: "CONNECTED",
          mode: "simulated",
          battery: 10.0,
          position_x: 0.0,
          position_y: 0.0,
          position_alt: 0.0,
          last_heartbeat: freshHb,
        },
      });
      ws1.triggerMessage({
        type: "event",
        sequence: 11,
        payload: {
          id: "EVT-2",
          mission_id: "M002",
          timestamp: "2026-10-28T10:01:05Z",
          category: "TELEMETRY",
          message: "SCOUT01 reached checkpoint",
        },
      });
    });

    expect(within(scoutCard).getByText("81.2%")).toBeInTheDocument();
    expect(within(scoutCard).getByText("7.25m")).toBeInTheDocument();
    expect(screen.getByText("SCOUT01 reached checkpoint")).toBeInTheDocument();

    // Simulate WebSocket disconnect and reconnect
    vi.useFakeTimers();
    act(() => {
      ws1.triggerClose();
    });
    expect(screen.getByText(/WS: DISCONNECTED/i)).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1600);
    });
    expect(MockWebSocket.instances.length).toBe(2);
    const ws2 = MockWebSocket.instances[1];
    act(() => {
      ws2.triggerOpen();
    });
    expect(screen.getByText(/WS: LINKED \(LIVE PUSH\)/i)).toBeInTheDocument();
    vi.useRealTimers();
  });

  // 7. Simulation-mode status & hardware lockout error
  it("handles simulated mode confirmation and displays hardware lockout error when attempting real mode", async () => {
    vi.spyOn(apiClient, "setVehicleSimulatedMode")
      .mockResolvedValueOnce({
        vehicle_id: "SCOUT01",
        mode: "simulated",
        enabled: true,
        status: "IDLE",
        connection: "CONNECTED",
      })
      .mockRejectedValueOnce(
        new apiClient.ApiClientError(
          "Real hardware control is locked (SHADOW_HARDWARE_LOCKOUT=true).",
          409,
          "HARDWARE_LOCKOUT_ACTIVE",
        ),
      );

    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    const simBtn = screen.getByRole("button", {
      name: "Set SCOUT01 to Simulated Mode",
    });
    fireEvent.click(simBtn);
    await waitFor(() => {
      expect(
        screen.getByText(/SCOUT01 mode confirmed by backend: SIMULATED/i),
      ).toBeInTheDocument();
    });

    const realBtn = screen.getByRole("button", {
      name: "Attempt Real Hardware Mode for SCOUT01",
    });
    fireEvent.click(realBtn);
    await waitFor(() => {
      expect(
        screen.getByText(/Real hardware control is locked/i),
      ).toBeInTheDocument();
    });

    // Reducer also enforces hardware lockout
    const nextState = dashboardReducer(initialDashboardState, {
      type: "SET_VEHICLE_MODE",
      payload: { vehicleId: "SCOUT01", mode: "real" },
    });
    expect(nextState.vehicles.SCOUT01.mode).toBe("simulated");
  });

  // 9. Mission replay loading and timeline selection
  it("loads mission replay history, scrubs timeline entries, and honestly reports missing snapshot data", async () => {
    vi.spyOn(apiClient, "fetchMissionReplay").mockResolvedValueOnce({
      mission_id: "M001",
      entries: [
        {
          entry_type: "event",
          timestamp: "2026-10-28T10:00:00Z",
          event: {
            id: "M001-EVT01",
            mission_id: "M001",
            timestamp: "2026-10-28T10:00:00Z",
            category: "MISSION",
            message: "Mission M001 initialized",
          },
          reasoning_trace: null,
          world_model_snapshot: null,
        },
        {
          entry_type: "reasoning_trace",
          timestamp: "2026-10-28T10:00:01Z",
          event: null,
          reasoning_trace: {
            id: "M001-RT01",
            mission_id: "M001",
            timestamp: "2026-10-28T10:00:01Z",
            trigger: "Operator submitted M001",
            rationale: "[ULTRON_STUB] Assigned SCOUT01 and HAWK01",
            resulting_task_ids: ["M001-T01", "M001-T02"],
          },
          world_model_snapshot: {
            is_stub: true,
            resulting_task_ids: ["M001-T01", "M001-T02"],
          },
        },
      ],
    });

    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    const replayInput = screen.getByLabelText("Replay Mission ID");
    fireEvent.change(replayInput, { target: { value: "M001" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Load Mission Replay" }),
    );

    await waitFor(() => {
      expect(screen.getByText(/M001 \/\/ 2 ENTRIES/i)).toBeInTheDocument();
    });

    const detailBox = screen.getByTestId("replay-selected-detail");
    expect(
      within(detailBox).getByText(/Not supplied by backend for this entry/i),
    ).toBeInTheDocument();

    // Select second timeline entry (reasoning trace)
    const options = screen.getAllByRole("option");
    fireEvent.click(options[1]);
    expect(
      within(detailBox).getByText(/TRIGGER: Operator submitted M001/i),
    ).toBeInTheDocument();
    expect(
      within(detailBox).getByText(/BACKEND SNAPSHOT METADATA:/i),
    ).toBeInTheDocument();
  });

  // 10. Camera feed unavailable/error states
  it("displays honest camera unavailable state when unconfigured and error state when stream fails", () => {
    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    expect(
      screen.getByText(
        /CAMERA STREAM UNAVAILABLE \/\/ NO DIRECT MJPEG URL CONFIGURED/i,
      ),
    ).toBeInTheDocument();

    const urlInput = screen.getByLabelText("SCOUT01 MJPEG Stream URL");
    fireEvent.change(urlInput, {
      target: { value: "http://192.0.2.10:8080/stream.mjpg" },
    });
    fireEvent.click(screen.getByRole("button", { name: "SET URL" }));

    const img = screen.getByAltText("SCOUT01 direct MJPEG camera stream");
    expect(img).toHaveAttribute("src", "http://192.0.2.10:8080/stream.mjpg");

    // Simulate stream load failure
    fireEvent.error(img);
    expect(
      screen.getByText(
        /CAMERA STREAM ERROR \/\/ DIRECT MJPEG FEED UNREACHABLE/i,
      ),
    ).toBeInTheDocument();
  });

  // 11 & 12. Emergency-stop confirmation, partial failure, and distinction between HTTP 200 and confirmed stopping
  it("requires confirmation for E-Stop, displays partial failure, and distinguishes HTTP acceptance from confirmed stop", async () => {
    const estopSpy = vi
      .spyOn(apiClient, "triggerEmergencyStop")
      // First call: partial failure (SCOUT01 failed/disconnected, HAWK01 confirmed stopped)
      .mockResolvedValueOnce({
        request_accepted: true,
        target: "ALL",
        overall_outcome: "PARTIAL_STOPPED",
        all_confirmed_stopped: false,
        any_dispatch_failed: true,
        hardware_lockout_active: true,
        real_hardware_estop_available: false,
        fast_path_used: true,
        async_audit_log_scheduled: true,
        dispatch_elapsed_ms: 0.42,
        safety_notice: "SIMULATED MODE ONLY",
        vehicle_outcomes: {
          SCOUT01: {
            vehicle_id: "SCOUT01",
            mode: "simulated",
            is_simulated: true,
            real_hardware_available: false,
            connection_at_dispatch: "DISCONNECTED",
            dispatch_attempted: true,
            dispatch_succeeded: false,
            vehicle_acknowledged: false,
            vehicle_confirmed_stopped: false,
            resulting_status: "IDLE",
            was_already_stopped: false,
            dispatched_at: "2026-10-28T10:05:00Z",
            acknowledged_at: null,
            reason:
              "Simulated vehicle SCOUT01 is DISCONNECTED; stop signal could not be delivered or confirmed.",
          },
          HAWK01: {
            vehicle_id: "HAWK01",
            mode: "simulated",
            is_simulated: true,
            real_hardware_available: false,
            connection_at_dispatch: "CONNECTED",
            dispatch_attempted: true,
            dispatch_succeeded: true,
            vehicle_acknowledged: true,
            vehicle_confirmed_stopped: true,
            resulting_status: "EMERGENCY_STOPPED",
            was_already_stopped: false,
            dispatched_at: "2026-10-28T10:05:00Z",
            acknowledged_at: "2026-10-28T10:05:00Z",
            reason:
              "Simulated emergency stop acknowledged and confirmed by HAWK01.",
          },
        },
      });

    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    // Step 1: Arm E-STOP ALL
    fireEvent.click(
      screen.getByRole("button", { name: "Emergency Stop All Vehicles" }),
    );
    expect(estopSpy).not.toHaveBeenCalled();

    // Step 2: Confirm E-STOP [ALL]
    const confirmBtn = screen.getByRole("button", {
      name: "Confirm Emergency Stop ALL",
    });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(screen.getByTestId("estop-outcome-summary")).toBeInTheDocument();
    });

    const summary = screen.getByTestId("estop-outcome-summary");
    expect(
      within(summary).getByText(
        /HTTP REQUEST ACCEPTED: YES \/\/ OVERALL: PARTIAL_STOPPED/i,
      ),
    ).toBeInTheDocument();

    const scoutOutcome = screen.getByTestId("estop-vehicle-outcome-SCOUT01");
    expect(
      within(scoutOutcome).getByText(/DISPATCH: FAILED/i),
    ).toBeInTheDocument();
    expect(
      within(scoutOutcome).getByText(/VEHICLE ACK: NO/i),
    ).toBeInTheDocument();
    expect(
      within(scoutOutcome).getByText(/CONFIRMED STOPPED: NO \(UNCONFIRMED\)/i),
    ).toBeInTheDocument();

    const hawkOutcome = screen.getByTestId("estop-vehicle-outcome-HAWK01");
    expect(
      within(hawkOutcome).getByText(/DISPATCH: SUCCEEDED/i),
    ).toBeInTheDocument();
    expect(
      within(hawkOutcome).getByText(/VEHICLE ACK: YES/i),
    ).toBeInTheDocument();
    expect(
      within(hawkOutcome).getByText(/CONFIRMED STOPPED: YES/i),
    ).toBeInTheDocument();
  });

  // 13. Simulated mission completion and end-to-end replay inspection
  it("completes an active simulated mission from MissionPanel and updates mission status to COMPLETED", async () => {
    vi.spyOn(apiClient, "createMission").mockResolvedValueOnce({
      mission_id: "M003",
    });
    vi.spyOn(apiClient, "fetchMission").mockResolvedValueOnce({
      id: "M003",
      objective: "Full simulated mission lifecycle",
      status: "ACTIVE",
      created_at: "2026-10-28T10:10:00Z",
      completed_at: null,
      tasks: [
        {
          id: "M003-T01",
          mission_id: "M003",
          vehicle_id: "SCOUT01",
          type: "SIMULATED_GROUND_RECON",
          area: "SECTOR_A",
          target: "Full simulated mission lifecycle",
          priority: 1,
          status: "ASSIGNED",
        },
      ],
    });
    const completeSpy = vi
      .spyOn(apiClient, "completeMission")
      .mockResolvedValueOnce({
        id: "M003",
        objective: "Full simulated mission lifecycle",
        status: "COMPLETED",
        created_at: "2026-10-28T10:10:00Z",
        completed_at: "2026-10-28T10:12:00Z",
        tasks: [
          {
            id: "M003-T01",
            mission_id: "M003",
            vehicle_id: "SCOUT01",
            type: "SIMULATED_GROUND_RECON",
            area: "SECTOR_A",
            target: "Full simulated mission lifecycle",
            priority: 1,
            status: "COMPLETED",
          },
        ],
      });

    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    const input = screen.getByLabelText(/MISSION OBJECTIVE/i);
    fireEvent.change(input, {
      target: { value: "Full simulated mission lifecycle" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit Mission" }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Complete Simulated Mission" }),
      ).toBeInTheDocument();
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Complete Simulated Mission" }),
    );

    await waitFor(() => {
      expect(completeSpy).toHaveBeenCalledWith("M003");
      expect(
        screen.getByText(/Mission M003 marked COMPLETED/i),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/M003 \/\/ COMPLETED/i)).toBeInTheDocument();
  });
});
