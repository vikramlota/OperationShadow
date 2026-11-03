# Deterministic Fallback Plans & Safety Validator Reference (`docs/FALLBACK_PLANS.md`)

## 1. Overview
When the Gemini LLM API is unavailable, unconfigured (`GEMINI_API_KEY` unset), times out (`SHADOW_LLM_PLANNING_TIMEOUT_SEC` / `SHADOW_LLM_REPLANNING_TIMEOUT_SEC`), or is bypassed by an open circuit breaker (`SHADOW_LLM_BREAKER_COOLDOWN_SEC`), ULTRON falls back to `build_evidence_grounded_plan()` in `ultron/planner/engine.py` and `get_fallback_plan(mission_id, objective=None)` in `ultron/planner/cache.py`.

Every fallback plan **must**:
1. Include the exact substring `"USED CACHED PLAN"` in `plan.rationale` (enforced by unit tests and downstream audit traces).
2. Preserve `world.mission.objective` when non-empty (falling back to `"Inspect the building, find the target, and report hazards."` only when `objective` is `None` or blank).
3. Pass both `validate_plan_evidence(world, plan)` (`ultron/planner/grounding.py`) and `validate_plan(plan, world)` (`ultron/safety/validator.py`).

---

## 2. Allowed Task Types (`ultron/safety/constraints.py`)

Only `SCOUT01` (`VehicleID.SCOUT`) is permitted in single-vehicle deployment (`task.vehicle == VehicleID.SCOUT`). Assigning tasks to `HAWK01` is rejected by `validate_plan` with `Vehicle Compatibility: Vehicle 'HAWK01' is not present. Only SCOUT01 is active in this deployment.`

| Task Type (`SCOUT_ALLOWED_ACTIONS`) | Category | Movement / Active? | Notes |
| --- | --- | --- | --- |
| `EXPLORE` | Area reconnaissance | Active | Requires valid `area` or target grounding |
| `NAVIGATE` | Waypoint / zone traversal | Active | Requires valid `area` or target grounding |
| `SCAN` | 180°/360° sensor sweep | Active | Permitted with `area` or `target` |
| `INSPECT` | Close target inspection | Active | Permitted with `target` or `area` |
| `FOLLOW` | Target tracking | Active | Requires grounded `target` |
| `RETURN_HOME` | Return to base station | Active | Used in low-battery recovery branch (`> 20%` required for active tasks in `validate_plan`) |
| `PICK` | Object pickup | Active | Requires grounded `target` |
| `DROP` | Object release | Active | Requires grounded `target`/`area` |
| `FIND_TARGET` | Target search | Active | Legacy alias supported in `SCOUT_ALLOWED_ACTIONS` |
| `HOLD_POSITION` | Active station-keeping | Active | Supported in `SCOUT_ALLOWED_ACTIONS` |
| `STOP` | Immediate halt | Passive (`_PASSIVE_HOLD_ACTIONS`) | Permitted even when pose/connection/battery is degraded (unless `EMERGENCY_STOPPED`) |
| `REPORT` | Telemetry / status report | Passive (`_PASSIVE_HOLD_ACTIONS`) | Permitted even when pose/connection/battery is degraded (unless `EMERGENCY_STOPPED`) |

---

## 3. Accepted Areas & Targets (`ultron/safety/validator.py`)

`validate_plan` rejects any `task.area` or `task.target` that is not grounded in the standard vocabulary, the World Model, or the mission objective (`Hallucination: Area '...' does not exist` / `Hallucination: Target '...' has not been observed`).

### Always-Accepted Standard Areas (`standard_areas`)
- `sector_a` (or `sector_A`, case-insensitive match)
- `sector_b` (or `sector_B`)
- `zone_1`
- `zone_2`
- `corridor`
- `building`
- `arena`

### Dynamically Accepted Areas
- Any entry in `world.environment.explored_areas` (lowercased)
- Any whitespace-delimited token (with `,` and `.` stripped) appearing in `world.mission.objective.lower()`
- Any string containing one of the known area tokens as a substring (`any(w in task_area_clean for w in known_areas)`)

### Always-Accepted Standard Targets (`standard_targets`)
- `red_package`
- `target`
- `hazards`
- `hazard`
- `package`
- `thermal_anomaly`
- `obstacle`

### Dynamically Accepted Targets
- Any label in `world.environment.objects` (`obj.label.lower()`)
- Any label in `world.environment.tracked_objects.values()` (`t.label.lower()`)
- Any token appearing in `world.mission.objective.lower()`
- Any string containing one of the known target tokens as a substring (`any(w in task_target_clean for w in known_targets)`)

---

## 4. Deterministic Template Constraints (`validate_plan` & `validate_plan_evidence`)

When designing new deterministic standing-order templates in Phase 2, every template must satisfy all of the following invariants:

1. **Single-Vehicle Assignment**: Every `Task.vehicle` must be `VehicleID.SCOUT` (`"SCOUT01"`). Note that `configs/mission_demo.json` still lists a historical `HAWK01` task (`T001`), which is **not** used by `get_fallback_plan` because `validate_plan` enforces single-vehicle `SCOUT01` operation.
2. **No Duplicate Concurrent Task Types per Vehicle**: Within a single `MissionPlan.tasks` list, `(task.vehicle, task.type.strip().upper())` must be **strictly unique**. For example, a template cannot contain two `SCAN` tasks or two `NAVIGATE` tasks for `SCOUT01` in the same plan (`Contradiction: SCOUT01 is assigned duplicate concurrent task '...'`).
3. **Valid Priority Enum**: `Task.priority` must be an integer in `{1, 2, 3}` (`TaskPriority.HIGH = 1`, `TaskPriority.MEDIUM = 2`, `TaskPriority.LOW = 3`).
4. **Grounded Area and Target Strings**: Every non-`None` `task.area` and `task.target` must match `standard_areas` / `standard_targets` (Section 3) or tokens guaranteed to be present in `world.mission.objective` or `world.environment`.
5. **Verified Evidence IDs Only**: Every ID in `plan.evidence_ids` and `task.evidence_ids` must exist in `{o.observation_id for o in world.environment.observation_history} | set(world.environment.tracked_objects.keys())`. `validate_plan_evidence` strips unverified IDs, and `validate_plan` rejects any remaining unknown IDs.
6. **SCOUT01 Health Preconditions for Active Tasks**:
   - `SCOUT01` must exist in `world.vehicles`.
   - `scout.status` must not be `"EMERGENCY_STOPPED"` (blocks all tasks, including `STOP`/`REPORT`).
   - For active tasks (anything other than `STOP` and `REPORT`):
     - `scout.connection` must be `"ONLINE"` or `"CONNECTED"`.
     - `scout.status` must not be in `{"OFFLINE", "UNKNOWN", "DISCONNECTED", "FAULT"}`.
     - `scout.pose_valid` must be `True`.
     - `scout.battery` must be finite and `> 20.0`.
7. **Venue Geofence (`configs/geofence.json`)**:
   - Arena bounds: `0.0 <= x <= 20.0`, `0.0 <= y <= 15.0`, `alt <= 5.0`.

---

## 5. Existing Fallback Branches in `build_evidence_grounded_plan` (`ultron/planner/engine.py`)

`build_evidence_grounded_plan(world, trigger_rationale=None, repair_prompt=None)` evaluates four deterministic branches in priority order:

| Branch Priority | Trigger Condition | Generated Tasks (`SCOUT01`) | Confidence | Rationale Prefix |
| ---: | --- | --- | --- | --- |
| **1. Critical Battery** | `pose.battery is not None and pose.battery < 20.0` | `T-SAFE-01`: `RETURN_HOME` (`HIGH`)<br>`T-SAFE-02`: `REPORT` (`MEDIUM`) | `"high"` | `"USED CACHED PLAN — Critical battery (<pct>%) on SCOUT01. Aborting exploration and executing safe RETURN_HOME and REPORT."` |
| **2. Sensor Conflict** | `conflicted_objs` non-empty (`EpistemicStatusEnum.CONFLICTED`) | `T-CONF-01`: `STOP` (`HIGH`)<br>`T-CONF-02`: `SCAN`, `target="target"` (`MEDIUM`)<br>`T-CONF-03`: `REPORT` (`LOW`) | `"low"` | `"USED CACHED PLAN — Sensor conflict detected on <id> ('<label>'). Halting forward navigation to SCAN and REPORT discrepancy."` |
| **3. Blocked Route / Obstacle** | Active non-stale obstacles or `known_obstacles` present, and `trigger_rationale` contains `"obstacle"` or `"blocked"` | `T-OBS-01`: `STOP` (`HIGH`)<br>`T-OBS-02`: `SCAN`, `area="sector_A"` (`MEDIUM`)<br>`T-OBS-03`: `REPORT` (`LOW`) | `"medium"` if any sensor available else `"low"` | `"USED CACHED PLAN — <trigger_rationale> No validated alternative route geometry is currently available; holding position to SCAN and REPORT."` |
| **4. Baseline / Default** | No critical battery, conflict, or blocked-route trigger | `T-CACHE-01`: `NAVIGATE`, `area="zone_2"` (`HIGH`)<br>`T-CACHE-02`: `EXPLORE`, `area="sector_A"` (`MEDIUM`)<br>`T-CACHE-03`: `INSPECT`, `target="red_package"` (`LOW`) | `"medium"` if any sensor available else `"low"` | `"USED CACHED PLAN — Live planning call timed out. Executing rehearsed demo initialization."` (or `"USED CACHED PLAN — Replanned due to evidence trigger: <trigger_rationale>"`) |
