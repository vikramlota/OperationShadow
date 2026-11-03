# ULTRON Event Router (`docs/EVENT_ROUTER.md`)

## 1. Overview & Role
The **Event Router** (`ultron/events/router.py` and `ultron/events/schemas.py`) is an in-process, non-blocking event normalization, prioritization, and deduplication layer between validated `WorldModel` observations/telemetry and ULTRON's decision pipeline (**Standing Orders** and **LLM Event Triage**).

Key architectural invariants:
- **Separate Internal Schema**: `UltronEvent` (`ultron/events/schemas.py`) is strictly separate from `MissionPlan` (the Gemini mission-planning `response_schema`) and from existing public REST/WebSocket schemas.
- **No Unvalidated Input**: The Event Router consumes only validated `WorldModel` entities, `SensorObservation`s, and `TelemetryPayloadStub` updates—never raw socket strings.
- **Emergency Stop Independence**: `POST /emergency-stop` and `EMERGENCY_STOPPED` vehicle states bypass the Event Router completely (`status="excluded_emergency_stop"`).

---

## 2. Internal Event Contract (`UltronEvent`)

| Field | Type | Required? | Validation & Epistemic Rules |
| --- | --- | --- | --- |
| `event_id` | `str` (`UEVT-...`) | Yes (auto-default) | Non-blank unique identifier (`1..64` chars). Never used to compute deduplication signatures. |
| `event_type` | `EventTypeEnum` | Yes | One of the 10 supported categories in Section 3. |
| `source` | `EventSourceEnum` | Yes | `camera`, `rosbridge`, `telemetry`, `sensor_health`, `vehicle_state`, `world_model`, `mission_lifecycle`. |
| `source_mode` | `SensorSourceModeEnum` | Yes (`simulated` default) | Explicitly tracks `physical` vs. `simulated` per `docs/SIGNAL_AUDIT.md`. |
| `mission_id` | `str \| None` | Optional | Preserved as `None` when no mission is active; never fabricated. |
| `vehicle_id` | `str \| None` | Optional | Must be `"SCOUT01"` or `None`. Rejects unknown or unintegrated vehicles (`"HAWK01"`, `"ROVER99"`). |
| `source_timestamp` | `float \| None` | Optional | Must be finite and `> 0.0` if present; kept `None` if source timestamp is unknown (never replaced with `now()`). |
| `received_at` | `float` | Yes | Finite `> 0.0` backend receive timestamp (Unix epoch seconds). |
| `priority` | `EventPriorityEnum` | Auto-derived if omitted | Deterministic priority (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`). |
| `observation_ids` | `list[str]` | Optional (`[]`) | Validated observation IDs in `world.environment.observation_history`. |
| `tracked_object_ids` | `list[str]` | Optional (`[]`) | Persistent `TrackedObject` IDs (`OBJ-001`, ...). |
| `coordinate_frame` | `str \| None` | Optional | Preserved verbatim (`oak_rgb_camera_optical_frame`, `oak_parent_frame`, `map`). Never assumed to be `map`. |
| `confidence` | `float \| None` | Optional | Finite float in `[0.0, 1.0]`. |
| `epistemic_status` | `EpistemicStatusEnum` | Yes (`UNKNOWN` default) | `OBSERVED`, `CORROBORATED`, `INFERRED`, `UNKNOWN`, `STALE`, or `CONFLICTED`. |
| `evidence_payload` | `dict[str, Any]` | Optional (`{}`) | Structured evidence fields (positions, range, battery, conflict details). |
| `summary` | `str` | Yes | Concise human-readable diagnostic summary (`1..2000` chars). |
| `dedup_signature` | `str \| None` | Auto-computed | Deterministic content signature for duplicate suppression. |

---

## 3. Event Types, Actual Sources & Priority Model

Grounded in `docs/SIGNAL_AUDIT.md`, only verified backend ingest paths are marked real-capable (`physical` when `SHADOW_ROS_ENABLED=true` and `telemetry_source == "rosbridge"`), while all unconfirmed or mock sources remain `SIMULATED` or `UNKNOWN`:

| `EventTypeEnum` | Default Priority | Originating Source(s) | Source Mode Status (`SIGNAL_AUDIT.md`) | Default Route Target | SQLite `EventCategoryEnum` |
| --- | --- | --- | --- | --- | --- |
| `VEHICLE_FAULT` | `CRITICAL` (4) | `vehicle_state` (`ScoutState.status == "FAULT"`) | `SIMULATED` | `LLM_TRIAGE` / Operator Escalation | `SAFETY` |
| `HAZARD_DETECTED` | `HIGH` (3) | `camera` / `rosbridge` (`HazardEntity` or `/detections_json` hazard class) | `PHYSICAL` (via `/detections_json`) or `SIMULATED` | `STANDING_ORDER` / `LLM_TRIAGE` | `SAFETY` |
| `OBSTACLE_DETECTED` | `HIGH` (3) if blocking (`<= 2.5m`), else `MEDIUM` (2) | `camera` / `rosbridge` (`TrackedObject.is_obstacle`) or `world_model` (`known_obstacles`) | `PHYSICAL` (via `/detections_json`) or `SIMULATED` | `STANDING_ORDER` (when blocking route) else `LLM_TRIAGE` | `OBSERVATION` |
| `SENSOR_CONFLICT` | `HIGH` (3) | `camera` / `rosbridge` / `world_model` (`EpistemicStatusEnum.CONFLICTED`) | `SIMULATED` (until LiDAR extrinsics confirmed) | `STANDING_ORDER` | `OBSERVATION` |
| `VEHICLE_LOW_BATTERY` | `HIGH` (3) | `telemetry` (`ScoutState.battery < 20.0%`) | `SIMULATED` (rover `fusion.py` packet unaligned) | `STANDING_ORDER` | `SAFETY` |
| `TARGET_DETECTED` | `MEDIUM` (2) if matches objective/target, else `LOW` (1) | `camera` / `rosbridge` (`TrackedObject` / `ObjectEntity`) | `PHYSICAL` (via `/detections_json`) or `SIMULATED` | `LLM_TRIAGE` | `OBSERVATION` |
| `SENSOR_DEGRADED` | `MEDIUM` (2) | `sensor_health` (`DISCONNECTED` / `ERROR` on enabled sensor) | `PHYSICAL` (for `depth_camera`) / `UNKNOWN` (for `lidar`) | `LLM_TRIAGE` | `TELEMETRY` |
| `OBSERVATION_STALE` | `MEDIUM` (2) | `camera` / `rosbridge` (`TrackingStatusEnum.STALE`) | `PHYSICAL` or `SIMULATED` | `LLM_TRIAGE` | `OBSERVATION` |
| `VEHICLE_DISCONNECTED` | `MEDIUM` (2) | `vehicle_state` (`connection` in `{"OFFLINE", "UNKNOWN"}`) | `PHYSICAL` or `SIMULATED` | `LLM_TRIAGE` | `TELEMETRY` |
| `MISSION_STATE_CHANGED` | `LOW` (1) | `mission_lifecycle` (`MissionService` / `UltronLangGraphAgent`) | `SIMULATED` | `LOG_ONLY` | `MISSION` |

---

## 4. Deduplication, Coalescing & Material-Change Rules

Consecutive camera frames (up to 5 Hz on `/detections_json`) often report the same object or obstacle. `EventRouter` prevents event and LLM storms via:

1. **Deterministic Content Signature (`compute_event_signature`)**:
   - Constructed from `event_type | mission_id | vehicle_id | coordinate_frame | sorted(tracked_object_ids) | label | coarse_0.5m_spatial_bucket`.
   - Never uses the random `event_id`.
2. **Time-Bounded Suppression Window (`dedup_window_sec`, default `2.0s`)**:
   - If an event with the same `dedup_signature` arrives within `dedup_window_sec`, it is suppressed (`status="suppressed_duplicate"`, `route_target="SUPPRESSED"`) and increments `coalesced_count` **unless** a material change occurred.
3. **Material-Change Bypass (`_is_material_change`)**:
   - Even within `dedup_window_sec`, an event is **not** suppressed if any of the following hold:
     - **Priority Escalation**: `priority_rank(new.priority) > priority_rank(prev.priority)` (e.g., `MEDIUM` → `HIGH` when an obstacle enters the blocking threshold).
     - **Source Change**: `new.source != prev.source`.
     - **Confidence Jump**: `abs(new.confidence - prev.confidence) >= 0.20`.
     - **Spatial Displacement**: Euclidean distance between 3D positions is `>= 0.50 m`.
     - **Battery Drop**: Battery decreased by `>= 5.0%` since the last `VEHICLE_LOW_BATTERY` event.

---

## 5. Stale, Out-of-Order & Bounded-Memory Guarantees

1. **Out-of-Order Protection (`rejected_out_of_order`)**:
   - `EventRouter` tracks the latest accepted `source_timestamp` per stream key (`source:vehicle_id:object_or_sensor`). Any event with `source_timestamp < latest_source_timestamp` is rejected immediately so older frames cannot overwrite newer World Model evidence.
2. **Stale Event Protection (`rejected_stale`)**:
   - Any event whose `received_at - source_timestamp > stale_threshold_sec` (default `5.0s`) is rejected unless `event_type` is `OBSERVATION_STALE` or `SENSOR_DEGRADED` (which explicitly report staleness/degradation).
3. **Bounded Memory**:
   - Priority queue: bounded to `max_queue_size = 128` (evicts lowest-priority/oldest item on overflow and increments `dropped_queue_overflow_count`).
   - Event history: `collections.deque(maxlen=200)`.
   - Deduplication & stream timestamp caches: `OrderedDict` capped at `max_dedup_entries = 256` with FIFO/LRU eviction.
