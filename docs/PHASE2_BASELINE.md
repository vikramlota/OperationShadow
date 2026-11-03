# Phase 2 Baseline & Handoff Verification (`docs/PHASE2_BASELINE.md`)

## 1. Git Status, Branch & Phase 1 Handoff Verification
- **Branch**: `phase2-event-operations` (branched cleanly from `phase1-stabilize` at `37ddc07`).
- **Working Tree**: Clean (`git status` reports no uncommitted user changes).
- **Verified Phase 1 Deliverables Present**:
  1. `docs/BASELINE.md` — Phase 1 baseline test matrix and REST/WebSocket verification.
  2. `docs/SUPERVISED_REAL_MODE.md` — Human-only preconditions, environment gates, and transmitter USB-loss firmware failsafe warning.
  3. `docs/FALLBACK_PLANS.md` — Allowed `SCOUT01` task types (`SCOUT_ALLOWED_ACTIONS`), standard areas/targets, validator constraints, and the 4 deterministic fallback branches in `build_evidence_grounded_plan()`.
  4. `docs/SIGNAL_AUDIT.md` — End-to-end audit of all 8 signals with `file:line` producers, transports, backend ingest points, simulator emission status, and the blank `Real-rover result` column awaiting physical rover execution.
  5. `docs/PHASE1_REPORT.md` — Complete Phase 1 summary and Definition of Done mapping.
  6. `scripts/force_gemini_timeout.py`, `scripts/signal_probe.py`, and `ultron/diagnostics.py`.
- **Missing Artifacts / Discrepancies Check**:
  - `docs/PHASE2_ARCHITECTURE_AUDIT.md` does not exist in `docs/` (listed as optional in the prompt: `"if it exists"`). No Phase 1 deliverable is missing.
  - The `Real-rover result` column in `docs/SIGNAL_AUDIT.md` remains intentionally blank because no physical rover session has been executed by a human operator yet. Per Phase 2 rules, we do **not** infer physical hardware capabilities from code alone: only signals verified to reach the backend by code (`camera_detections` on `/detections_json`, `connection_state` / `last_heartbeat` / `is_stale`) are treated as real-capable ingestion paths, and all signals emitted in simulated mode are explicitly tagged `SIMULATED` (or `UNKNOWN` when unverified).

---

## 2. Phase 2 Baseline Test Results

All four test suites were executed on `phase2-event-operations` with `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_HARDWARE_LOCKOUT=true`, and `GEMINI_API_KEY` unset:

| Suite Target | Command | Passed | Failed | Skipped | Warnings | Runtime |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `make test-architecture` | `python3 -c "from tests.test_system_architecture import ..."` | 2 | 0 | 0 | 0 | 0.13s |
| `make test-ultron` | `PYTHONPATH=.:backend:backend/fastapi pytest tests` | 18 | 0 | 0 | 0 | 1.58s |
| `make test-backend` | `cd backend && PYTHONPATH=fastapi:.:.. pytest` | 50 | 0 | 0 | 4 | 10.10s |
| `make test-frontend` | `cd frontend && npm run typecheck && npm test -- --run` | 16 (`tsc` + 16 Vitest) | 0 | 0 | 0 | 2.28s |

---

## 3. Signal Classification for Phase 2 Event Generation (from `docs/SIGNAL_AUDIT.md`)

Because the human-confirmed `Real-rover result` column in `docs/SIGNAL_AUDIT.md` is not yet populated from a live rover run:

| Signal | Backend Ingest Path | Phase 2 Source Mode Classification | Event Types Generated |
| --- | --- | --- | --- |
| **Camera Detections (`/detections_json`)** | `RosBridgeAdapter` / `PerceptionService.ingest_detection_frame` → `UltronLangGraphAgent.ingest_detection_frame_into_world_model` | `PHYSICAL` when `telemetry_source == "rosbridge"` and `SHADOW_ROS_ENABLED=true`; `SIMULATED` otherwise | `HAZARD_DETECTED`, `OBSTACLE_DETECTED`, `TARGET_DETECTED`, `SENSOR_CONFLICT`, `OBSERVATION_STALE` |
| **Sensor Health & Freshness (`depth_camera`, `lidar`)** | `PerceptionService` / `WorldModel.environment.sensor_health` / `refresh_world_model_freshness` | `PHYSICAL` or `SIMULATED` for `depth_camera`; `UNKNOWN` (`UNAVAILABLE`) for `lidar` | `SENSOR_DEGRADED`, `OBSERVATION_STALE` |
| **Vehicle Battery (`battery`)** | `VehicleGateway` (`WS /ws/vehicle`) → `VehicleService.ingest_telemetry` → `UltronLangGraphAgent.update_vehicle_state` | `SIMULATED` (physical Pi `fusion.py` packet does not match `validate_inbound_telemetry_frame` and has no active sender loop) | `VEHICLE_LOW_BATTERY` |
| **Vehicle Connection State & Heartbeat (`connection`, `last_heartbeat`)** | `VehicleService.evaluate_heartbeats`, `VehicleService.disconnect_vehicle`, `UltronLangGraphAgent.update_vehicle_state` / `mark_vehicle_unknown` | `SIMULATED` by default (`PHYSICAL` only if `/ws/vehicle` is connected from rover) | `VEHICLE_DISCONNECTED` |
| **Vehicle Operational Status (`status == "FAULT"`)** | `VehicleService` / `MissionService` → `UltronLangGraphAgent.update_vehicle_state` | `SIMULATED` | `VEHICLE_FAULT` |
| **Mission Lifecycle Transitions (`status`)** | `MissionService` / `UltronLangGraphAgent` (`create_simulated_mission`, replanning, completion, degraded transition) | `SIMULATED` | `MISSION_STATE_CHANGED` |
| **ToF/Ultrasonic Distance, Temperature, Gas, LiDAR PointCloud2** | Not ingested into backend `WorldModel` from physical rover (`lidar` is `UNAVAILABLE` in backend; `distance`/`temp`/`gas` blocked in `fusion.py`) | `SIMULATED` (only when explicitly supplied via structured simulation/observations) or `UNKNOWN` (`UNAVAILABLE`) | Handled only if explicitly ingested as validated `StructuredObservation` / `SensorObservation`; never fabricated |

---

## 4. Existing Interfaces & Phase 2 Event-Routing Boundaries

To avoid duplicating existing capabilities, Phase 2 integrates cleanly at the following boundaries:

1. **Authoritative State (`ultron/schemas/world.py` & `ultron/world_model/updater.py`)**:
   - `WorldModel` remains the single source of truth for `mission`, `vehicles["SCOUT01"]`, `tracked_objects`, `observation_history`, `sensor_health`, and `decision_traces`.
2. **Event Generation & Routing (`ultron/events/`)**:
   - `EventRouter` (`ultron/events/router.py`) consumes **already-validated** `WorldModel` updates and `SensorObservation`s (never raw unvalidated socket strings).
   - Normalizes updates into typed `UltronEvent` instances (`ultron/events/schemas.py`), assigns deterministic priority (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`), suppresses duplicates via stable signatures + time-window cooldowns while allowing material changes (severity escalation, confidence jump, spatial displacement), and rejects stale/out-of-order source timestamps.
   - Emergency Stop (`EMERGENCY_STOPPED` / `POST /emergency-stop`) bypasses `EventRouter`, `StandingOrderRegistry`, and `LLMTriage` completely and immediately cancels any pending triage or replanning tasks.
3. **Standing Orders (`configs/standing_orders.json` & `ultron/events/standing_orders.py`)**:
   - Evaluated **before** LLM triage whenever an accepted event matches a registered Standing Order's `event_types`, preconditions, and required evidence.
   - Uses `build_evidence_grounded_plan()` (`ultron/planner/engine.py`), `get_fallback_plan(mission_id, objective=...)` (`ultron/planner/cache.py`), `validate_plan_evidence()` (`ultron/planner/grounding.py`), and `validate_plan()` (`ultron/safety/validator.py`) so every Standing Order preserves `world.mission.objective`, carries verified `evidence_ids`, includes `"USED CACHED PLAN"` in fallback rationales, and satisfies all `SCOUT01` safety constraints.
4. **LLM Event Triage (`ultron/events/triage.py`)**:
   - Invoked asynchronously via a bounded background worker (`UltronLangGraphAgent`) **only** when a normalized event survives deduplication and is not deterministically resolved by a Standing Order.
   - Uses a dedicated `EventTriageResult` Pydantic schema (`IGNORE`, `LOG_ONLY`, `APPLY_STANDING_ORDER`, `REQUEST_REPLAN`, `ESCALATE_TO_OPERATOR`) completely separate from `MissionPlan`.
   - Reuses Phase 1's bounded wall-clock executor and module-level circuit breaker (`SHADOW_LLM_TRIAGE_TIMEOUT_SEC`, default `3.0s`), validates every returned `evidence_id`, `vehicle_id`, and `standing_order_id` against the authoritative `WorldModel` and `StandingOrderRegistry`, and falls back deterministically on timeout, breaker-open, or validation failure.
5. **Replanning & Persistence (`backend/fastapi/services/ultron_agent_adapter.py`, `event_service.py`, `dashboard_gateway.py`)**:
   - Reuses the existing `_schedule_or_coalesce_replan` / `_replan_worker_loop` in `UltronLangGraphAgent` (preserving coalescing, cooldown, mission-epoch fencing, and E-Stop cancellation) and persists events and reasoning traces through `EventService` and `DashboardGateway` without altering any existing REST or WebSocket schemas.
