# Phase 3 Baseline Audit — Mission Memory, Replay & After-Action Intelligence (`docs/PHASE3_BASELINE.md`)

## 1. Branch & Git Status Verification

- **Active Branch**: `phase3-mission-history` (branched cleanly from `phase2-event-operations` at commit `d78b087`).
- **Working Tree Status**: Clean (`Working tree clean. No changes.`).
- **Safety & Hardware Lockout Posture**:
  - `SHADOW_HARDWARE_LOCKOUT=true`
  - `SHADOW_DEFAULT_VEHICLE_MODE=simulated`
  - `SHADOW_SCOUT_SERIAL_ENABLED=false`
  - `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false`
  - `SHADOW_ROS_ENABLED=false`
  - `HAWK01` remains simulated / roadmap, not integrated (`SCOUT01` is the sole active vehicle).

---

## 2. Verification of Phase 1 & Phase 2 Deliverables

All Phase 1 and Phase 2 deliverables and runtime invariants were verified present and intact:

| Phase | Deliverable / Module | Status | Verified Capabilities |
| --- | --- | --- | --- |
| **Phase 1** | `docs/PHASE1_REPORT.md`, `docs/BASELINE.md`, `docs/FALLBACK_PLANS.md`, `docs/SIGNAL_AUDIT.md`, `docs/SUPERVISED_REAL_MODE.md` | Present | Documented baseline, fallback guarantees, signal provenance audit, and supervised real-mode safety gates. |
| **Phase 1** | `ultron/planner/engine.py`, `ultron/planner/cache.py`, `ultron/diagnostics.py`, `scripts/force_gemini_timeout.py`, `scripts/signal_probe.py` | Present | Bounded Gemini wall-clock timeout (`10s` plan / `8s` replan / `3s` connect), 30s thread-safe circuit breaker, objective-preserving fallback plans with `"USED CACHED PLAN"`. |
| **Phase 2** | `ultron/events/schemas.py`, `ultron/events/router.py`, `docs/EVENT_ROUTER.md` | Present | Typed internal `UltronEvent` (`SCOUT01`-only, finite source timestamps, E-Stop exclusion) and deterministic `EventRouter` (priority ordering, content-signature deduplication, material-change bypass, stale/out-of-order rejection, bounded queues). |
| **Phase 2** | `configs/standing_orders.json`, `ultron/events/standing_orders.py`, `docs/STANDING_ORDERS.md` | Present | Deterministic `StandingOrderRegistry` (`SO-01-CRITICAL-BATTERY`, `SO-02-SENSOR-CONFLICT`, `SO-03-BLOCKED-ROUTE`, `SO-04-HAZARD-HOLD`) evaluated before LLM triage and validated via `validate_plan_evidence()` and `validate_plan()`. |
| **Phase 2** | `ultron/events/triage.py`, `docs/LLM_TRIAGE.md` | Present | Separate `EventTriageResult` schema, bounded wall-clock timeout (`SHADOW_LLM_TRIAGE_TIMEOUT_SEC=3`), shared circuit breaker reuse, epistemic validation, and deterministic fallback. |
| **Phase 2** | `backend/fastapi/services/ultron_agent_adapter.py`, `tests/test_phase2_event_intelligence.py`, `docs/PHASE2_REPORT.md` | Present | Non-blocking sensor ingestion decoupled from async `_triage_worker_loop` (concurrency=1) and coalesced `_replan_worker_loop`, epoch fencing, and immediate cancellation on `EMERGENCY_STOPPED`. |

---

## 3. Baseline Test Suite Results

All four test suites were executed offline with `SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, and `GEMINI_API_KEY` unset:

| Suite Target | Command Executed | Passed | Failed | Skipped | Warnings | Runtime (Wall) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `make test-architecture` | `python3 -c "from tests.test_system_architecture import ..."` | 2 | 0 | 0 | 0 | `0.12s` |
| `make test-ultron` | `PYTHONPATH=.:backend:backend/fastapi pytest tests` | 28 | 0 | 0 | 0 | `2.90s` (`5.07s` wall) |
| `make test-backend` | `cd backend && PYTHONPATH=fastapi:.:.. pytest` | 50 | 0 | 0 | 4 | `9.89s` (`11.92s` wall) |
| `make test-frontend` | `cd frontend && npm run typecheck && npm test -- --run` | 16 (`tsc` + 16 Vitest) | 0 | 0 | 0 | `2.28s` (`5.40s` wall) |

---

## 4. Audit of Existing Mission Persistence & Memory Capabilities

### 4.1 Current SQLite Schema (`backend/fastapi/db/models.py`)
The database currently consists of six tables initialized via `Base.metadata.create_all` in `backend/fastapi/db/session.py`:
1. **`missions` (`Mission`)**: `id`, `objective`, `status`, `created_at`, `completed_at`.
2. **`tasks` (`Task`)**: `id`, `mission_id`, `vehicle_id`, `type`, `area`, `target`, `priority`, `status`.
3. **`vehicles` (`Vehicle`)**: `id`, `vehicle_type`, `status`, `battery`, `position_x`, `position_y`, `position_alt`, `connection`, `mode`, `last_heartbeat`.
4. **`observations` (`Observation`)**: `id`, `mission_id`, `vehicle_id`, `obs_type`, `confidence`, `location_x`, `location_y`, `timestamp`, `raw` (JSON).
5. **`events` (`Event`)**: `id`, `mission_id`, `timestamp`, `category`, `message`.
6. **`reasoning_trace` (`ReasoningTrace`)**: `id`, `mission_id`, `timestamp`, `trigger`, `rationale`, `resulting_task_ids` (JSON).

### 4.2 What Can Currently Be Reconstructed vs. What Is Lost
- **In-Memory Only (`WorldModel` in `UltronLangGraphAgent`)**:
  - `WorldModel` holds the *current* live state (`mission`, `vehicles["SCOUT01"]`, `environment.tracked_objects`, `environment.hazards`, `environment.objects`, `environment.observation_history` bounded to 500, `environment.sensor_health`, `environment.decision_traces` bounded to 100, and `transform_map_validated` / `extrinsics_camera_lidar_validated`).
  - Once `WorldModel` mutates on subsequent observations, vehicle telemetry updates, or replanning passes, **prior World Model states are overwritten in place**.
  - On backend restart, all in-memory `WorldModel` state, `EventRouter` history (`maxlen=200`), `_standing_order_outcomes`, and `_triage_outcomes` are lost.
- **Mutable Overwrites in SQLite (`vehicles` and `tasks`)**:
  - `vehicles` stores only a single mutable row for `SCOUT01` (checkpointed on state transitions and every 5th heartbeat). Historical vehicle position, battery, and connection state at the moment an event or replan occurred cannot be queried from `vehicles`.
  - `tasks` rows are inserted during `MissionService.create_mission_with_rationale()` and mutated in place during `execute_mission_on_rover()` or `complete_mission()`. When Phase 2 Standing Orders or LLM replanning assign a new plan in `UltronLangGraphAgent._replan_worker_loop`, the updated tasks replace `self._world.mission.tasks` in memory, but **are not persisted back to the SQLite `tasks` table**, and prior task states before the replan are not preserved.
- **Partial Observation Persistence**:
  - `VehicleService.record_observation()` and `PerceptionService._maybe_persist_observations()` write to SQLite `observations`, whereas direct calls to `UltronLangGraphAgent.ingest_detection_frame_into_world_model()` or `UltronLangGraphAgent.record_observation()` populate `self._world.environment.observation_history` in memory without writing to SQLite unless invoked through the service methods.

---

## 5. Audit of Phase 2 Event → Triage / Standing Order → Decision → Task Linkage

1. **Originating Events (`UltronEvent`)**:
   - `EventRouter` assigns `event_id` (`UEVT-...`), `event_type`, `source`, `source_mode`, `mission_id`, `vehicle_id`, `source_timestamp`, `received_at`, `priority`, `observation_ids`, `tracked_object_ids`, `coordinate_frame`, `confidence`, `epistemic_status`, and `evidence_payload`.
   - When synced via `sync_event_intelligence_to_event_service()`, `Event` rows in SQLite only persist `id=ev.event_id`, `mission_id`, `timestamp=ev.received_at`, `category`, and `message=f"[{ev.event_type.value}] {ev.summary}"`. Structured fields (`source_timestamp`, `priority`, `source`, `source_mode`, `observation_ids`, `tracked_object_ids`, `coordinate_frame`, `epistemic_status`) are not stored on the SQLite `Event` row or in a linked snapshot.
2. **Standing Order Decisions**:
   - `_dispatch_routed_events()` evaluates `self.standing_orders.evaluate_and_execute(event, self._world)` and passes `standing_order_id=so_res.standing_order_id` and `source_event_id=event.event_id` into `_schedule_or_coalesce_replan()`.
   - However, `_replan_worker_loop()` only passes `trigger_rationale=f"[STANDING_ORDER:{so_res.standing_order_id}] {event.summary}"` into `UltronState`. `DecisionTraceEntry` does not have explicit fields for `source_event_id` or `standing_order_id` (requiring string parsing of `trigger`), and `node_assign` records un-scoped task IDs (`T-HAZ-01` or `M001-T01`) without capturing the before/after `WorldModel` snapshot.
3. **LLM Event Triage Decisions**:
   - `_record_triage_decision_trace()` creates a `DecisionTraceEntry` (`DT-TRIAGE-...`) with `trigger=f"EVENT_TRIAGE:{event.event_type.value}:{event.event_id}"`.
   - If triage disposition is `REQUEST_REPLAN` or `APPLY_STANDING_ORDER`, it schedules a replan with `source_event_id=event.event_id`, which subsequently produces a second `DecisionTraceEntry` (`DT-...`) in `node_assign`. Currently, there is no explicit `source_event_id` or `parent_trace_id` field linking the resulting replan `DecisionTraceEntry` back to the originating `event_id` and triage `trace_id`.
4. **Timestamp Handling in `sync_event_intelligence_to_event_service()`**:
   - `sync_event_intelligence_to_event_service()` previously nudged `trace_dt` relative to `latest_event_dt` across the entire batch (`if latest_event_dt is not None and trace_dt <= latest_event_dt: trace_dt = latest_event_dt + timedelta(milliseconds=idx)`), which could shift a trace timestamp past unrelated later events instead of preserving each record's true chronological timestamp and using deterministic tie-breaking.

---

## 6. Current Mission Replay Limitations (`GET /mission/{id}/replay` & `MissionReplayScrubber.tsx`)

1. **Placeholder Snapshot in `MissionService.get_mission_replay()`**:
   - Every `"event"` entry is returned with `world_model_snapshot=None`.
   - Every `"reasoning_trace"` entry is returned with a stub dictionary:
     ```python
     world_model_snapshot={
         "is_stub": True,
         "resulting_task_ids": list(rt_read.resulting_task_ids),
     }
     ```
2. **Missing Before/After State Around Decisions**:
   - Replay cannot show what ULTRON knew *before* a triggering event arrived (active tasks, vehicle pose/battery/connection, known objects/hazards, sensor health) vs. *after* the Standing Order or replan was validated and applied.
3. **Missing Structured Metadata on Replay Entries**:
   - Replay entries do not expose structured trigger metadata (originating `event_id`, `event_type`, `priority`, `source`, `source_mode`, `evidence_ids`, `standing_order_id`, `triage_disposition`, `validation_passed`, `before_snapshot`, `after_snapshot`, `historical_state_available`) needed by `MissionReplayScrubber.tsx` to render genuine before/after mission replay.
4. **Legacy Missions Without Snapshots**:
   - Missions created prior to Phase 3 (or via minimal DB fixtures in existing tests) have no persisted snapshots; replay must explicitly mark historical state as unavailable (`historical_state_available=False`, `is_stub=False`) rather than fabricating historical state from current live memory.
