# Operation Shadow — Assumptions, Missing Specifications & Provisional Decisions

## 1. Available Source Documents & Specification Status

The repository contains the authoritative specification documents:
- **Present**: `docs/Operation Shadow — Backend & Frontend Architecture.md` (Version 1.0)
- **Present**: `docs/Operation Shadow — PRD v2.0.md` (Product Requirements Document v2.0)
- **Present**: `docs/Operation Shadow — TRD v2.0.md` (Technical Requirements Document v2.0, including §26 Repository Structure)
- **Present**: `docs/Operation Shadow — ULTRON Agent Architecture.md` (Version 1.0 Agent Architecture)
- **Present**: `docs/ui-design-spec.md` (Military tactical command-and-control visual specification)
- **Present**: `docs/implementation-plan.md` (Implementation milestones and verification criteria)

The entire repository tree has been structured to adhere strictly to TRD v2.0 §26 (`ultron/`, `scout/`, `hawk/`, `firmware/esp32/`, `backend/fastapi/`, `dashboard/react/`, `configs/`, `tests/`, and `docs/`).

Hardware & physical IoT wiring details (motor driver pinouts, companion computer USB/serial ports) remain isolated behind safety controllers, while all software wire frames, WebSocket topics, and safety protocols adhere to TRD v2.0 §7, §19, and §20. Real hardware control remains guarded by `SHADOW_HARDWARE_LOCKOUT=true`.

## 2. Approved Tooling & Architectural Decisions

1. **Backend Database & ORM**:
   - **Decision**: SQLAlchemy 2.0 asynchronous ORM (`AsyncSession`, `create_async_engine`) with `aiosqlite` driver and `PRAGMA foreign_keys=ON` enforced on every SQLite connection.
2. **Frontend Stack & State Management**:
   - **Decision**: Vite + React 19 + TypeScript, using React Context and `useReducer` (`dashboard/react/src/state/store.tsx`) for shared state.
3. **Isolated ULTRON Stub & Vehicle Simulator Package (`backend/ultron_stub/`)**:
   - **Decision**: Define `UltronAgentPort` and `VehicleAdapterPort` (`typing.Protocol` interfaces) in `backend/ultron_stub/interface.py` and `backend/ultron_stub/vehicle_adapter.py`, backed by `StubUltronAgent`, `LockedRealVehicleAdapter`, and `MockFleetSimulator` (`MockVehicleAdapter` for `SCOUT01` and `HAWK01`).
4. **Python Module Path Resolution for `backend/fastapi/`**:
   - **Decision**: Preserve the exact `backend/fastapi/` directory layout from §3, omit `__init__.py` at the top level of `backend/fastapi/`, and configure `pythonpath = ["fastapi", "."]` in `backend/pyproject.toml`.

## 3. Safety & Hardware Lockout Policy

1. **Mandatory Hardware Lockout (`SHADOW_HARDWARE_LOCKOUT=true` & `LockedRealVehicleAdapter`)**:
   - By default, `SHADOW_HARDWARE_LOCKOUT` is `true` and `SHADOW_DEFAULT_VEHICLE_MODE` is `"simulated"`.
   - While `SHADOW_HARDWARE_LOCKOUT=true`, any configuration, service, or REST API attempt (`POST /vehicle/{id}/simulate` with `{"enabled": false}`) to place `SCOUT01` or `HAWK01` into `"real"` hardware mode is rejected with `409 Conflict` (`HARDWARE_LOCKOUT_ACTIVE`).
   - `LockedRealVehicleAdapter` raises `RealHardwareLockedError` on `start()`, `connect()`, or `send_command()`, and its `trigger_emergency_stop()` explicitly returns `real_hardware_available=False`, `dispatch_attempted=False`, `vehicle_confirmed_stopped=False`, reminding operators to use the physical vehicle kill switch and onboard hardware failsafes.

## 4. Milestone 2 & 3A Persistence, Schema & Simulation Decisions

1. **Nullable / Optional Columns & Value Range Constraints**:
   - `VEHICLES.battery` in `[0.0, 100.0]`, `OBSERVATIONS.confidence` in `[0.0, 1.0]`, `TASKS.priority` in `[1, 100]`, and `VEHICLES.mode` in `('real', 'simulated')`.
2. **Configurable Heartbeat Timing & Stale Telemetry Filtering**:
   - Provisional defaults are configurable via `SHADOW_HEARTBEAT_INTERVAL_MS=250` and `SHADOW_HEARTBEAT_TIMEOUT_MS=1500` using an injectable `ClockPort`. Out-of-order or expired telemetry frames are rejected so stale frames never overwrite current state or falsely recover an `UNKNOWN` vehicle.
3. **SQLite Persistence Boundary (`VehicleService`)**:
   - Every valid telemetry heartbeat updates the in-process World Model and broadcasts to `DashboardGateway` immediately, while SQLite `vehicles` table writes occur immediately on state transitions (`CONNECTED` / `DISCONNECTED` / `UNKNOWN`, `status`, `mode`) and every `persist_every_n_heartbeats` (default `5`) for routine position/battery ticks.

## 5. Milestone 3B & Milestone 4 REST, WebSocket & Emergency-Stop Decisions

1. **Dashboard WebSocket Snapshot-to-Live Synchronization (`websocket/dashboard_gateway.py`)**:
   - Clients connecting to `/ws/dashboard` buffer concurrent broadcasts during snapshot construction, receive `DashboardSnapshotEnvelope` (`type="snapshot"`) first, deduplicate events/traces already captured in the snapshot, and flush remaining buffered updates in FIFO order.
   - Slow clients exceeding `max_client_queue_size=64` or `send_timeout_seconds=1.0` are evicted without blocking healthy clients.
2. **Emergency Stop Fast Path (`POST /emergency-stop` & `websocket/vehicle_gateway.py`)**:
   - **Bypass Invariant (§10)**: `VehicleGatewayFastPath.dispatch_emergency_stop()` invokes `adapter.trigger_emergency_stop()` directly on `SCOUT01`, `HAWK01`, or both (`ALL`), completely bypassing ULTRON planning/replanning, normal command queues (`send_command`), and synchronous SQLite writes.
   - **Independent Multi-Vehicle Dispatch**: When `ALL` is targeted, dispatch is attempted to `SCOUT01` and `HAWK01` in independent `try ... except` blocks so a transport failure on one vehicle never prevents stopping the other.
   - **Explicit Evidence Separation**: `EmergencyStopResponse` and `VehicleEmergencyStopOutcome` explicitly separate `request_accepted`, `dispatch_attempted`, `dispatch_succeeded`, `vehicle_acknowledged`, `vehicle_confirmed_stopped`, `is_simulated`, and `real_hardware_available`. A `200 OK` HTTP response never implies that a `DISCONNECTED`, `UNKNOWN`, or locked real-hardware vehicle has stopped.
   - **Asynchronous Post-Dispatch Logging**: SQLite vehicle status updates and `SAFETY` event logging (`EVENTS` table when a mission exists) are scheduled via background `asyncio.Task`s *after* dispatch completes. Database errors are caught and logged without affecting the fast-path dispatch result, and pending tasks are awaited/cleaned up on application shutdown.

## 6. Milestone 5 & Milestone 6 Dashboard, Cold-Boot Safety & End-to-End Validation Decisions

1. **Cold-Boot Stale Connection Reset (`db/session.py:seed_default_vehicles`)**:
   - When `init_db()` runs on application startup, any existing `Vehicle` rows in SQLite whose `connection` was persisted as `"CONNECTED"` by a prior process are automatically reset to `connection="DISCONNECTED"` (and `status="IDLE"` if previously `"ACTIVE"` or `"EXECUTING"`). This guarantees that restarting the backend never leaves stale connections reported as healthy before a fresh heartbeat is received.
2. **Simulated Mission Completion Helper (`POST /mission/{id}/complete`)**:
   - Because the real ULTRON autonomous completion engine is not yet specified (ULTRON Agent Architecture Document is missing), `MissionService.complete_mission()` and `POST /mission/{id}/complete` (along with the **Complete Simulated Mission** button in `MissionPanel.tsx`) are provided as an explicit simulation-only helper to transition an active simulated mission to `COMPLETED`, mark its assigned tasks `COMPLETED`, record a completion event and stub reasoning trace, and broadcast updates over `/ws/dashboard`.
3. **Measured Performance & Latency Scope (`test_milestone6_e2e_and_load.py`)**:
   - **Telemetry Ingest-to-Dashboard Broadcast Latency** (`n = 50` updates across 2 simulated vehicles at 250ms cadence with active WebSocket subscriber): **median `0.305 ms`, max `1.249 ms`** (well below the `< 500 ms` target in §11).
   - **Simulated Emergency-Stop Fast-Path Dispatch Latency** (`n = 20` runs targeting `ALL` vehicles): **median `0.018 ms`, max `0.033 ms`** in-process handler-to-adapter dispatch, and **median `0.276 ms`, max `4.844 ms`** full ASGI HTTP roundtrip (well below the `< 100 ms` target in §10 and §11).
   - **Excluded Physical Scope**: These measurements cover backend Python/ASGI execution, SQLite checkpointing, and WebSocket frame delivery on the local machine; they exclude physical Wi-Fi radio propagation, ESP32/Raspberry Pi companion-computer serial links, and physical motor/ESC braking.
