# Operation Shadow

Operation Shadow is a local, indoor multi-vehicle mission-control system coordinating two autonomous vehicles (`SCOUT01` ground rover and `HAWK01` indoor drone) with an in-process ULTRON agent adapter, a FastAPI + SQLite + WebSocket backend, and a military tactical command-and-control React + TypeScript dashboard.

## Documentation

- [Product Requirements Document — PRD v2.0](docs/Operation%20Shadow%20%E2%80%94%20PRD%20v2.0.md)
- [Technical Requirements Document — TRD v2.0](docs/Operation%20Shadow%20%E2%80%94%20TRD%20v2.0.md)
- [ULTRON Agent Architecture Specification](docs/Operation%20Shadow%20%E2%80%94%20ULTRON%20Agent%20Architecture.md)
- [Backend & Frontend Architecture Specification](docs/Operation%20Shadow%20%E2%80%94%20Backend%20%26%20Frontend%20Architecture.md)
- [UI & Tactical Design Specification](docs/ui-design-spec.md)
- [Implementation Plan & Milestones](docs/implementation-plan.md)
- [Assumptions & Architecture Reference](docs/assumptions.md)
- [Agent Guidelines](AGENTS.md)

## Repository Layout (TRD v2.0 §26)

```text
operation-shadow/
├── AGENTS.md
├── README.md
├── .env.example
├── .gitignore
├── docs/                                  # Complete product specification suite
│   ├── Operation Shadow — PRD v2.0.md
│   ├── Operation Shadow — TRD v2.0.md
│   ├── Operation Shadow — ULTRON Agent Architecture.md
│   ├── Operation Shadow — Backend & Frontend Architecture.md
│   ├── ui-design-spec.md
│   ├── implementation-plan.md
│   └── assumptions.md
├── ultron/                                # In-process ULTRON Agent System (§4, §26)
│   ├── planner/                           # LLM planning call, structured schemas, fallback cache
│   ├── memory/                            # World Model snapshot store & timeline scrubber
│   ├── world_model/                       # Shared World Model, vehicle, hazard & object entities
│   ├── replanner/                         # Cross-vehicle replanning engine & trigger evaluators
│   ├── vision/                            # Vision pipeline & observation sanitization (TRD §13)
│   ├── communication/                     # Vehicle communication channels (TRD §20)
│   ├── safety/                            # Deterministic safety validator & repair prompts (§11)
│   ├── simulation/                        # Synthetic telemetry generator & simulation harness (§13)
│   ├── graph/                             # LangGraph state machine workflow & node handlers (§12)
│   ├── schemas/                           # Pydantic v2 mission, command, and observation schemas
│   ├── config.py
│   ├── state.py
│   └── main.py                            # Standalone ULTRON runner
├── scout/                                 # SCOUT01 Ground Rover System (Raspberry Pi, TRD §8, §15)
│   ├── navigation/                        # Waypoint following + reactive obstacle avoidance (§15)
│   ├── sensors/                           # Sensor fusion (IMU, ToF, ultrasonic, temp, gas, camera)
│   ├── communication/                     # Authenticated WebSocket client & ESP32 UART bridge
│   └── main.py                            # SCOUT Pi entry point
├── hawk/                                  # HAWK01 Aerial Drone System (Companion link, TRD §9, §16)
│   ├── flight_interface/                  # Flight controller wrapper & onboard geofence/safety
│   ├── sensors/                           # Flight telemetry monitor (alt, battery) & camera stream
│   ├── communication/                     # Authenticated WebSocket companion link
│   └── main.py                            # HAWK companion entry point
├── firmware/                              # SCOUT01 Low-Level Hardware Firmware (TRD §8, §26)
│   └── esp32/
│       ├── motors/                        # PWM motor controller & emergency cutoff
│       ├── sensors/                       # Ultrasonic, ToF, temperature & gas ADC reader
│       ├── safety/                        # Hardware kill switch & watchdog safety layer
│       └── main.cpp                       # ESP32 main setup/loop entry
├── backend/                               # FastAPI C2 Backend Service (§3, §26)
│   ├── pyproject.toml
│   ├── requirements.txt
│   ├── fastapi/                           # FastAPI application modules (§3)
│   │   ├── main.py                        # FastAPI app, lifespan, /health, and /ws/dashboard
│   │   ├── api/                           # REST endpoints (/mission, /vehicle, /events, /map, /emergency-stop)
│   │   ├── websocket/                     # DashboardGateway & VehicleGatewayFastPath (§5, §10)
│   │   ├── db/                            # SQLAlchemy 2.0 ORM models & async session lifecycle (§6)
│   │   ├── schemas/                       # Pydantic v2 domain, error, and WebSocket contracts (§5, §6, §7)
│   │   ├── core/                          # Settings, security, structured logging, error handlers
│   │   └── services/                      # MissionService, VehicleService, and EventService
│   ├── ultron_stub/                       # Isolated ULTRON stub, VehicleAdapterPort & deterministic simulator
│   └── tests/                             # Backend pytest suite (29 tests across Milestones 1–6)
├── dashboard/                             # React Tactical C2 Dashboard (§8, §26)
│   └── react/                             # Vite + React + TypeScript tactical C2 dashboard
│       ├── package.json
│       ├── tsconfig.json
│       ├── vite.config.ts
│       └── src/
│           ├── App.tsx                    # Global operations bar & 3-column tactical C2 grid
│           ├── api/client.ts              # Typed REST client for all backend endpoints
│           ├── components/                # FloorMap, VehicleStatusCard, MissionPanel, CameraFeed,
│           │                              # EventLog, ReasoningTracePanel, MissionReplayScrubber,
│           │                              # SimulatedModeToggle, EmergencyStopButton
│           ├── hooks/                     # useDashboardSocket (single managed WS + reconnect resync)
│           ├── state/store.tsx            # React Context + useReducer store with sequence guards
│           ├── styles/tactical.css        # Military tactical C2 design tokens & layout
│           └── __tests__/                 # Vitest + React Testing Library test suite (8 suites)
├── configs/                               # Tactical & Scenario Configuration Files (TRD §26)
│   ├── mission_demo.json                  # Rehearsed building inspection scenario (M001)
│   ├── geofence.json                      # Venue boundaries, fixed hover points & no-fly zones
│   ├── vehicles.json                      # Vehicle hardware profiles, ports & heartbeat limits
│   └── simulation.json                    # Simulation tick rate & scripted hazard injection timing
└── tests/                                 # End-to-end and agent verification tests (TRD §26)
    ├── test_system_architecture.py        # Validates TRD §26 layout compliance
    ├── test_ultron_safety.py              # Validates Safety Validator & geofence constraints
    ├── test_ultron_replanning.py          # Validates Evaluator replanning triggers
    └── test_ultron_planner.py             # Validates fallback cache & task decomposition
```

## Implemented Backend Endpoints (Milestones 1–6)

- `GET /health` — System health, SQLite connectivity, hardware lockout status, and vehicle list.
- `POST /mission` — Create a mission via the in-process ULTRON stub and persist tasks, events, and reasoning traces.
- `GET /mission/{id}` — Retrieve full mission state and ordered tasks.
- `POST /mission/{id}/complete` — Complete an active simulated mission, finalize tasks, persist completion event and reasoning trace, and broadcast updates over `/ws/dashboard`.
- `GET /mission/{id}/replay` — Retrieve chronological replay timeline of events and reasoning traces.
- `GET /vehicle/{id}/status` — Retrieve authoritative vehicle status, battery, position, connection, mode, and `is_stale` flag.
- `GET /vehicle/{id}/sensors` — Retrieve latest raw telemetry and sensor state.
- `GET /vehicle/{id}/observations` — Retrieve chronological observations reported by a vehicle (optional `?mission_id=`).
- `POST /vehicle/{id}/command` — Dispatch a validated structured command (requires `X-Vehicle-Token` or `Authorization: Bearer` shared-secret token).
- `POST /vehicle/{id}/simulate` — Toggle vehicle simulated mode (enforces `SHADOW_HARDWARE_LOCKOUT`).
- `GET /events` — Retrieve chronological event log (optional `?mission_id=`).
- `GET /map` — Retrieve static indoor floor-plan configuration (labeled provisional simulation config).
- `POST /emergency-stop` — Dedicated Emergency Stop Fast Path (`ALL`, `SCOUT01`, or `HAWK01`) bypassing ULTRON and synchronous DB writes, with explicit per-vehicle dispatch/ack/stop-confirmation evidence and async post-dispatch audit logging.
- `WS /ws/dashboard` — Push-only dashboard WebSocket streaming an initial `snapshot` envelope followed by live `telemetry`, `event`, `reasoning_trace`, and `mission_status` envelopes.

## Quick Start

### 1. Environment Configuration
Copy `.env.example` to `.env` in the repository root (or inside `backend/`):
```bash
cp .env.example .env
```
By default, `SHADOW_HARDWARE_LOCKOUT=true` and `SHADOW_DEFAULT_VEHICLE_MODE=simulated` are enabled so no physical hardware can be controlled until hardware and protocol specifications are finalized.

### 2. Backend Setup, Database Initialization & Run
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Initialize SQLite schema (all 6 tables + idempotent SCOUT01/HAWK01 simulated seed)
PYTHONPATH=fastapi:. python3 -c "import asyncio; from db.session import init_db; asyncio.run(init_db())"

# Run the FastAPI server (automatically runs init_db() and resets any stale CONNECTED rows on startup)
PYTHONPATH=fastapi:. uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

### 3. Frontend Setup & Run
```bash
cd dashboard/react
npm install
npm run dev
```

### 4. End-to-End Simulated Mission Walkthrough
With both the FastAPI backend (`http://localhost:8000`) and the React dashboard (`http://localhost:5173`) running:
1. **Verify Link & Lockout Status**: Confirm the top Global Operations Bar displays `API: ONLINE`, `WS: LINKED (LIVE PUSH)`, and `Hardware Lockout: ACTIVE (Simulated Only)`.
2. **Submit a Simulated Mission**: In **Mission Command & Tasking**, enter an objective (e.g., `Sweep Sector Alpha and locate thermal anomaly`) and click **DISPATCH**. Observe the mission status (`ACTIVE`), assigned tasks for `SCOUT01` and `HAWK01`, the initial mission event in **Event Log**, and the stub trace in **ULTRON Reasoning Trace**.
3. **Inspect Floor Map & Vehicle Telemetry**: View the 20m × 15m indoor sector map (`FloorMap`), `HAWK01` geofence, waypoints, and vehicle status cards (`SCOUT01` and `HAWK01`).
4. **Complete Simulated Mission & Inspect Replay**: Click **COMPLETE SIMULATED MISSION** in the Mission panel (or call `POST /mission/{id}/complete`), then click **LOAD ACTIVE** or **LOAD** in **Mission Replay Scrubber** to step through the chronological event and reasoning-trace timeline.
5. **Test Emergency Stop Fast Path**: In **Emergency Stop Control**, select `ALL`, `SCOUT01`, or `HAWK01`, click **ARM E-STOP**, and click **CONFIRM E-STOP** to inspect the per-vehicle dispatch, acknowledgement, and stop-confirmation report.

### 5. Automated Testing, Type Checking & Build
All automated backend tests use isolated temporary SQLite databases (`tmp_path`) and deterministic simulated clocks (`ManualClock`):
```bash
# Backend (29 tests + strict mypy across 39 source files)
cd backend
.venv/bin/pytest -v
.venv/bin/mypy fastapi ultron_stub tests

# Frontend (TypeScript check, 8 Vitest suites, and production Vite build)
cd ../dashboard/react
npm run typecheck
npm test -- --run
npm run build
```

## Hardware Invariants & Physical Safety
- **Simulated Mode Default & Hardware Lockout**: Physical vehicle control (`mode="real"`) is strictly locked out by default (`SHADOW_HARDWARE_LOCKOUT=true` and `LockedRealVehicleAdapter`) until onboard physical failsafes and venue boundaries are confirmed.
- **Specification Suite Fully Integrated**: PRD v2.0, TRD v2.0, ULTRON Agent Architecture, and Backend & Frontend Architecture specifications are now integrated in `docs/` and reflected across `ultron/`, `scout/`, `hawk/`, `firmware/`, `backend/`, and `dashboard/`.
