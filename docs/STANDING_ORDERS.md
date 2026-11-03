# ULTRON Standing Orders (`docs/STANDING_ORDERS.md`)

## 1. Overview & Design Principles
**Standing Orders** (`configs/standing_orders.json` and `ultron/events/standing_orders.py`) are declarative, deterministic operational policies that ULTRON executes immediately when known conditions occur, without invoking the Gemini LLM.

Core invariants:
1. **Evaluated Before LLM Triage**: Whenever a normalized `UltronEvent` matches a registered Standing Order's `supported_event_types`, `preconditions`, and `required_evidence`, the Standing Order executes deterministically and skips LLM triage.
2. **Never Bypasses Safety or Grounding**: Every plan produced by a Standing Order is constructed via `build_evidence_grounded_plan()` (`ultron/planner/engine.py`), sanitized via `validate_plan_evidence()` (`ultron/planner/grounding.py`), and verified through `validate_plan()` (`ultron/safety/validator.py`).
3. **Preserves Mission Objective & Rationale Substring**: Every generated `MissionPlan` retains `world.mission.objective`, carries verified `evidence_ids`, and includes the exact substring `"USED CACHED PLAN"` in `rationale`.
4. **Fail-Fast Configuration Validation**: `StandingOrderRegistry.from_json_file()` validates `configs/standing_orders.json` at load time (`StandingOrderConfigError`), rejecting unknown task types, non-`SCOUT01` vehicles, duplicate concurrent actions, ungrounded areas/targets, or duplicate order IDs.

---

## 2. Registered Standing Orders (`configs/standing_orders.json`)

| ID | Name | Supported Event Types | Priority | Preconditions | Required World Model Evidence | Deterministic Response Template (`SCOUT01`) | Cooldown | Operator Intervention? |
| --- | --- | --- | ---: | --- | --- | --- | ---: | --- |
| `SO-01-CRITICAL-BATTERY` | Critical Battery Safe Hold & Report | `VEHICLE_LOW_BATTERY` | `100` | Active mission; `SCOUT01` not `EMERGENCY_STOPPED`; `battery < 20.0%` | `ScoutState.battery < 20.0%` in `world.vehicles["SCOUT01"]` | `T-SAFE-01`: `STOP` (`P1`)<br>`T-SAFE-02`: `REPORT` (`P2`)<br>*(Uses passive hold actions so `validate_plan` passes under the `<= 20%` battery movement lock without inventing an unverified home route)* | `15.0s` | **Yes** |
| `SO-02-SENSOR-CONFLICT` | Conflicting Sensor Evidence Hold & Re-Scan | `SENSOR_CONFLICT` | `90` | Active mission; `SCOUT01` `ONLINE`/`CONNECTED`; `pose_valid=True`; `battery > 20.0%` | Unhandled `TrackedObject` with `epistemic_status == CONFLICTED` and valid `observation_ids` | `T-CONF-01`: `STOP` (`P1`)<br>`T-CONF-02`: `SCAN`, `target="target"` (`P2`)<br>`T-CONF-03`: `REPORT` (`P3`) | `8.0s` | No |
| `SO-03-BLOCKED-ROUTE` | Blocked Route Conservative Stop & Scan | `OBSTACLE_DETECTED` | `80` | Active mission; `SCOUT01` `ONLINE`/`CONNECTED`; `pose_valid=True`; `battery > 20.0%`; `blocks_route == True` (`range_m <= 2.5m` or `known_obstacles`) | Active non-stale obstacle (`TrackedObject.is_obstacle` with valid `observation_ids`, or `known_obstacles`) | `T-OBS-01`: `STOP` (`P1`)<br>`T-OBS-02`: `SCAN`, `area="sector_A"` (`P2`)<br>`T-OBS-03`: `REPORT` (`P3`)<br>*(Never invents a speculative detour when 360° LiDAR free-space geometry is unverified)* | `8.0s` | No |
| `SO-04-HAZARD-HOLD` | Verified Hazard Immediate Stop, Scan & Report | `HAZARD_DETECTED` | `85` | Active mission; `SCOUT01` `ONLINE`/`CONNECTED`; `pose_valid=True`; `battery > 20.0%`; `confidence >= 0.70` | Unhandled `HazardEntity` or hazard `TrackedObject` with valid `observation_ids` in `WorldModel` | `T-HAZ-01`: `STOP` (`P1`)<br>`T-HAZ-02`: `SCAN`, `area="sector_A"` (`P2`)<br>`T-HAZ-03`: `REPORT` (`P3`) | `10.0s` | **Yes** |

---

## 3. Precedence & Duplicate-Execution Suppression

1. **Deterministic Precedence**:
   - Candidate Standing Orders matching `event.event_type` are sorted by `-(order.priority + priority_rank(event.priority) * 10)` and tie-broken lexicographically by `order.id`.
   - `SO-01-CRITICAL-BATTERY` (`priority=100`) takes highest precedence, followed by `SO-02-SENSOR-CONFLICT` (`90`), `SO-04-HAZARD-HOLD` (`85`), and `SO-03-BLOCKED-ROUTE` (`80`).
2. **Condition Cooldown & Handled Tracking**:
   - Each executed Standing Order records a composite condition key `(order.id, mission_id, tracked_object_ids_or_signature)` in a bounded LRU map (`max_handled_signatures = 256`).
   - Subsequent events matching the same condition within `order.cooldown_sec` return `status="suppressed_already_handled"` (`executed=False`) so repeated detections do not recreate identical tasks or restart the response sequence.
3. **Ambiguous Events Fall Through to LLM Triage**:
   - Events that do not match a Standing Order—such as a lower-confidence hazard (`confidence < 0.70`), a non-blocking obstacle (`blocks_route=False`), `TARGET_DETECTED` (which never auto-completes a mission), `OBSERVATION_STALE`, `SENSOR_DEGRADED`, or `VEHICLE_DISCONNECTED`—return `matched=False` from `StandingOrderRegistry` and proceed to **LLM Event Triage**.

---

## 4. Known Limitations
- **Passive Hold on Critical Battery (`<= 20.0%`)**: Because `ultron/safety/validator.py` strictly blocks all movement task types (including `RETURN_HOME`) when `SCOUT01.battery <= 20.0%` and no calibrated home return path is confirmed, `SO-01-CRITICAL-BATTERY` issues `STOP` + `REPORT` and flags `operator_intervention_required=True`.
- **Single-Vehicle Scope**: All Standing Orders target `SCOUT01` (`HAWK01` remains simulated/roadmap, not integrated).
