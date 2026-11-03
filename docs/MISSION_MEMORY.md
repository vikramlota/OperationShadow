# Persistent Mission Memory (`docs/MISSION_MEMORY.md`)

## 1. Overview & Architectural Principle

In Operation Shadow Phase 3, **Mission Memory** (`ultron/memory/store.py`, `backend/fastapi/db/models.py:MissionSnapshot`, and `backend/fastapi/services/event_service.py`) provides an immutable, auditable historical record of ULTRON's authoritative `WorldModel` at meaningful mission transitions.

Core design principles:
1. **Single Authoritative World Model**: Mission Memory does **not** introduce a second mutable mission state, vector database, or RAG pipeline. The live `WorldModel` (`ultron/schemas/world.py`) remains the single source of truth; `MissionMemorySnapshot` records are immutable point-in-time snapshots of its accepted state.
2. **Meaningful Transitions Only (No Heartbeat/Frame Storms)**: Routine 4 Hz telemetry heartbeats and duplicate/empty 5 Hz camera frames never trigger snapshots. Snapshots are captured strictly at meaningful operational state transitions and around Phase 2 event-driven decisions.
3. **Before/After Decision Reconstruction**: Every routed event, Standing Order execution, LLM Event Triage evaluation, and replanning operation records paired `BEFORE` and `AFTER` snapshots (`paired_before_snapshot_id`) linked by explicit `associated_event_id` and `associated_trace_id` identifiers.
4. **Triple-Layer Immutability**:
   - **Pydantic Layer**: `MissionMemorySnapshot` and all nested snapshot schemas use `ConfigDict(frozen=True, extra="forbid")` and deep-copy on creation and serialization.
   - **SQLAlchemy ORM Layer**: `@event.listens_for(MissionSnapshot, "before_update")` raises `ValueError` on any update attempt.
   - **SQLite Database Trigger**: `trg_mission_snapshots_immutable` (`BEFORE UPDATE ON mission_snapshots`) aborts any SQL `UPDATE` against persisted snapshot rows.

---

## 2. Snapshot Schema (`MissionMemorySnapshot`)

Defined in `ultron/memory/store.py`:

| Field | Type | Description |
| --- | --- | --- |
| `snapshot_id` | `str` | Unique immutable identifier (`SNAP-<mission_id>-<seq>-<hash>`). |
| `mission_id` | `str` | Associated mission ID (`M001`, ...). |
| `sequence` | `int` (`>= 1`) | Monotonically increasing per-mission sequence number. |
| `timestamp` | `float` | Finite UTC epoch timestamp (preserves `source_timestamp` for sensor/event-driven transitions). |
| `timestamp_iso` | `str` | ISO-8601 UTC string representation. |
| `trigger_type` | `SnapshotTriggerTypeEnum` | Transition category (see Section 3). |
| `trigger_reason` | `str` | Human-readable reason for the snapshot. |
| `phase` | `SnapshotPhaseEnum` | `BEFORE`, `AFTER`, or `STATE_TRANSITION`. |
| `associated_event_id` | `Optional[str]` | Exact `UltronEvent.event_id` (`UEVT-...`) or `Event.id` (`M001-EVT01`). |
| `associated_trace_id` | `Optional[str]` | Exact `DecisionTraceEntry.trace_id` (`DT-...`) or `ReasoningTrace.id` (`M001-RT01`). |
| `paired_before_snapshot_id` | `Optional[str]` | For `AFTER` snapshots, points directly to the corresponding `BEFORE` snapshot ID. |
| `mission_status` | `str` | Mission status at snapshot time (`IN_PROGRESS`, `ACTIVE`, `DEGRADED`, `COMPLETED`, etc.). |
| `mission_objective` | `str` | Exact mission objective string. |
| `task_states` | `Tuple[SnapshotTaskState, ...]` | Full task breakdown including `task_id`, `vehicle_id`, `task_type`, `area`, `target`, `priority`, `status`, `execution_stage`, and `is_simulated`. |
| `active_task_ids` | `Dict[str, Optional[str]]` | Currently active task ID per vehicle (`SCOUT01`). |
| `completed_task_ids` | `Tuple[str, ...]` | Completed task IDs at snapshot time. |
| `pending_task_ids` | `Tuple[str, ...]` | Pending task IDs at snapshot time. |
| `vehicles` | `Dict[str, SnapshotVehicleState]` | `SCOUT01` status, connection, mode (`real` vs `simulated`), `pose_valid`, `coordinate_frame`, `position_x/y/alt`, `heading`, `battery`, `last_heartbeat_timestamp`, and `epistemic_status`. |
| `observation_refs` | `Tuple[SnapshotObservationRef, ...]` | Compact references to the most recent `<= 25` accepted observations. |
| `total_observation_count` | `int` | Total observations ingested into `WorldModel` at snapshot time. |
| `tracked_objects` | `Tuple[TrackedObject, ...]` | Up to `50` persistent tracked objects evaluated for staleness at `timestamp`. |
| `hazards` | `Tuple[HazardEntity, ...]` | Up to `50` known hazard entities. |
| `known_obstacles` | `Tuple[Position2D, ...]` | Up to `50` map-frame obstacle coordinates. |
| `explored_areas` | `Tuple[str, ...]` | Explored area identifiers. |
| `sensor_health` | `Dict[str, SensorHealthReport]` | Per-sensor health (`depth_camera`, `lidar`) evaluated for freshness at `timestamp`. |
| `evidence_provenance` | `SnapshotEvidenceProvenance` | Explicit provenance breakdown (see Section 4). |
| `decision_context` | `Optional[SnapshotDecisionContext]` | Event type/priority/source, evidence IDs, Standing Order ID, triage disposition/severity/source, planner source, validation outcome, and resulting task IDs. |

---

## 3. Meaningful Transition Triggers (`SnapshotTriggerTypeEnum`)

| Trigger Type | Phase | When Captured |
| --- | --- | --- |
| `MISSION_CREATED` | `BEFORE` | Immediately when a new mission is initialized, before initial planning runs. |
| `INITIAL_PLAN_ACCEPTED` | `AFTER` | Immediately after the initial mission plan passes `validate_plan()` and tasks are assigned. |
| `SIGNIFICANT_OBSERVATION_ACCEPTED` | `STATE_TRANSITION` | When an accepted observation introduces a new `TrackedObject`/`HazardEntity` or changes an object's epistemic/tracking/hazard/obstacle status. |
| `EVENT_ROUTED` | `BEFORE` | When `EventRouter` accepts a non-duplicate event, capturing the exact `BEFORE` state prior to Standing Orders or LLM Triage. |
| `STANDING_ORDER_APPLIED` | `AFTER` | After a deterministic Standing Order (`SO-01`..`SO-04`) is validated and assigned to `WorldModel`. |
| `LLM_TRIAGE_COMPLETED` | `AFTER` | After `invoke_event_triage()` and `validate_triage_result()` complete for an ambiguous event. |
| `REPLAN_BEFORE` | `BEFORE` | Immediately before `_execute_replan_pass()` if no prior `EVENT_ROUTED` snapshot was recorded for the trigger. |
| `REPLAN_ACCEPTED` | `AFTER` | Immediately after an LLM or fallback replan passes `validate_plan()` and updates mission tasks. |
| `REPLAN_DEGRADED` | `AFTER` | When plan validation exhausts retries and transitions the mission to `DEGRADED` hold mode. |
| `TASK_STATE_TRANSITION` | `STATE_TRANSITION` / `BEFORE` | When task execution states transition (`ASSIGNED` -> `IN_PROGRESS` -> `COMPLETED` / `CANCELLED` / `FAILED`). |
| `VEHICLE_STATE_TRANSITION` | `STATE_TRANSITION` | When `SCOUT01` connection changes (`ONLINE`/`OFFLINE`/`UNKNOWN`), status enters/exits `FAULT`/`UNKNOWN`, mode changes, or battery drops below `20.0%`. |
| `EMERGENCY_STOP_TRIGGERED` | `STATE_TRANSITION` | When `SCOUT01` transitions to `EMERGENCY_STOPPED` (recorded in-memory without blocking the E-Stop fast path). |
| `MISSION_COMPLETED` | `AFTER` | When a mission transitions to `COMPLETED`. |
| `MISSION_FAILED` / `MISSION_CANCELLED` | `AFTER` | When a mission fails or is cancelled. |

---

## 4. Evidence Provenance & Epistemic Honesty (`SnapshotEvidenceProvenance`)

Every `MissionMemorySnapshot` computes a structured `SnapshotEvidenceProvenance` block that explicitly separates:
- **`real_observation_ids`**: Observations ingested with `source_mode == "physical"` (e.g., live ROSBridge `/detections_json` when enabled).
- **`simulated_observation_ids`**: Observations ingested with `source_mode == "simulated"`.
- **`stale_observation_ids`**: Observations whose age at `snapshot.timestamp` exceeds `camera_stale_after_sec` (`2.0s`).
- **`conflicted_object_ids`**: `TrackedObject` IDs with `epistemic_status == "CONFLICTED"`.
- **`corroborated_object_ids`**: `TrackedObject` IDs corroborated across multiple sensors (`corroborated_by_multiple_sensors == True`).
- **`unconfirmed_coordinate_frames`**: Coordinate frames (`oak_rgb_camera_optical_frame`, `oak_parent_frame`, or `map` when `transform_map_validated == False`) whose global floor-map transform is unverified.
- **`unknowns`**: Active sensor warnings (e.g., `lidar` `UNAVAILABLE`, `depth_camera` `DISCONNECTED` or `STALE`, unconfirmed TF/extrinsics).
- **`inferences`**: Operational conclusions inferred during planning or triage, kept distinct from direct sensor facts.

---

## 5. Storage Bounds, Non-Blocking Execution & Failure Visibility

1. **Bounded Payload & Retention**:
   - `MAX_SNAPSHOT_OBSERVATION_REFS = 25` compact observation references per snapshot (never duplicating unbounded raw sensor frames).
   - `MAX_SNAPSHOT_TRACKED_OBJECTS = 50`, `MAX_SNAPSHOT_HAZARDS = 50`, `MAX_SNAPSHOT_OBSTACLES = 50`.
   - `MAX_SNAPSHOTS_PER_MISSION = 250` in `MemoryStore` (with FIFO eviction tracked via `overflow_evicted_count`) and `MAX_SNAPSHOT_QUERY_LIMIT = 250` on SQLite queries.
2. **Non-Blocking Sensor Ingestion & E-Stop Fast Path**:
   - Snapshot construction in `MemoryStore` operates on an in-memory copy in sub-millisecond time and never blocks `/detections_json` ingestion, telemetry heartbeats, or `POST /emergency-stop`.
3. **Visible Persistence Failures**:
   - SQLite snapshot writes in `EventService.record_mission_snapshot()` and `UltronLangGraphAgent.sync_event_intelligence_to_event_service()` log structured `ERROR` messages on failure and increment `EventService.snapshot_persistence_failures` and `UltronLangGraphAgent.snapshot_persistence_failures_count` (`snapshot_persistence_errors`), ensuring storage errors are never silently ignored.

---

## 6. Additive SQLite Schema Migration & Backward Compatibility

`backend/fastapi/db/session.py:ensure_phase3_schema_migrations()` runs automatically inside `init_db()`:
- Executes `CREATE TABLE IF NOT EXISTS mission_snapshots (...)`, creates indexes on `(mission_id, sequence)`, `(mission_id, timestamp)`, `associated_event_id`, and `associated_trace_id`, and installs `CREATE TRIGGER IF NOT EXISTS trg_mission_snapshots_immutable`.
- Existing tables (`missions`, `tasks`, `vehicles`, `observations`, `events`, `reasoning_trace`) and pre-Phase-3 mission rows are never dropped or destructively altered.
- Missions created before Phase 3 with zero rows in `mission_snapshots` remain fully readable across all endpoints.
