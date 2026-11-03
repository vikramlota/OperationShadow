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
    label: "HAWK01 Indoor Flight Envelope (Simulated / Roadmap, Not Integrated)",
    x: 1,
    y: 1,
    width: 18,
    height: 13,
    max_altitude_m: 3,
  },
};

class MockWebSocket {
  public static instances: MockWebSocket[] = [];
  public static OPEN = 1;
  public url: string;
  public readyState = 0;
  public sentMessages: string[] = [];
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
    this.readyState = 3;
  }

  public send(message: string): void {
    this.sentMessages.push(message);
  }

  public triggerOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  public triggerMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  public triggerClose(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

function dashboardWebSockets(): MockWebSocket[] {
  return MockWebSocket.instances.filter((socket) =>
    socket.url.includes("/ws/dashboard"),
  );
}

function makePointCloudMessage(topic = "/cloud_registered", shift = 0): object {
  const buffer = new ArrayBuffer(36);
  const view = new DataView(buffer);
  const points = [
    [1 + shift, 0, 0],
    [0 + shift, 2, 0],
    [-1 + shift, 0, 0],
  ];
  points.forEach(([x, y, z], pointIndex) => {
    const offset = pointIndex * 12;
    view.setFloat32(offset, x, true);
    view.setFloat32(offset + 4, y, true);
    view.setFloat32(offset + 8, z, true);
  });
  const bytes = String.fromCharCode(...new Uint8Array(buffer));

  return {
    op: "publish",
    topic,
    msg: {
      header: {
        frame_id: "camera_init",
        stamp: { sec: 100, nanosec: 0 },
      },
      height: 1,
      width: points.length,
      fields: [
        { name: "x", offset: 0, datatype: 7, count: 1 },
        { name: "y", offset: 4, datatype: 7, count: 1 },
        { name: "z", offset: 8, datatype: 7, count: 1 },
      ],
      is_bigendian: false,
      point_step: 12,
      row_step: 36,
      data: btoa(bytes),
    },
  };
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
      vehicles: ["SCOUT01"],
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
      screen.queryByRole("article", { name: "Vehicle HAWK01" }),
    ).not.toBeInTheDocument();
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

    expect(dashboardWebSockets().length).toBe(1);
    const ws1 = dashboardWebSockets()[0];

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
    expect(
      screen.queryByRole("article", { name: "Vehicle HAWK01" }),
    ).not.toBeInTheDocument();

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
    expect(dashboardWebSockets().length).toBe(2);
    const ws2 = dashboardWebSockets()[1];
    act(() => {
      ws2.triggerOpen();
    });
    expect(screen.getByText(/WS: LINKED \(LIVE PUSH\)/i)).toBeInTheDocument();
    vi.useRealTimers();
  });

  // 7. Simulation-mode status & hardware lockout
  it("confirms simulated mode and prevents real-mode requests while hardware is locked", async () => {
    const modeSpy = vi.spyOn(apiClient, "setVehicleSimulatedMode")
      .mockResolvedValue({
        vehicle_id: "SCOUT01",
        mode: "simulated",
        enabled: true,
        status: "IDLE",
        connection: "CONNECTED",
      });

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
    expect(realBtn).toBeDisabled();
    expect(modeSpy).toHaveBeenCalledTimes(1);

    // Reducer also enforces hardware lockout
    const nextState = dashboardReducer(initialDashboardState, {
      type: "SET_VEHICLE_MODE",
      payload: { vehicleId: "SCOUT01", mode: "real" },
    });
    expect(nextState.vehicles.SCOUT01.mode).toBe("simulated");
  });

  it("syncs vehicle status, raw sensors, observations, and SCOUT ROS status through their routes", async () => {
    const sensorsSpy = vi.spyOn(apiClient, "fetchVehicleSensors").mockResolvedValue({
      vehicle_id: "SCOUT01",
      status: "IDLE",
      connection: "DISCONNECTED",
      mode: "simulated",
      battery: 100,
      position_x: 0,
      position_y: 0,
      position_alt: 0,
      last_heartbeat: null,
      is_stale: true,
      sequence: 7,
      raw_sensors: { lidar_points: 12 },
    });
    const scoutStatusSpy = vi.spyOn(apiClient, "fetchScoutStatus").mockResolvedValue({
      scout_id: "SCOUT01",
      rosbridge_url: "ws://192.0.2.41:9090",
      enabled: false,
      connected: false,
      mode: "disabled",
    });
    vi.spyOn(apiClient, "fetchVehicleStatus").mockResolvedValue({
      id: "SCOUT01",
      vehicle_type: "ground",
      status: "IDLE",
      battery: 100,
      position_x: 0,
      position_y: 0,
      position_alt: 0,
      connection: "DISCONNECTED",
      mode: "simulated",
      last_heartbeat: null,
      is_stale: true,
      active_task_id: null,
    });
    vi.spyOn(apiClient, "fetchVehicleObservations").mockResolvedValue([]);

    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Sync SCOUT01 telemetry" }),
    );
    await waitFor(() => {
      expect(
        screen.getByText(/SENSOR SNAPSHOT \/\/ SEQ 7 \/\/ STALE/i),
      ).toBeInTheDocument();
    });
    expect(sensorsSpy).toHaveBeenCalledWith("SCOUT01");
    expect(apiClient.fetchVehicleStatus).toHaveBeenCalledWith("SCOUT01");
    expect(apiClient.fetchVehicleObservations).toHaveBeenCalledWith(
      "SCOUT01",
      undefined,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Sync SCOUT01 ROS status" }),
    );
    await waitFor(() => {
      expect(
        screen.getByText(/BACKEND ROS: DISABLED\s*\/\/\s*NOT LINKED/i),
      ).toBeInTheDocument();
    });
    expect(scoutStatusSpy).toHaveBeenCalledOnce();
  });

  it("archives selected SLAM map files through the map upload route with confirmation", async () => {
    const uploadSpy = vi.spyOn(apiClient, "uploadMapFiles").mockResolvedValue({
      status: "success",
      vehicle_id: "SCOUT01",
      saved_files: ["lidar_map.pcd"],
    });
    vi.stubGlobal("confirm", vi.fn(() => true));

    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );
    const file = new File(["pcd data"], "lidar_map.pcd", {
      type: "application/octet-stream",
    });
    fireEvent.change(screen.getByLabelText("Select SCOUT01 map files"), {
      target: { files: [file] },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Archive 1 selected map files" }),
    );

    await waitFor(() => {
      expect(screen.getByText("Archived: lidar_map.pcd")).toBeInTheDocument();
    });
    expect(uploadSpy).toHaveBeenCalledWith("SCOUT01", [file]);
    expect(window.confirm).toHaveBeenCalledOnce();
  });

  it("sends held keyboard input, suppresses repeats, and sends X on release or conflict", async () => {
    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    await waitFor(() => {
      expect(
        MockWebSocket.instances.some((socket) =>
          socket.url.includes("/ws/scout/control"),
        ),
      ).toBe(true);
    });
    const controlSocket = MockWebSocket.instances.find((socket) =>
      socket.url.includes("/ws/scout/control"),
    );
    if (!controlSocket) throw new Error("Missing simulated control WebSocket.");
    act(() => controlSocket.triggerOpen());
    expect(
      screen.getByRole("button", { name: "Simulate W forward" }),
    ).toBeEnabled();

    act(() => {
      fireEvent.keyDown(window, { key: "w" });
      fireEvent.keyDown(window, { key: "w", repeat: true });
    });
    expect(
      controlSocket.sentMessages.map((message) => JSON.parse(message)),
    ).toEqual([{ type: "command", command: "W" }]);

    act(() => fireEvent.keyUp(window, { key: "w" }));
    expect(JSON.parse(controlSocket.sentMessages[1])).toEqual({
      type: "command",
      command: "X",
    });

    act(() => {
      fireEvent.keyDown(window, { key: "a" });
      fireEvent.keyDown(window, { key: "d" });
    });
    expect(JSON.parse(controlSocket.sentMessages[2])).toEqual({
      type: "command",
      command: "A",
    });
    expect(JSON.parse(controlSocket.sentMessages[3])).toEqual({
      type: "command",
      command: "X",
    });
    expect(screen.getByText(/Only one movement direction may be held/i)).toBeInTheDocument();
  });

  it("encodes map archival as multipart without overriding the browser boundary header", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "success",
          vehicle_id: "SCOUT01",
          saved_files: ["lidar_map.pcd"],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const file = new File(["pcd data"], "lidar_map.pcd", {
      type: "application/octet-stream",
    });

    await expect(apiClient.uploadMapFiles("SCOUT01", [file])).resolves.toMatchObject({
      saved_files: ["lidar_map.pcd"],
    });

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe(`${apiClient.API_BASE_URL}/upload-map/SCOUT01`);
    const requestBody = init?.body;
    expect(requestBody).toBeInstanceOf(FormData);
    if (!(requestBody instanceof FormData)) {
      throw new Error("Map archive request body must be FormData.");
    }
    expect(requestBody.getAll("files")).toHaveLength(1);
    expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
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
            rationale: "[ULTRON_STUB] Assigned SCOUT01",
            resulting_task_ids: ["M001-T01"],
          },
          world_model_snapshot: {
            is_stub: true,
            resulting_task_ids: ["M001-T01"],
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

  // 10. Direct SCOUT MJPEG camera feed
  it("loads the confirmed SCOUT MJPEG URL directly and reports when the stream fails", () => {
    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    const img = screen.getByAltText("SCOUT01 direct MJPEG camera stream");
    expect(img).toHaveAttribute(
      "src",
      "http://192.0.2.41:8081/stream.mjpg",
    );
    expect(screen.getByText("DIRECT MJPEG")).toBeInTheDocument();

    const urlInput = screen.getByLabelText("SCOUT01 MJPEG Stream URL");
    fireEvent.change(urlInput, {
      target: { value: "http://192.0.2.10:8080/stream.mjpg" },
    });
    fireEvent.click(screen.getByRole("button", { name: "USE MJPEG URL" }));

    expect(img).toHaveAttribute("src", "http://192.0.2.10:8080/stream.mjpg");

    // Simulate stream load failure
    fireEvent.error(img);
    expect(
      screen.getByText(
        /CAMERA STREAM ERROR \/\/ DIRECT MJPEG FEED UNREACHABLE/i,
      ),
    ).toBeInTheDocument();
  });

  it("accumulates registered LiDAR scans and plots the SLAM path in 2D", async () => {
    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "LIVE 2D MAP" }));
    expect(screen.getByText(/Subscribing read-only to \/cloud_registered and \/path/i)).toBeInTheDocument();

    const lidarSocket = MockWebSocket.instances.find(
      (socket) => socket.url === apiClient.ROSBRIDGE_URL,
    );
    expect(lidarSocket).toBeDefined();
    if (!lidarSocket) throw new Error("ROSBridge LiDAR socket was not created");

    act(() => lidarSocket.triggerOpen());
    expect(JSON.parse(lidarSocket.sentMessages[0])).toMatchObject({
      op: "subscribe",
      topic: "/cloud_registered",
      type: "sensor_msgs/msg/PointCloud2",
    });
    expect(JSON.parse(lidarSocket.sentMessages[1])).toMatchObject({
      op: "subscribe",
      topic: "/path",
      type: "nav_msgs/msg/Path",
    });

    act(() => {
      lidarSocket.triggerMessage(makePointCloudMessage());
      lidarSocket.triggerMessage({
        op: "publish",
        topic: "/path",
        msg: {
          poses: [
            { pose: { position: { x: 0, y: 0, z: 0 } } },
            { pose: { position: { x: 2, y: 1, z: 0 } } },
          ],
        },
      });
      lidarSocket.triggerMessage(makePointCloudMessage("/cloud_registered", 3));
    });
    await waitFor(() => {
      expect(screen.getByText("LIVE")).toBeInTheDocument();
      expect(screen.getByText(/PTS 3 \/ VOXELS 6/i)).toBeInTheDocument();
      expect(screen.getByText("FRAME camera_init")).toBeInTheDocument();
      expect(screen.getByTestId("lidar-scan-canvas")).toBeInTheDocument();
      expect(
        screen.getByText(/XY VISUAL ONLY \/\/ NO NAVIGATION/i),
      ).toBeInTheDocument();
    });
  });

  it("loads the short simulated hackathon demo objective", () => {
    render(
      <DashboardProvider>
        <App enableWebSocket={false} />
      </DashboardProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "LOAD HACKATHON DEMO TASK" }));
    expect(screen.getByLabelText(/MISSION OBJECTIVE/i)).toHaveValue(
      "Simulated SCOUT01: perform one short sweep of Sector Alpha, identify visible obstacles, and report findings.",
    );
  });

  // 11 & 12. Emergency-stop confirmation, partial failure, and distinction between HTTP 200 and confirmed stopping
  it("requires confirmation for E-Stop, displays partial failure, and distinguishes HTTP acceptance from confirmed stop", async () => {
    const estopSpy = vi
      .spyOn(apiClient, "triggerEmergencyStop")
      // First call: failed stop (SCOUT01 failed/disconnected)
      .mockResolvedValueOnce({
        request_accepted: true,
        target: "ALL",
        overall_outcome: "NONE_CONFIRMED_STOPPED",
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
        /HTTP REQUEST ACCEPTED: YES \/\/ OVERALL: NONE_CONFIRMED_STOPPED/i,
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
    expect(
      screen.getByRole("note", {
        name: /ULTRON planning and safety validation run on dispatch; simulated only/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Run ULTRON Simulation Pipeline" }),
    ).not.toBeInTheDocument();

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

  // 14. ROS 2 Humble Perception integration: camera-relative oak_rgb_camera_optical_frame & oak_parent_frame,
  // unsupported/unknown frames, unconfirmed vs confirmed map frame, empty, stale & disconnected states
  it("renders oak_rgb_camera_optical_frame and oak_parent_frame in camera-relative panel without map plotting, flags unsupported frames, and distinguishes all 6 stream states", async () => {
    render(
      <DashboardProvider>
        <App enableWebSocket={true} />
      </DashboardProvider>,
    );

    expect(MockWebSocket.instances.length).toBe(1);
    const ws = MockWebSocket.instances[0];

    // State 1: Disconnected state via snapshot
    act(() => {
      ws.triggerOpen();
      ws.triggerMessage({
        type: "snapshot",
        protocol_version: "1.0",
        sequence: 1,
        payload: {
          protocol_version: "1.0",
          generated_at: new Date().toISOString(),
          current_mission: null,
          vehicles: {
            SCOUT01: {
              id: "SCOUT01",
              vehicle_type: "ground",
              status: "ACTIVE",
              battery: 90.0,
              position_x: 2.0,
              position_y: 2.0,
              position_alt: 0.0,
              connection: "CONNECTED",
              mode: "simulated",
              last_heartbeat: new Date().toISOString(),
            },
          },
          detections: {
            SCOUT01: {
              vehicle_id: "SCOUT01",
              configured_vehicle_id: "SCOUT01",
              telemetry_source: "rosbridge",
              actuation_locked: true,
              ros_connection_state: "DISCONNECTED",
              topic: "/detections_json",
              message_type: "std_msgs/msg/String",
              stamp: null,
              source_timestamp: null,
              received_at: null,
              frame: null,
              frame_classification: null,
              map_projection_allowed: false,
              objects: [],
              is_stale: false,
              stale_after_seconds: 2.0,
              has_received_frame: false,
              out_of_order_count: 0,
              invalid_message_count: 0,
            },
          },
          recent_events: [],
          recent_reasoning_traces: [],
        },
      });
    });

    expect(
      screen.getByTestId("perception-source-badge"),
    ).toHaveTextContent(/LIVE ROS PERCEPTION/i);
    expect(
      screen.getByTestId("perception-actuation-badge"),
    ).toHaveTextContent(/PHYSICAL ACTUATION LOCKED/i);
    expect(
      screen.getByTestId("perception-state-disconnected"),
    ).toBeInTheDocument();

    // State 2: Connected but awaiting first detection message
    act(() => {
      ws.triggerMessage({
        type: "detections",
        sequence: 2,
        payload: {
          vehicle_id: "SCOUT01",
          configured_vehicle_id: "SCOUT01",
          telemetry_source: "rosbridge",
          actuation_locked: true,
          ros_connection_state: "CONNECTED",
          topic: "/detections_json",
          message_type: "std_msgs/msg/String",
          stamp: null,
          source_timestamp: null,
          received_at: null,
          frame: null,
          frame_classification: null,
          map_projection_allowed: false,
          objects: [],
          is_stale: false,
          stale_after_seconds: 2.0,
          has_received_frame: false,
          out_of_order_count: 0,
          invalid_message_count: 0,
        },
      });
    });
    expect(screen.getByTestId("perception-state-awaiting")).toBeInTheDocument();

    // State 4A: Live `oak_rgb_camera_optical_frame` detections -> displayed in camera-relative panel, NEVER plotted on SVG floor map
    const nowIso = new Date().toISOString();
    act(() => {
      ws.triggerMessage({
        type: "detections",
        sequence: 3,
        payload: {
          vehicle_id: "SCOUT01",
          configured_vehicle_id: "SCOUT01",
          telemetry_source: "rosbridge",
          actuation_locked: true,
          ros_connection_state: "CONNECTED",
          topic: "/detections_json",
          message_type: "std_msgs/msg/String",
          stamp: 1793363063.737,
          source_timestamp: "2026-10-30T12:00:00Z",
          received_at: nowIso,
          frame: "oak_rgb_camera_optical_frame",
          frame_classification: "CAMERA_OPTICAL_RELATIVE",
          map_projection_allowed: false,
          objects: [
            {
              id: 0,
              class: "person",
              score: 0.95,
              position: { x: 0.12, y: -0.02, z: 0.65 },
              range_m: 0.65,
            },
          ],
          is_stale: false,
          stale_after_seconds: 2.0,
          has_received_frame: true,
          out_of_order_count: 0,
          invalid_message_count: 0,
        },
      });
    });

    expect(
      screen.getByTestId("perception-camera-optical-frame-list"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("perception-camera-optical-object-0"),
    ).toHaveTextContent(/#0 PERSON/i);
    expect(
      screen.getByTestId("perception-camera-optical-object-0"),
    ).toHaveTextContent(/CONF: 95%/i);
    expect(
      screen.getByTestId("perception-camera-optical-object-0"),
    ).toHaveTextContent(/FRAME: oak_rgb_camera_optical_frame/i);
    // Must NOT plot camera-optical detections on the global SVG floor map
    expect(
      screen.queryByTestId("map-detection-marker-0"),
    ).not.toBeInTheDocument();

    // State 4B: Frame-relative `oak_parent_frame` detections -> distinct from optical frame, NEVER plotted on SVG floor map
    act(() => {
      ws.triggerMessage({
        type: "detections",
        sequence: 4,
        payload: {
          vehicle_id: "SCOUT01",
          configured_vehicle_id: "SCOUT01",
          telemetry_source: "rosbridge",
          actuation_locked: true,
          ros_connection_state: "CONNECTED",
          topic: "/detections_json",
          message_type: "std_msgs/msg/String",
          stamp: 1793363064.5,
          source_timestamp: "2026-10-30T12:00:01Z",
          received_at: new Date().toISOString(),
          frame: "oak_parent_frame",
          frame_classification: "CAMERA_PARENT_RELATIVE",
          map_projection_allowed: false,
          objects: [
            {
              id: 3,
              class: "chair",
              score: 0.91,
              position: { x: 0.8, y: -0.2, z: 1.1 },
              range_m: 1.36,
            },
          ],
          is_stale: false,
          stale_after_seconds: 2.0,
          has_received_frame: true,
          out_of_order_count: 0,
          invalid_message_count: 0,
        },
      });
    });

    expect(
      screen.getByTestId("perception-camera-frame-list"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("perception-camera-object-3")).toHaveTextContent(
      /#3 CHAIR/i,
    );
    expect(
      screen.queryByTestId("map-detection-marker-3"),
    ).not.toBeInTheDocument();

    // State 6: Unsupported / unknown coordinate frame -> preserved verbatim and flagged with warning
    act(() => {
      ws.triggerMessage({
        type: "detections",
        sequence: 5,
        payload: {
          vehicle_id: "SCOUT01",
          configured_vehicle_id: "SCOUT01",
          telemetry_source: "rosbridge",
          actuation_locked: true,
          ros_connection_state: "CONNECTED",
          topic: "/detections_json",
          message_type: "std_msgs/msg/String",
          stamp: 1793363064.8,
          source_timestamp: "2026-10-30T12:00:01.800Z",
          received_at: new Date().toISOString(),
          frame: "custom_lidar_frame",
          frame_classification: "UNSUPPORTED_FRAME",
          map_projection_allowed: false,
          objects: [
            {
              id: 8,
              class: "cone",
              score: 0.85,
              position: { x: 1.5, y: 0.5, z: 0.0 },
              range_m: 1.58,
            },
          ],
          is_stale: false,
          stale_after_seconds: 2.0,
          has_received_frame: true,
          out_of_order_count: 0,
          invalid_message_count: 0,
        },
      });
    });
    expect(
      screen.getByTestId("perception-state-unsupported-frame"),
    ).toHaveTextContent(/UNSUPPORTED COORDINATE FRAME \[custom_lidar_frame\]/i);
    expect(
      screen.queryByTestId("map-detection-marker-8"),
    ).not.toBeInTheDocument();

    // State 3: Live stream with empty objects array (`objects: []`) -> shows 0 objects
    act(() => {
      ws.triggerMessage({
        type: "detections",
        sequence: 6,
        payload: {
          vehicle_id: "SCOUT01",
          configured_vehicle_id: "SCOUT01",
          telemetry_source: "rosbridge",
          actuation_locked: true,
          ros_connection_state: "CONNECTED",
          topic: "/detections_json",
          message_type: "std_msgs/msg/String",
          stamp: 1793363065.0,
          source_timestamp: "2026-10-30T12:00:02Z",
          received_at: new Date().toISOString(),
          frame: "oak_rgb_camera_optical_frame",
          frame_classification: "CAMERA_OPTICAL_RELATIVE",
          map_projection_allowed: false,
          objects: [],
          is_stale: false,
          stale_after_seconds: 2.0,
          has_received_frame: true,
          out_of_order_count: 0,
          invalid_message_count: 0,
        },
      });
    });
    expect(screen.getByTestId("perception-state-empty")).toBeInTheDocument();

    // State 5: Stale detection stream (`is_stale: true`) -> shows DETECTION STREAM STALE while SCOUT01 remains CONNECTED
    act(() => {
      ws.triggerMessage({
        type: "detections",
        sequence: 7,
        payload: {
          vehicle_id: "SCOUT01",
          configured_vehicle_id: "SCOUT01",
          telemetry_source: "rosbridge",
          actuation_locked: true,
          ros_connection_state: "CONNECTED",
          topic: "/detections_json",
          message_type: "std_msgs/msg/String",
          stamp: 1793363066.0,
          source_timestamp: "2026-10-30T12:00:03Z",
          received_at: "2026-10-30T11:50:00Z",
          frame: "oak_rgb_camera_optical_frame",
          frame_classification: "CAMERA_OPTICAL_RELATIVE",
          map_projection_allowed: false,
          objects: [
            {
              id: 5,
              class: "person",
              score: 0.9,
              position: { x: 2.0, y: 2.0, z: 0.5 },
              range_m: 1.0,
            },
          ],
          is_stale: true,
          stale_after_seconds: 2.0,
          has_received_frame: true,
          out_of_order_count: 0,
          invalid_message_count: 0,
        },
      });
    });
    expect(screen.getByTestId("perception-state-stale")).toHaveTextContent(
      /DETECTION STREAM STALE/i,
    );
    expect(
      screen.queryByTestId("map-detection-marker-5"),
    ).not.toBeInTheDocument();
    // SCOUT01 vehicle card remains CONNECTED (not marked UNKNOWN by stale perception)
    const scoutCard = screen.getByRole("article", { name: "Vehicle SCOUT01" });
    expect(within(scoutCard).getByText("CONNECTED")).toBeInTheDocument();
  });

  // 15. Evidence Inspector, Sensor Status Strip (LiDAR UNAVAILABLE), and ULTRON Environmental Query
  it("displays sensor status (depth camera + LiDAR UNAVAILABLE), interactive evidence inspector, and answers grounded environmental queries", async () => {
    const querySpy = vi
      .spyOn(apiClient, "queryEnvironmentalIntelligence")
      .mockResolvedValueOnce({
        answer:
          "[OBJ-001] 'person' detected at 0.65m (pos=(0.12, -0.02, 0.65)m in 'oak_rgb_camera_optical_frame').",
        facts: ["[OBJ-001] 'person' detected at 0.65m"],
        inferences: [],
        unknowns: [
          "LiDAR geometric scan and multi-sensor corroboration are UNAVAILABLE.",
        ],
        warnings: [],
        evidence: [
          {
            observation_id: "OBS-CAM-001",
            object_id: "OBJ-001",
            sensor: "depth_camera",
            sensor_type: "depth_camera",
            timestamp: 1793363063.737,
            coordinate_frame: "oak_rgb_camera_optical_frame",
            confidence: 0.95,
            epistemic_status: "OBSERVED",
            source_mode: "simulated",
          },
        ],
        confidence: "medium",
        source_mode: "simulated",
      });

    render(
      <DashboardProvider>
        <App enableWebSocket={true} />
      </DashboardProvider>,
    );

    const ws = dashboardWebSockets()[0];
    act(() => {
      ws.triggerOpen();
      ws.triggerMessage({
        type: "detections",
        sequence: 1,
        payload: {
          vehicle_id: "SCOUT01",
          configured_vehicle_id: "SCOUT01",
          telemetry_source: "rosbridge",
          actuation_locked: true,
          ros_connection_state: "CONNECTED",
          topic: "/detections_json",
          message_type: "std_msgs/msg/String",
          stamp: 1793363063.737,
          source_timestamp: "2026-10-30T12:00:00Z",
          received_at: new Date().toISOString(),
          frame: "oak_rgb_camera_optical_frame",
          frame_classification: "CAMERA_OPTICAL_RELATIVE",
          map_projection_allowed: false,
          objects: [
            {
              id: 0,
              class: "person",
              score: 0.95,
              position: { x: 0.12, y: -0.02, z: 0.65 },
              range_m: 0.65,
            },
          ],
          is_stale: false,
          stale_after_seconds: 2.0,
          has_received_frame: true,
          out_of_order_count: 0,
          invalid_message_count: 0,
        },
      });
    });

    // Verify Sensor Status Strip shows LiDAR UNAVAILABLE
    expect(screen.getByTestId("sensor-status-lidar")).toHaveTextContent(
      /UNAVAILABLE/i,
    );

    // Verify Evidence Inspector shows tracked object provenance and single-sensor status
    const inspector = screen.getByTestId("evidence-inspector");
    expect(inspector).toHaveTextContent(/OBJ-001/i);
    expect(inspector).toHaveTextContent(/STATUS: OBSERVED/i);
    expect(inspector).toHaveTextContent(/SINGLE-SENSOR ONLY/i);

    // Submit environmental query to ULTRON
    const qInput = screen.getByLabelText(
      /QUERY ULTRON ENVIRONMENTAL INTELLIGENCE/i,
    );
    fireEvent.change(qInput, { target: { value: "Where is the person?" } });
    fireEvent.click(screen.getByRole("button", { name: /ASK ULTRON/i }));

    await waitFor(() => {
      expect(screen.getByTestId("ultron-env-query-result")).toBeInTheDocument();
    });
    expect(querySpy).toHaveBeenCalledWith("Where is the person?");
    expect(screen.getByTestId("ultron-env-query-result")).toHaveTextContent(
      /0.65m/i,
    );
    expect(screen.getByTestId("ultron-env-query-result")).toHaveTextContent(
      /OBS-CAM-001/i,
    );
  });
});

