# Operation Shadow — Comprehensive Bug Hunt, Code Audit & Second-Pass Safety/Reliability Report

**Date:** 2026-11-03  
**Repository:** `OperationShadow-new`  
**Scope:** Pass 1 & Pass 2 end-to-end technical audit, critical safety/reliability hardening, ROS vehicle identity & timestamp verification, asynchronous replanning decoupling, strict telemetry validation, single-vehicle `SCOUT01` safety validation, latched Emergency Stop hardening, and regression testing.

---

## 1. Issues Found and Fixed (Pass 1 + Pass 2)

### Second-Pass Critical Safety & Reliability Fixes

| Area | File(s) Changed | Issue Found | Fix Implemented |
| :--- | :--- | :--- | :--- |
| **1. ROS Vehicle Identity & Timestamp Validation** | `backend/fastapi/services/rosbridge_client.py`, `backend/fastapi/core/config.py`, `backend/fastapi/main.py` | `rosbridge_client.py` used a hardcoded `ROSBRIDGE_URL` (`ws://10.241.171.41:9090`), silently fell back to `"SCOUT01"` when `vehicle_id` was unconfigured, replaced missing/invalid `stamp` values with `datetime.now().timestamp()`, and returned early on empty detection frames (`objects: []`) instead of updating camera freshness. | Added `build_rosbridge_url(settings)` driven by `SHADOW_ROS_HOST` and `SHADOW_ROS_PORT`. Removed the silent `"SCOUT01"` fallback: when `SHADOW_ROS_VEHICLE_ID` is unconfigured, valid frames are ingested as unassociated (`vehicle_id=None`), while unknown (`ROVER99`, `HAWK01`) or mismatched vehicle IDs are rejected with explicit diagnostics. Added `_parse_finite_stamp` rejecting `None`, `bool`, `NaN`, `Infinity`, `<= 0`, and non-numeric stamps without substituting current time. Processed valid empty frames (`objects: []`) to update `depth_camera` freshness while keeping `free_space.is_verified_clear=False`. |
| **2. Decoupled Sensor Ingestion & Async LLM Replanning** | `backend/fastapi/services/ultron_agent_adapter.py` | `ingest_detection_frame_into_world_model` and `record_observation` invoked `self.app.invoke(...)` synchronously inline, blocking camera/sensor ingestion for the full duration of Gemini LLM calls (`256.18 ms` for a 250ms call, or `>3,000 ms` on live API calls) and risking stale LLM overwrites. | Decoupled sensor ingestion from LLM planning via an asynchronous background replanning worker (`_replan_worker_loop`) running `self.app.invoke` in `asyncio.to_thread` on a deep-copied `WorldModel` snapshot. Added update coalescing (`_CoalescedReplanRequest`), priority elevation (`REPLAN_PRIORITY_SIGNIFICANT` for hazards/obstacles/faults vs. `REPLAN_PRIORITY_ORDINARY`), configurable cooldowns, bounded retries (`replan_max_retries=2`), mission-epoch fencing (`_mission_epoch`) so stale replans never overwrite newer observations or missions, and immediate cancellation on Emergency Stop (`cancel_pending_replanning`). |
| **3. Strict Telemetry & Observation Validation** | `backend/fastapi/websocket/vehicle_gateway.py` | `handle_vehicle_session` silently converted missing battery to `100.0`, missing positions to `(0.0, 0.0)`, malformed timestamps to `datetime.now()`, missing confidence to `1.0`, and missing observation coordinates to `(0.0, 0.0)`. | Implemented `validate_inbound_telemetry_frame` and `validate_inbound_observation_frame` using `_require_finite_float` and `_parse_strict_timestamp`. Distinguished actual `0.0` measurements (accepted) from `None`, missing keys, `bool`, `NaN`, and `Infinity` (rejected). Invalid frames are logged with structured error codes without refreshing `last_heartbeat` or dropping the WebSocket session. |
| **4. SCOUT01 Single-Vehicle Safety Validation & HAWK01 Cleanup** | `ultron/safety/validator.py`, `ultron/config.py`, `ultron/schemas/world.py`, `ultron/schemas/commands.py`, `ultron/schemas/__init__.py`, `ultron/safety/constraints.py`, `ultron/safety/__init__.py`, `ultron/world_model/__init__.py`, `ultron/world_model/model.py`, `backend/fastapi/core/config.py`, `backend/fastapi/services/vehicle_service.py` | `validate_plan` did not reject missions when `SCOUT01` was missing from `WorldModel.vehicles`, when `SCOUT01` connection was `UNKNOWN`, when `battery` was `None`/`NaN`, or when `SCOUT01` was `EMERGENCY_STOPPED`. Obsolete `HAWK01` schemas (`HawkState`, `HawkCommand`, `HawkAction`) remained in active ULTRON schema/config modules. | Hardened `validate_plan` to require `SCOUT01` in `WorldModel.vehicles`, require finite battery (`0.0 <= battery <= 100.0`, and `> 20.0%` for movement/active tasks), require `ONLINE`/`CONNECTED` status, check `pose_valid`, and treat `EMERGENCY_STOPPED` as an overriding block. Added independent execution-layer safety validation (`_validate_scout_execution_safety`) in `MissionService.execute_mission_on_rover`. Removed obsolete active `HAWK01` code from ULTRON schemas/config and `SHADOW_ROS_VEHICLE_ID` validation. |
| **5. Emergency Stop Hardening & Latching** | `backend/fastapi/services/scout_serial_writer.py`, `backend/fastapi/services/mission_service.py`, `backend/fastapi/websocket/vehicle_gateway.py`, `backend/tests/conftest.py` | A timing gap existed between checking `EMERGENCY_STOPPED` in SQLite and writing movement bytes (`W`, `A`, `S`, `D`) over serial; step delays used uninterruptible `asyncio.sleep`; and failures to send `'X'` (stop) over serial or WebSocket were silently ignored (`except Exception: pass`). | Added process-wide and writer-level authoritative Emergency Stop latching (`latch_global_emergency_stop`, `reset_global_emergency_stop(authorized=True)`). `ScoutSerialWriter.send()` checks the latch inside its serial lock right before writing, raising `EmergencyStopLatchedError` on any movement command. Replaced blind `asyncio.sleep` in rover execution with `wait_unless_emergency_stopped` so E-Stop wakes sleeping tasks in `< 1 ms`. Added `send_verified_stop()` and `_send_stop_and_report_fault_if_failed()` to log `CRITICAL` faults and record `EventCategoryEnum.SAFETY` fault events whenever transmitting `'X'` or WebSocket `STOP` fails. |
| **6. Bounded Retries & Reliability Improvements** | `ultron/graph/workflow.py`, `backend/fastapi/services/mission_service.py`, `ultron/graph/nodes.py` | `SimpleGraphRunner._plan_and_validate_with_retries` and `MissionService._next_mission_id` used unbounded `while True:` loops. | Replaced `while True:` with `for _attempt in range(MAX_PLAN_VALIDATION_ATTEMPTS):` (`MAX_PLAN_VALIDATION_ATTEMPTS = 3`) in `workflow.py` and bounded `for _ in range(10_000):` in `_next_mission_id`. |

---

## 2. Files Changed

1. `.env` — Safe defaults (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_LLM_MODEL=gemini-2.5-flash`, removed hardcoded key).
2. `backend/pyproject.toml` — Added `langgraph.*`, `google.*`, `elevenlabs.*` mypy overrides.
3. `backend/fastapi/core/config.py` — Restricted `SHADOW_ROS_VEHICLE_ID` validation to `'SCOUT01'`.
4. `backend/fastapi/main.py` — Updated `/scout/status` to use `build_rosbridge_url(settings)`.
5. `backend/fastapi/api/map.py` — Offloaded blocking `shutil.copyfileobj` to `asyncio.to_thread`.
6. `backend/fastapi/api/mission.py` — Strict return type annotations for `/intelligence/snapshot` and `/intelligence/query`.
7. `backend/fastapi/services/event_service.py` — Added `commit` and `broadcast` flags for atomic transaction batching.
8. `backend/fastapi/services/mission_service.py` — Preserved user's `_task_serial_steps` changes; added atomic commits, bounded `_next_mission_id`, `_validate_scout_execution_safety`, latched `_is_emergency_stopped`, interruptible movement waits, and `_send_stop_and_report_fault_if_failed`.
9. `backend/fastapi/services/rosbridge_adapter.py` — Fixed `roslibpy.Topic` strict mypy typing.
10. `backend/fastapi/services/rosbridge_client.py` — Removed hardcoded URL and silent `SCOUT01` fallback; added strict finite timestamp validation, vehicle identity verification, empty-frame freshness handling, and malformed message isolation.
11. `backend/fastapi/services/scout_serial_writer.py` — Added authoritative Emergency Stop latching (`latch_global_emergency_stop`, `reset_global_emergency_stop(authorized=True)`), `wait_unless_emergency_stopped`, and `send_verified_stop` (`SerialStopCommandFaultError`).
12. `backend/fastapi/services/tts.py` — Cleaned up unused `type: ignore` comments.
13. `backend/fastapi/services/ultron_agent_adapter.py` — Decoupled sensor ingestion from LLM replanning via an asynchronous coalescing replanning worker with priority elevation, cooldowns, bounded retries, mission-epoch fencing, and E-Stop cancellation; updated camera sensor freshness on empty frames.
14. `backend/fastapi/services/vehicle_service.py` — Removed obsolete `HAWK01` branch/docstring references.
15. `backend/fastapi/websocket/dashboard_gateway.py` — Fixed `build_snapshot_envelope` to return the newest 50 events/traces in chronological order and sort mission tasks.
16. `backend/fastapi/websocket/vehicle_gateway.py` — Added strict `validate_inbound_telemetry_frame` and `validate_inbound_observation_frame`, immediate E-Stop latching in `dispatch_emergency_stop`, and critical fault reporting on stop transport failure.
17. `ultron/config.py`, `ultron/schemas/commands.py`, `ultron/schemas/world.py`, `ultron/schemas/__init__.py`, `ultron/safety/constraints.py`, `ultron/safety/__init__.py`, `ultron/world_model/__init__.py`, `ultron/world_model/model.py` — Removed obsolete active `HAWK01` schemas and constants.
18. `ultron/graph/nodes.py`, `ultron/graph/workflow.py` — Fixed `node_degraded` to clear rejected `proposed_plan=None` and set `MissionStatus.DEGRADED`; replaced `while True:` with bounded `MAX_PLAN_VALIDATION_ATTEMPTS = 3`.
19. `ultron/planner/engine.py`, `ultron/planner/grounding.py`, `ultron/replanner/triggers.py`, `ultron/vision/pipeline.py`, `ultron/world_model/tools.py`, `ultron/world_model/updater.py` — Guarded `load_dotenv`, fixed `STALE` object filtering, bounded `observation_history` to 500, and optimized `get_environment_snapshot`.
20. `ultron/safety/validator.py` — Added comprehensive single-vehicle `SCOUT01` existence, connection, battery, `pose_valid`, and `EMERGENCY_STOPPED` validation.
21. `frontend/src/state/store.tsx`, `frontend/src/App.tsx`, `frontend/src/components/FloorMap.tsx`, `frontend/src/components/MissionPanel.tsx`, `frontend/src/components/ReasoningTracePanel.tsx`, `frontend/src/components/PerceptionPanel.tsx` — Fixed WebSocket disconnect/heartbeat staleness, duplicate `/health` fetch, disconnected breadcrumb filtering, `FAILED`/`CANCELLED` task badge styling, per-trace decision matching, and `selectedObjectIndex` clamping.
22. `backend/tests/conftest.py`, `backend/tests/test_audit_regressions.py` — Added E-Stop latch test isolation fixture and comprehensive Pass 1 & Pass 2 regression test suite.

---

## 3. Tests Added (`backend/tests/test_audit_regressions.py`)

1. `test_ros_vehicle_identity_timestamps_empty_frames_and_malformed_handling`:
   - Tests configured vs. unconfigured `build_rosbridge_url`, unassociated preservation (`vehicle_id=None`) when `SHADOW_ROS_VEHICLE_ID=""`, rejection of unknown/removed vehicle IDs (`ROVER99`, `HAWK01`, whitespace, int), rejection of invalid timestamps (`None`, string, `0.0`, `-10.5`, `NaN`, `inf`, `-inf`, `True`), empty detection list (`objects: []`) freshness updates without claiming clear space, and malformed payload/frame resilience.
2. `test_high_frequency_sensor_ingestion_decoupled_from_slow_llm_replanning`:
   - Bursts 20 high-frequency camera detection frames (including a `fire` hazard) while `invoke_gemini_planner` is delayed by `200 ms` per call; verifies every ingestion call completes in `< 5 ms` median (`~0.14 ms`), repeated updates coalesce (`replan_coalesced_updates_count > 0`), all 20 observations remain intact in `observation_history`, and triggering `EMERGENCY_STOPPED` immediately cancels in-flight replanning (`replan_discarded_stale_count >= 1`).
3. `test_strict_telemetry_and_observation_validation_and_ws_preservation`:
   - Verifies actual zero values (`battery=0.0`, `position_x=0.0`, `position_y=0.0`, `confidence=0.0`) are accepted, while missing/None/bool/NaN battery, missing positions, missing/malformed timestamps, non-zero ground altitude, missing confidence, and missing observation coordinates are rejected without refreshing `last_heartbeat` or dropping the `/ws/vehicle` session.
4. `test_scout01_single_vehicle_safety_validation_matrix`:
   - Verifies `validate_plan` across all 6 required states: (1) `SCOUT01` connected and healthy, (2) `SCOUT01` `OFFLINE`, (3) `SCOUT01` `UNKNOWN` connection, (4) `SCOUT01` missing from `WorldModel`, (5) `SCOUT01` low (`12%`) or unavailable (`None`) battery, and (6) `SCOUT01` in `EMERGENCY_STOPPED`.
5. `test_emergency_stop_hardening_pre_command_mid_tick_mid_delay_and_serial_fault`:
   - Verifies unauthorized reset rejection, immediate pre-command `EmergencyStopLatchedError` on `writer.send("W")`, immediate `< 50 ms` wake-up during a `5.0 s` `wait_unless_emergency_stopped` delay, `SerialStopCommandFaultError` when transmitting `'X'` over a disconnected/broken serial port, and mid-tick E-Stop abort during `MissionService.execute_mission_on_rover` recording a `CRITICAL STOP FAULT` `SAFETY` event in SQLite while preserving `EMERGENCY_STOPPED`.
6. `test_node_degraded_and_bounded_retries_and_history_cap` & `test_dashboard_snapshot_returns_most_recent_50_events_in_chronological_order`:
   - Verifies `node_degraded` clears `proposed_plan=None`, `SimpleGraphRunner` bounds validation retries to 3, `observation_history` caps at 500, and dashboard snapshots return the newest 50 events in chronological order.

---

## 4. Sensor-Ingestion Latency (Before vs. After)

Measured under a simulated `250 ms` Gemini LLM replanning delay when a camera obstacle detection triggers replanning:

| Metric | Before (Synchronous Inline LLM Replan) | After (Decoupled Async Coalescing Worker) | Improvement |
| :--- | :--- | :--- | :--- |
| **`ingest_detection_frame_into_world_model` latency (250ms LLM call)** | **256.181 ms** (blocked on LLM) | **0.144 ms** (non-blocking) | **-99.94% latency (1,779× faster)** |
| **`get_environment_snapshot` latency (600 obs, n=100)** | **0.257 ms** median | **0.151 ms** median | **-41.1% latency** |
| **`MissionService.create_mission` latency (SQLite, n=30)** | **4.750 ms** median (3 commits) | **2.740 ms** median (1 atomic commit) | **-42.3% latency** |

---

## 5. Emergency Stop Verification Scope

- **Verified in Software & Simulated Hardware (`PASS`)**:
  - Process-wide in-memory E-Stop latch (`latch_global_emergency_stop`), `VehicleGatewayFastPath.dispatch_emergency_stop`, `MockVehicleAdapter.trigger_emergency_stop`, `UltronLangGraphAgent.cancel_pending_replanning`, `ScoutSerialWriter` pre-write lock gate (`EmergencyStopLatchedError`), interruptible movement delays (`wait_unless_emergency_stopped`), and critical fault reporting (`SerialStopCommandFaultError` + SQLite `SAFETY` event on simulated serial disconnect) are deterministically verified by automated tests.
- **NOT Verified on Physical Hardware (`HARDWARELOCKOUT=true`)**:
  - Software tests do **not** verify physical motor power cutoff, ESP-NOW radio link latency/dropouts, or the physical E-Stop kill switch on the AgileX Scout Mini chassis. Physical hardware actuation remains locked by default (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_SCOUT_SERIAL_ENABLED=false`, `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false`).

---

## 6. Remaining Physical Hardware Dependencies

Before setting `SHADOW_HARDWARE_LOCKOUT=false` on the physical `SCOUT01` rover, the hardware/firmware team must verify on actual hardware:

1. **Independent Onboard Firmware Dead-Man Watchdog**: The ESP32 / Scout Mini motor controller firmware must independently zero motor PWM/CAN velocity commands if no valid command frame arrives within `< 500 ms`, so a USB serial disconnect, ESP-NOW packet loss, or backend crash halts the rover without relying on a software `'X'` byte.
2. **Physical Hardware Kill Switch**: Verify the physical E-Stop button on the Scout Mini chassis directly severs actuator power independently of the Jetson/ESP32 software stack.
3. **ROS 2 TF Tree & Extrinsics Confirmation**: Confirm the `map -> odom -> oak_parent_frame -> oak_rgb_camera_optical_frame` transform chain before setting `SHADOW_ROS_MAP_FRAME_CONFIRMED=true`, and confirm LiDAR topic, message type, frame, and camera-to-LiDAR extrinsics before setting `SHADOW_LIDAR_ENABLED=true`.
