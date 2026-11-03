# Operation Shadow — Assumptions, Implementation Status & Architectural Decisions

**Canonical Terminology Reference:** [docs/PRODUCT_LANGUAGE.md](./PRODUCT_LANGUAGE.md)

## 1. Current Implementation Status vs. Original Design Assumptions

Operation Shadow evolved from an initial two-vehicle hackathon concept (**PRD v2.0** and **TRD v2.0**, which proposed both a `SCOUT01` ground rover and a `HAWK01` indoor aerial drone) into a **SCOUT01-centered robotics mission-control and environmental intelligence platform**:

| Capability Area | Original v2.0 Assumption | Current Active Implementation Status (Phases 1–4) |
| --- | --- | --- |
| **Active Vehicle Fleet** | Two integrated vehicles (`SCOUT01` ground rover + `HAWK01` aerial drone). | **`SCOUT01` is the sole active integrated vehicle** in ULTRON, the FastAPI vehicle service, and the React operations dashboard (`VehicleId = "SCOUT01"`). **`HAWK01` is simulated / roadmap, not integrated** (retained only in `hardware/hawk/` roadmap stubs, `backend/ultron_stub/` legacy simulator adapters, and the `hawk_geofence` schema compatibility field). |
| **Product Identity & UI** | Framed as a multi-vehicle military tactical C2 workstation. | Framed accurately as a **local robotics mission-control and operations console**, preserving the high-density dark tactical operations-console visual design (`frontend/src/styles/tactical.css`). |
| **ULTRON Intelligence** | Synchronous LLM planning and cross-vehicle replanning. | In-process LangGraph agent with bounded Gemini wall-clock timeouts (`10s` plan / `8s` replan / `3s` triage), a 30s circuit breaker, objective-preserving deterministic fallback plans (`USED CACHED PLAN`), a priority `EventRouter`, declarative **Standing Orders** (`SO-01`..`SO-04`), bounded async **LLM Event Triage**, immutable **Mission Memory** snapshots (`mission_snapshots`), and deterministic **After-Action Reports** (`GET /mission/{id}/report`). |
| **Hardware Execution & Safety** | Per-vehicle real/simulated mode toggle. | **Hardware lockout is active by default** (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_SCOUT_SERIAL_ENABLED=false`, `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false`). Physical serial output uses the hardware team's one-byte `W`/`A`/`S`/`D`/`X` (115200 baud, no newline) ESP-NOW transmitter protocol and remains disabled unless all four safety gates are explicitly cleared by a human operator ([docs/SUPERVISED_REAL_MODE.md](./SUPERVISED_REAL_MODE.md)). |
| **Rover Perception** | Laptop-side YOLO detection on SCOUT/HAWK video streams. | Read-only ROS 2 Humble `rosbridge_suite` integration (`ws://<rover_ip>:9090`) subscribing to `/detections_json` (`std_msgs/msg/String`) in camera-relative frame `oak_rgb_camera_optical_frame` (parent `oak_parent_frame`), plus direct browser MJPEG/ROS video and read-only 2D LiDAR scan visualization (`/cloud_registered`, `/path`). |
| **Coordinate Frames & Sensors** | Global `map` coordinates and onboard temperature/gas sensors assumed available. | Global `map -> odom -> oak_parent_frame` transform (`SHADOW_ROS_MAP_FRAME_CONFIRMED=false`) and LiDAR World Model ingestion (`SHADOW_LIDAR_ENABLED=false`) are **unconfirmed**; camera detections remain strictly camera-relative and are never plotted on the global floor map by default. Onboard thermal and gas telemetry are **unimplemented** (`N/A`). |

---

## 2. Available Source Documents & Specification Status

The repository contains both the current implementation documentation and the historical specification documents:
- **Canonical Product Language**: `docs/PRODUCT_LANGUAGE.md`
- **UI & Operations Console Design Specification**: `docs/ui-design-spec.md`
- **Implementation Plan & Milestones**: `docs/implementation-plan.md`
- **Live Rover ROSBridge Perception Integration**: `docs/rosbridge-integration.md`
- **Supervised Real Mode Checklist**: `docs/SUPERVISED_REAL_MODE.md`
- **Phase 1–4 Engineering Reports**: `docs/PHASE1_REPORT.md`, `docs/PHASE2_REPORT.md`, `docs/PHASE3_REPORT.md`, `docs/PHASE4_REPORT.md`
- **Historical Versioned Specifications (Preserved for Architectural Context)**:
  - `docs/Operation Shadow — Backend & Frontend Architecture.md` (Version 1.0)
  - `docs/Operation Shadow — ULTRON Agent Architecture.md` (Version 1.0)
  - `docs/Operation Shadow — PRD v2.0.md` (Product Requirements Document v2.0)
  - `docs/Operation Shadow — TRD v2.0.md` (Technical Requirements Document v2.0, including §26 Repository Structure)

The repository directory structure adheres to TRD v2.0 §26 (`ultron/`, `hardware/scout/`, `hardware/hawk/`, `hardware/firmware/esp32/`, `backend/fastapi/`, `frontend/`, `configs/`, `tests/`, and `docs/`).

---

## 3. Approved Tooling & Architectural Decisions

1. **Backend Database & ORM**:
   - **Decision**: SQLAlchemy 2.0 asynchronous ORM (`AsyncSession`, `create_async_engine`) with `aiosqlite` driver and `PRAGMA foreign_keys=ON` enforced on every SQLite connection, persisting 7 tables (`missions`, `tasks`, `vehicles`, `observations`, `events`, `reasoning_traces`, and append-only `mission_snapshots`).
2. **Frontend Stack & State Management**:
   - **Decision**: Vite + React 19 + TypeScript, using React Context and `useReducer` (`frontend/src/state/store.tsx`) for shared state.
3. **Isolated ULTRON Stub & Vehicle Simulator Package (`backend/ultron_stub/`)**:
   - **Decision**: Define `UltronAgentPort` and `VehicleAdapterPort` (`typing.Protocol` interfaces) in `backend/ultron_stub/interface.py` and `backend/ultron_stub/vehicle_adapter.py`, backed by `StubUltronAgent`, `LockedRealVehicleAdapter`, and `MockFleetSimulator` (retaining `MockVehicleAdapter` instances for `SCOUT01` and legacy stub `HAWK01` compatibility).
4. **Python Module Path Resolution for `backend/fastapi/`**:
   - **Decision**: Preserve the exact `backend/fastapi/` directory layout from §3, omit `__init__.py` at the top level of `backend/fastapi/`, and configure `pythonpath = ["fastapi", "."]` in `backend/pyproject.toml`.

---

## 4. Safety & Hardware Lockout Policy

1. **Mandatory Hardware Lockout (`SHADOW_HARDWARE_LOCKOUT=true` & `LockedRealVehicleAdapter`)**:
   - By default, `SHADOW_HARDWARE_LOCKOUT` is `true` and `SHADOW_DEFAULT_VEHICLE_MODE` is `"simulated"`.
   - While `SHADOW_HARDWARE_LOCKOUT=true`, any configuration, service, or REST API attempt (`POST /vehicle/{id}/simulate` with `{"enabled": false}`) to place a vehicle into `"real"` hardware mode is rejected with `409 Conflict` (`HARDWARE_LOCKOUT_ACTIVE`).
   - `LockedRealVehicleAdapter` raises `RealHardwareLockedError` on `start()`, `connect()`, or `send_command()`, and its `trigger_emergency_stop()` explicitly returns `real_hardware_available=False`, `dispatch_attempted=False`, `vehicle_confirmed_stopped=False`, reminding operators to use the physical vehicle kill switch and onboard hardware failsafes.

---

## 5. Persistence, Schema & Simulation Decisions (Milestones 2–6 & Phases 1–3)

1. **Nullable / Optional Columns & Value Range Constraints**:
   - `VEHICLES.battery` in `[0.0, 100.0]`, `OBSERVATIONS.confidence` in `[0.0, 1.0]`, `TASKS.priority` in `[1, 100]`, and `VEHICLES.mode` in `('real', 'simulated')`.
2. **Configurable Heartbeat Timing & Stale Telemetry Filtering**:
   - Provisional defaults are configurable via `SHADOW_HEARTBEAT_INTERVAL_MS=250` and `SHADOW_HEARTBEAT_TIMEOUT_MS=1500` using an injectable `ClockPort`. Out-of-order or expired telemetry frames are rejected so stale frames never overwrite current state or falsely recover an `UNKNOWN` vehicle.
3. **SQLite Persistence Boundary (`VehicleService`)**:
   - Every valid telemetry heartbeat updates the in-process World Model and broadcasts to `DashboardGateway` immediately, while SQLite `vehicles` table writes occur immediately on state transitions (`CONNECTED` / `DISCONNECTED` / `UNKNOWN`, `status`, `mode`) and every `persist_every_n_heartbeats` (default `5`) for routine position/battery ticks.
4. **Dashboard WebSocket Snapshot-to-Live Synchronization (`websocket/dashboard_gateway.py`)**:
   - Clients connecting to `/ws/dashboard` buffer concurrent broadcasts during snapshot construction, receive `DashboardSnapshotEnvelope` (`type="snapshot"`) first, deduplicate events/traces already captured in the snapshot, and flush remaining buffered updates in FIFO order.
   - Slow clients exceeding `max_client_queue_size=64` or `send_timeout_seconds=1.0` are evicted without blocking healthy clients.
5. **Emergency Stop Fast Path (`POST /emergency-stop` & `websocket/vehicle_gateway.py`)**:
   - **Bypass Invariant (§10)**: `VehicleGatewayFastPath.dispatch_emergency_stop()` latches the process-wide emergency stop (`latch_global_emergency_stop()`) and invokes `adapter.trigger_emergency_stop()` directly, completely bypassing ULTRON planning/replanning, normal command queues, and synchronous SQLite writes.
   - **Explicit Evidence Separation**: `EmergencyStopResponse` and `VehicleEmergencyStopOutcome` explicitly separate `request_accepted`, `dispatch_attempted`, `dispatch_succeeded`, `vehicle_acknowledged`, `vehicle_confirmed_stopped`, `is_simulated`, and `real_hardware_available`. A `200 OK` HTTP response never implies that a `DISCONNECTED`, `UNKNOWN`, or locked real-hardware vehicle has physically stopped.
   - **Asynchronous Post-Dispatch Logging**: SQLite vehicle status updates and `SAFETY` event logging occur via background `asyncio.Task`s *after* dispatch completes.
6. **Cold-Boot Stale Connection Reset (`db/session.py:seed_default_vehicles`)**:
   - When `init_db()` runs on application startup, any existing `Vehicle` rows in SQLite whose `connection` was persisted as `"CONNECTED"` by a prior process are automatically reset to `connection="DISCONNECTED"` (and `status="IDLE"` if previously `"ACTIVE"` or `"EXECUTING"`).
7. **Simulated Mission Completion Helper (`POST /mission/{id}/complete`)**:
   - `MissionService.complete_mission()` and `POST /mission/{id}/complete` provide an explicit simulation-only helper to transition an active simulated mission to `COMPLETED`, mark its assigned tasks `COMPLETED` (`SIMULATED_COMPLETED`), record before/after completion snapshots, and broadcast updates over `/ws/dashboard`.

---

## 6. ROS 2 Humble Live Perception Integration & Unresolved Hardware Confirmations

1. **Confirmed ROS 2 Humble Environment & Message Contract**:
   - The `SCOUT01` rover runs **ROS 2 Humble** on Ubuntu with `rosbridge_server` on port `9090`, publishing JSON strings on `/detections_json` (`std_msgs/msg/String`, up to `5 Hz`) in coordinate frame `oak_rgb_camera_optical_frame` (parent frame: `oak_parent_frame`).
   - `parse_std_msgs_string_detection_frame()` accepts both ROS `sec + nanosec / 1e9` stamp objects and finite positive Unix epoch floats (`stamp`), records local UTC receive time (`received_at`) independently, and treats object `id` values strictly as frame-local detection indices (`id_is_frame_local_only=True`).
2. **Coordinate-Frame Safety & Replaceable Transform Interface (`CoordinateTransformPort`)**:
   - All `oak_rgb_camera_optical_frame` (`CAMERA_OPTICAL_RELATIVE`) and `oak_parent_frame` (`CAMERA_PARENT_RELATIVE`) detections are treated as camera/frame-relative (`map_projection_allowed=False`) and are **never** plotted on `FloorMap.tsx` or confused with rover position.
   - `map` detections are gated behind `SHADOW_ROS_MAP_FRAME_CONFIRMED=false` (`UNCONFIRMED_MAP`) until calibration is verified.
   - Unrecognized coordinate frames are preserved verbatim (`UNSUPPORTED_FRAME`, `map_projection_allowed=False`).
3. **Outstanding Hardware Confirmations & Roadmap Items**:
   - **Independent Onboard USB-Loss Motor Failsafe**: Must be verified on physical `SCOUT01` hardware before setting `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=true`.
   - **TF & Extrinsics (`/tf`, `/tf_static`)**: Validate the `map -> odom -> oak_parent_frame -> oak_rgb_camera_optical_frame` chain and camera-to-LiDAR extrinsics before enabling `SHADOW_ROS_MAP_FRAME_CONFIRMED=true`.
   - **LiDAR World Model Ingestion (`SHADOW_LIDAR_ENABLED`)**: Confirm topic, message type, frame, and extrinsics with the hardware team before enabling LiDAR corroboration in ULTRON.
   - **`HAWK01` Aerial Integration**: Remains a simulated / roadmap concept and is not scheduled for active hardware integration in the current phase.
