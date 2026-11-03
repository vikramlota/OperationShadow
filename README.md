# Operation Shadow

Operation Shadow is a local robotics mission-control and environmental intelligence platform powered by **ULTRON**, coordinating the **SCOUT01** ground rover through a FastAPI + SQLite + WebSocket backend, read-only ROS 2 Humble perception integration (`/detections_json`), and a real-time React + TypeScript operations dashboard. The **HAWK01** aerial drone from the original v2.0 concept remains **simulated / roadmap, not integrated**.

---

## System Overview & Current Capabilities

### 1. What does this project do?
Operation Shadow accepts natural-language mission objectives from a human operator, decomposes them into validated robotics tasks via the in-process **ULTRON** agent, monitors environmental observations and system events, replans when conditions change, records immutable World Model snapshots for post-mission replay, and generates deterministic After-Action Mission Reports.

### 2. How does ULTRON interact with SCOUT01?
- **In-Process Intelligence (`ultron/` & `backend/fastapi/services/ultron_agent_adapter.py`)**: ULTRON runs inside the FastAPI process using a LangGraph state machine (`plan -> validate -> assign`), a centralized `EventRouter`, deterministic **Standing Orders** (`configs/standing_orders.json`), bounded **LLM Event Triage** (`SHADOW_LLM_TRIAGE_TIMEOUT_SEC=3`), and bounded **Gemini Mission Planning** (`SHADOW_LLM_PLANNING_TIMEOUT_SEC=10`, `SHADOW_LLM_REPLANNING_TIMEOUT_SEC=8`) with a shared 30-second circuit breaker and deterministic evidence-grounded fallback plans.
- **Deterministic Safety Validation (`ultron/safety/validator.py`)**: Every proposed plan or replan is checked against `SCOUT01` availability, connection health, battery thresholds (`> 20%`), coordinate-frame provenance, and Emergency Stop state before any task is assigned. LLMs never directly control motors or actuators.
- **Default Simulation & Hardware Lockout**: By default, `SHADOW_HARDWARE_LOCKOUT=true` and `SHADOW_DEFAULT_VEHICLE_MODE=simulated` keep `SCOUT01` in simulation.

### 3. What does the operator dashboard provide?
The React + TypeScript operations console (`frontend/src/`) provides a high-density, low-glare tactical operations interface with:
- **Global Operations Bar & Fast-Path Emergency Stop (`EmergencyStopButton.tsx`)**: Displays mission status, API/WebSocket link state, simulation/lockout posture, and a two-step confirmation Emergency Stop button (`POST /emergency-stop`) that bypasses ULTRON and synchronous DB writes.
- **Mission Command, Tasking & After-Action Reports (`MissionPanel.tsx`)**: Dispatches missions, triggers simulated completion (`POST /mission/{id}/complete`), and displays/exports deterministic **After-Action Mission Reports** (`GET /mission/{id}/report`) in Markdown or JSON.
- **Spatial Floor Map & Live 2D LiDAR View (`FloorMap.tsx`, `LidarScanView.tsx`)**: Renders the provisional indoor floor plan (`GET /map`), `SCOUT01` position and breadcrumb trail, confirmed map-frame observations, SLAM map file archival (`/upload-map/SCOUT01`), and a read-only browser ROSBridge 2D point-cloud view (`/cloud_registered`, `/path`).
- **Optical Feed, Telemetry & ROS Perception Inspector (`CameraFeed.tsx`, `VehicleStatusCard.tsx`, `PerceptionPanel.tsx`)**: Displays direct MJPEG or ROS camera streams, `SCOUT01` battery/pose/heartbeat readouts, camera-relative detections (`oak_rgb_camera_optical_frame`, `oak_parent_frame`), epistemic evidence provenance (`OBSERVED`, `STALE`, `CONFLICTED`, `UNKNOWN`), and grounded environmental Q&A (`POST /mission/intelligence/query`).
- **Mission Replay Scrubber, Event Log & Reasoning Trace (`MissionReplayScrubber.tsx`, `EventLog.tsx`, `ReasoningTracePanel.tsx`)**: Scrubs chronological mission events and ULTRON decisions (`GET /mission/{id}/replay`), showing recorded `BEFORE` and `AFTER` World Model snapshots or explicit unavailable markers.

### 4. What functionality works in simulation?
With default settings (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, and `GEMINI_API_KEY` optional/unset), the entire end-to-end loop runs offline in simulation:
- Mission creation, task assignment, deterministic fallback planning, Standing Orders, LLM triage fallback, simulated `SCOUT01` telemetry/keyboard control (`/ws/scout/control`), Emergency Stop fast-path dispatch, persistent `MissionMemorySnapshot` recording, chronological Mission Replay, and deterministic After-Action Report generation.

### 5. What has actually been verified on physical hardware?
- **Read-Only ROS 2 Humble Perception (`RosBridgeAdapter`)**: Subscribing over WebSocket (`ws://<rover_ip>:9090`) to `/detections_json` (`std_msgs/msg/String`) in coordinate frame `oak_rgb_camera_optical_frame` (parent `oak_parent_frame`), plus direct MJPEG camera streaming (`:8081/stream.mjpg`) and read-only browser visualization of `/cloud_registered` and `/path`.
- **ESP32 One-Byte Serial Transmitter Protocol (`ScoutSerialWriter`)**: The hardware team's ESP-NOW transmitter accepts single uppercase ASCII bytes (`W`, `A`, `S`, `D`, `X`) with no newline at `115200` baud. Serial output remains disabled by default behind four independent safety gates (`SHADOW_HARDWARE_LOCKOUT`, `SHADOW_DEFAULT_VEHICLE_MODE`, `SHADOW_SCOUT_SERIAL_ENABLED`, `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED`).

### 6. What remains future work or unverified on hardware?
- **HAWK01 Aerial Vehicle**: Simulated / roadmap only (`hardware/hawk/` and `backend/ultron_stub/` stubs); not integrated into the active ULTRON runtime or dashboard.
- **Physical USB-Loss Motor Failsafe**: The transmitter/receiver firmware must independently halt motors on serial/radio loss before `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=true` may be set by a human operator (see [docs/SUPERVISED_REAL_MODE.md](docs/SUPERVISED_REAL_MODE.md)).
- **Global Map Transform & Camera-to-LiDAR Extrinsics**: The `map -> odom -> oak_parent_frame -> oak_rgb_camera_optical_frame` TF chain (`SHADOW_ROS_MAP_FRAME_CONFIRMED=false`) and LiDAR World Model ingestion (`SHADOW_LIDAR_ENABLED=false`) remain unconfirmed; camera detections stay strictly camera-relative.
- **Onboard Thermal & Gas Telemetry**: Unimplemented on `SCOUT01` and explicitly reported as `N/A (UNIMPLEMENTED)`.

---

## Documentation

- [Canonical Product Language & Terminology Guide](docs/PRODUCT_LANGUAGE.md)
- [Phase 4 Language Audit](docs/PHASE4_LANGUAGE_AUDIT.md)
- [Phase 3 Report — Mission Memory, Replay & After-Action Intelligence](docs/PHASE3_REPORT.md)
- [Mission Memory Architecture](docs/MISSION_MEMORY.md)
- [Mission Replay Architecture](docs/MISSION_REPLAY.md)
- [After-Action Mission Reports](docs/AFTER_ACTION_REPORTS.md)
- [Phase 2 Report — Event-Driven Intelligence](docs/PHASE2_REPORT.md)
- [Event Router Specification](docs/EVENT_ROUTER.md)
- [Standing Orders Specification](docs/STANDING_ORDERS.md)
- [LLM Event Triage Specification](docs/LLM_TRIAGE.md)
- [Phase 1 Report — Stabilization, Timeouts & Signal Audit](docs/PHASE1_REPORT.md)
- [Supervised Real Mode Safety Checklist](docs/SUPERVISED_REAL_MODE.md)
- [Live Rover ROSBridge Perception Integration](docs/rosbridge-integration.md)
- [UI & Operations Console Design Specification](docs/ui-design-spec.md)
- [Implementation Plan & Milestones](docs/implementation-plan.md)
- [Assumptions & Architecture Reference](docs/assumptions.md)
- [Agent Guidelines](AGENTS.md)
- **Historical Versioned Specifications**:
  - [Product Requirements Document — PRD v2.0](docs/Operation%20Shadow%20%E2%80%94%20PRD%20v2.0.md)
  - [Technical Requirements Document — TRD v2.0](docs/Operation%20Shadow%20%E2%80%94%20TRD%20v2.0.md)
  - [ULTRON Agent Architecture Specification](docs/Operation%20Shadow%20%E2%80%94%20ULTRON%20Agent%20Architecture.md)
  - [Backend & Frontend Architecture Specification](docs/Operation%20Shadow%20%E2%80%94%20Backend%20%26%20Frontend%20Architecture.md)

---

## Repository Layout (Segregated Architecture)

```text
operation-shadow/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── Makefile
├── .env.example
├── .gitignore
├── frontend/                              # Segregated React Mission-Control & Operations Dashboard (§8, §26)
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── src/
│       ├── App.tsx                        # Global operations bar & 3-column operations console grid
│       ├── api/client.ts                  # Typed REST client for all backend endpoints
│       ├── components/                    # FloorMap, VehicleStatusCard, PerceptionPanel, MissionPanel,
│       │                                  # CameraFeed, EventLog, ReasoningTracePanel,
│       │                                  # MissionReplayScrubber, SimulatedModeToggle, EmergencyStopButton
│       ├── hooks/                         # useDashboardSocket (single managed WS + reconnect resync)
│       ├── state/store.tsx                # React Context + useReducer store with sequence & stamp guards
│       ├── styles/tactical.css            # Tactical operations-console design tokens & layout
│       └── __tests__/                     # Vitest + React Testing Library test suite
├── backend/                               # Segregated FastAPI Mission-Control Backend Service (§3, §26)
│   ├── pyproject.toml
│   ├── requirements.txt
│   ├── fastapi/                           # FastAPI application modules (§3)
│   │   ├── main.py                        # FastAPI app, lifespan, /health, and /ws/dashboard
│   │   ├── api/                           # REST endpoints (/mission, /vehicle, /events, /map, /emergency-stop)
│   │   ├── websocket/                     # DashboardGateway & VehicleGatewayFastPath (§5, §10)
│   │   ├── db/                            # SQLAlchemy 2.0 ORM models (incl. MissionSnapshot) & async session (§6)
│   │   ├── schemas/                       # Pydantic v2 domain, detections, report, error, and WebSocket contracts
│   │   ├── core/                          # Settings (incl. SHADOW_ROS_*), security, logging, error handlers
│   │   └── services/                      # MissionService, VehicleService, EventService,
│   │                                      # PerceptionService, and RosBridgeAdapter
│   ├── ultron_stub/                       # Isolated ULTRON stub, VehicleAdapterPort & deterministic simulator
│   └── tests/                             # Backend pytest suite (Milestones 1–7 + Phases 1–3 regressions)
├── hardware/                              # Dedicated Robotics & Embedded Hardware Layer
│   ├── scout/                             # SCOUT01 Ground Rover System (Raspberry Pi, TRD §8, §15)
│   │   ├── navigation/                    # Waypoint following + reactive obstacle avoidance (§15)
│   │   ├── sensors/                       # Sensor fusion (IMU, ToF, ultrasonic, temp, gas, camera)
│   │   ├── communication/                 # Authenticated WebSocket client & provisional UART bridge
│   │   └── main.py                        # SCOUT Pi entry point
│   ├── hawk/                              # HAWK01 Aerial Drone System (simulated / roadmap, not integrated; TRD §9, §16)
│   │   ├── flight_interface/              # Fixed hover-point & waypoint Flight Controller wrapper stub
│   │   ├── sensors/                       # IMU, barometer & critical battery RTL monitor stub
│   │   ├── communication/                 # Authenticated WebSocket telemetry & command client stub
│   │   └── main.py                        # HAWK companion entry point stub
│   └── firmware/                          # SCOUT01 Low-Level Hardware Firmware (TRD §8, §26)
│       └── esp32/
│           ├── motors/                    # PWM motor controller & emergency cutoff
│           ├── sensors/                   # Ultrasonic, ToF, temperature & gas ADC reader
│           ├── safety/                    # Hardware kill switch & watchdog safety layer
│           └── main.cpp                   # ESP32 main setup/loop entry
├── ultron/                                # In-process ULTRON Mission Intelligence Agent (§4, §26)
│   ├── events/                            # Phase 2 EventRouter, StandingOrderRegistry & LLM Event Triage
│   ├── planner/                           # Bounded Gemini planning call, grounding, fallback cache
│   ├── memory/                            # Phase 3 immutable MissionMemorySnapshot store & timeline
│   ├── world_model/                       # Authoritative World Model, multi-sensor fusion & read-only tools
│   ├── replanner/                         # Replanning trigger evaluators
│   ├── vision/                            # Depth-camera & LiDAR ingestion adapters (TRD §13)
│   ├── communication/                     # Vehicle communication channels (TRD §20)
│   ├── safety/                            # Deterministic safety validator & repair prompts (§11)
│   ├── simulation/                        # Synthetic telemetry generator & simulation harness (§13)
│   ├── graph/                             # LangGraph state machine workflow & node handlers (§12)
│   ├── schemas/                           # Pydantic v2 mission, command, world, and observation schemas
│   ├── config.py
│   ├── state.py
│   └── main.py                            # Standalone ULTRON runner
├── configs/                               # Operational & Scenario Configuration Files (TRD §26)
│   ├── standing_orders.json               # Declarative Standing Orders catalog (SO-01 .. SO-04)
│   ├── mission_demo.json                  # Rehearsed building inspection scenario (M001)
│   ├── geofence.json                      # Venue boundaries & provisional geofence config
│   ├── vehicles.json                      # Vehicle profiles (SCOUT01 active; HAWK01 simulated/roadmap)
│   └── simulation.json                    # Simulation tick rate & scripted hazard injection timing
├── docs/                                  # Product, architecture, safety & phase documentation suite
└── tests/                                 # ULTRON agent, Event Intelligence, Mission Memory & layout tests
```

## Implemented Backend Endpoints (Milestones 1–7 & Phases 1–3)

- `GET /health` — System health, SQLite connectivity, hardware lockout status, active vehicle list (`["SCOUT01"]`), and ROSBridge connection status (`ros_enabled`, `ros_connection_state`, `ros_vehicle_id`).
- `POST /mission` — Create a mission via the in-process ULTRON agent and persist tasks, events, reasoning traces, and initial World Model snapshots.
- `GET /mission/{id}` — Retrieve full mission state and ordered tasks.
- `POST /mission/{id}/complete` — Complete an active simulated mission, finalize tasks (`SIMULATED_COMPLETED`), persist completion event, reasoning trace, and snapshots, and broadcast updates over `/ws/dashboard`.
- `POST /mission/{id}/execute` — Execute a mission's planned tasks on `SCOUT01` (simulated dead-reckoning fallback when hardware lockout is active; supervised ESP32 serial steps only when all four hardware safety gates are explicitly cleared).
- `GET /mission/{id}/replay` — Retrieve chronological replay timeline of events and reasoning traces paired with historical `before_state` and `after_state` World Model snapshots (or `historical_state_available: false` when unrecorded).
- `GET /mission/{id}/report` — Generate a deterministic structured After-Action Mission Report (`?format=json|markdown`) from persisted records without calling an LLM.
- `GET /mission/intelligence/snapshot` and `POST /mission/intelligence/query` — Inspect ULTRON's evidence-grounded World Model snapshot and answer environmental queries with explicit observation citations.
- `GET /vehicle/{id}/status` — Retrieve authoritative vehicle status, battery, position, connection, mode, and `is_stale` flag.
- `GET /vehicle/{id}/sensors` — Retrieve latest raw telemetry and sensor state.
- `GET /vehicle/{id}/detections` — Retrieve latest validated ROSBridge object-detection state (`frame`, `objects`, `stamp`, `received_at`, `is_stale`, `ros_connection_state`, `actuation_locked`).
- `GET /vehicle/{id}/observations` — Retrieve chronological observations reported by a vehicle (optional `?mission_id=`).
- `POST /vehicle/{id}/command` — Dispatch a validated structured command (requires `X-Vehicle-Token` or `Authorization: Bearer` shared-secret token).
- `POST /vehicle/{id}/simulate` — Toggle vehicle simulated mode (enforces `SHADOW_HARDWARE_LOCKOUT`).
- `GET /events` — Retrieve chronological event log (optional `?mission_id=`).
- `GET /map` and `POST /upload-map/{vehicle_id}` — Retrieve static indoor floor-plan configuration (labeled provisional simulation config) and archive `.ply`, `.pcd`, or `.db` SLAM map files.
- `POST /emergency-stop` — Dedicated Emergency Stop Fast Path (`ALL` or `SCOUT01`) bypassing ULTRON and synchronous DB writes, with explicit per-vehicle dispatch/ack/stop-confirmation evidence and async post-dispatch audit logging.
- `WS /ws/dashboard` — Push-only dashboard WebSocket streaming an initial `snapshot` envelope (including `detections`) followed by live `telemetry`, `detections`, `event`, `reasoning_trace`, and `mission_status` envelopes.
- `GET /scout/control/status` and `WS /ws/scout/control` — W/A/S/D/X keyboard control. By default this is simulation-only (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_SCOUT_SERIAL_ENABLED=false`, `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false`). Optional USB serial output writes one uppercase ASCII command byte with no newline at 115200 baud, matching the hardware-team transmitter sketch. Serial remains disabled unless serial output is enabled, real vehicle mode is selected, hardware lockout is disabled, and the USB-loss failsafe is verified by a human (see [docs/SUPERVISED_REAL_MODE.md](docs/SUPERVISED_REAL_MODE.md)).

The serial writer does not change transmitter firmware and does not guarantee a stop after USB loss. The provided firmware continues broadcasting its last motor command if serial input stops; therefore keep the serial and hardware gates disabled until an independent transmitter/receiver failsafe on USB loss is verified. Do not use the provisional `hardware/scout/communication/serial_bridge.py` motor frame as this transmitter's protocol.

Frontend keepalive rate can be set with `VITE_SCOUT_CONTROL_RATE_HZ` in `frontend/.env` (5–20 Hz; default 10 Hz). In an explicitly enabled serial mode, keepalives retransmit the held one-byte movement command; lease expiry, browser disconnect, and backend shutdown attempt to send `X` while USB serial remains available.

### Keyboard Control Verification and Hardware Gate

1. **Backend-to-transmitter serial:** Use a USB serial loopback or mock serial port to verify exact bytes before connecting the transmitter. The backend sends no newline.
2. **W/A/S/D/X serial bytes:** Verify `b"W"`, `b"A"`, `b"S"`, `b"D"`, and `b"X"` at 115200 baud; backend lease keepalives repeat the current movement byte.
3. **ESP-NOW receiver forwarding:** Only test with the wheels lifted and motor power safely disabled, then verify behavior with the hardware team.
4. **Dashboard controls:** With default safety settings, wait for `SIMULATOR LINKED`, hold W/A/S/D, and verify `CURRENT` changes. Release and verify `CURRENT: X`. In authorized serial mode, status reports transmitter availability and controls are disabled when it is offline.
5. **Release/disconnect safety:** Verify X on release, emergency stop, browser disconnect, and backend shutdown. These send attempts do not guarantee stopping after USB loss; the firmware/receiver must independently stop the motors on missing serial input.

Keep `SHADOW_HARDWARE_LOCKOUT=true`, serial disabled, and the failsafe-verification flag false until the independent USB-loss failsafe is verified. Do not use live serial motion until the hardware safety gate is formally cleared.

Troubleshooting: check `GET /scout/control/status` and the browser connection to `/ws/scout/control`. In default mode, the panel identifies **SIMULATION ONLY** and `transmitter_connected: false` is expected. A failed serial write reports an error and marks the transmitter offline; it cannot send X over a disconnected port.

## Quick Start

### 1. Environment Configuration
Copy `.env.example` to `.env` in the repository root (or inside `backend/`):
```bash
cp .env.example .env
```
By default, `SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_SCOUT_SERIAL_ENABLED=false`, `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false`, and `SHADOW_ROS_ENABLED=false` are configured so physical hardware control remains locked out and the simulated `SCOUT01` environment works out of the box. Note that a developer's local `.env` may differ from `.env.example` and must be checked before starting the backend.

### 2. Backend Setup, Database Initialization & Run
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Initialize SQLite schema (all 7 tables incl. mission_snapshots + idempotent SCOUT01 simulated seed)
PYTHONPATH=fastapi:.:.. python3 -c "import asyncio; from db.session import init_db; asyncio.run(init_db())"

# Run the FastAPI server (simulated mode default)
PYTHONPATH=fastapi:.:.. uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

To enable live read-only ROSBridge perception from `SCOUT01` on the same local network (`ws://<rover_ip>:9090`, topic `/detections_json`), see [docs/rosbridge-integration.md](docs/rosbridge-integration.md):
```bash
cd backend
SHADOW_ROS_ENABLED=true \
SHADOW_ROS_HOST=<rover_ip> \
SHADOW_ROS_PORT=9090 \
SHADOW_ROS_VEHICLE_ID=SCOUT01 \
SHADOW_HARDWARE_LOCKOUT=true \
PYTHONPATH=fastapi:.:.. .venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

### 3. Frontend Setup & Run
```bash
cd frontend
npm install
npm run dev
```

### 4. Automated Testing, Type Checking & Build
All automated backend, ULTRON, and ROSBridge tests use isolated temporary SQLite databases (`tmp_path`), mocked ROSBridge clients (`FakeRosClient`/`FakeRosTopic`), and deterministic simulated clocks (`ManualClock`):
```bash
# Run all verification suites from the repository root
make test-architecture
make test-ultron
make test-backend
make test-frontend

# Or run individual suites directly:
cd backend
PYTHONPATH=fastapi:.:.. .venv/bin/pytest -v
PYTHONPATH=fastapi:.:.. .venv/bin/mypy fastapi ultron_stub tests

cd ..
PYTHONPATH=.:backend:backend/fastapi backend/.venv/bin/pytest tests -v

cd frontend
npm run typecheck
npm test -- --run
npm run build
```

## Hardware Invariants & Physical Safety
- **Simulated Mode Default & Hardware Lockout**: Physical vehicle control (`mode="real"`) is strictly locked out by default (`SHADOW_HARDWARE_LOCKOUT=true` and `LockedRealVehicleAdapter`) until onboard physical failsafes and venue boundaries are confirmed.
- **Read-Only ROSBridge Perception**: Enabling `SHADOW_ROS_ENABLED=true` subscribes exclusively to `/detections_json` (`std_msgs/String`) for live perception and **never** publishes ROS messages, calls ROS services, or enables physical motor actuation.
