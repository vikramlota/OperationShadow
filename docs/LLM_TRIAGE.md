# LLM Event Triage (`ultron/events/triage.py`)

## 1. Overview & Purpose

When the in-process Event Router (`ultron/events/router.py`) classifies a validated `UltronEvent` and no deterministic Standing Order (`ultron/events/standing_orders.py`) matches its preconditions, the event is routed to the **LLM Event Triage Layer** (`ultron/events/triage.py`).

The LLM Event Triage Layer evaluates ambiguous or context-dependent events against the verified `WorldModel` and recommends an explicit, structured operational disposition without modifying the `MissionPlan` schema.

---

## 2. Triage Output Schema (`EventTriageResult`)

`EventTriageResult` is a strict Pydantic model completely separate from `MissionPlan`:

| Field | Type | Description |
|---|---|---|
| `event_id` | `str` | Must match the triaged `UltronEvent.event_id`. |
| `disposition` | `TriageDispositionEnum` | One of `IGNORE`, `LOG_ONLY`, `APPLY_STANDING_ORDER`, `REQUEST_REPLAN`, `ESCALATE_TO_OPERATOR`. |
| `recommended_standing_order_id` | `Optional[str]` | Populated only when `disposition == APPLY_STANDING_ORDER` and verified against `StandingOrderRegistry`. |
| `severity_assessment` | `TriageSeverityAssessmentEnum` | `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, or `INFO`. |
| `confidence` | `TriageConfidenceEnum` | `high`, `medium`, or `low`. |
| `rationale` | `str` | Concise, evidence-grounded explanation of the triage decision. |
| `facts_used` | `List[str]` | Direct (`SINGLE_SENSOR`) and multi-sensor (`CORROBORATED`) facts used. |
| `inferences` | `List[str]` | Operational conclusions inferred from the facts. |
| `unknowns` | `List[str]` | Explicit sensor gaps, stale observations, unconfirmed transforms, or missing LiDAR. |
| `evidence_ids` | `List[str]` | Verified observation and object IDs from `WorldModel` supporting the triage decision. |
| `requires_replan` | `bool` | `True` when `disposition == REQUEST_REPLAN`. |
| `requires_operator_attention` | `bool` | `True` when `disposition == ESCALATE_TO_OPERATOR` or high-severity fallback. |

---

## 3. Epistemic & Safety Prompt Rules

`build_triage_prompt(event, world, registry)` supplies only scoped context:
- The serialized `UltronEvent`
- Active `MissionState` (`id`, `objective`, `status`, `tasks`)
- Verified `EnvironmentSnapshotResult` from `WorldModelTools` (including sensor health, freshness, and transform validation status)
- Explicit `VALID_EVIDENCE_IDS` present in `WorldModel`
- Catalog of enabled Standing Orders

`TRIAGE_SYSTEM_PROMPT` instructs the model to:
1. Distinguish between `SINGLE_SENSOR` observations, `CORROBORATED` observations, `INFERRED` conclusions, `STALE` data, `CONFLICTED` measurements, and `UNKNOWN` sensor gaps.
2. Never treat camera-relative coordinates (`oak_rgb_camera_optical_frame`, `oak_parent_frame`) as global `map` coordinates when `transform_map_validated == False`.
3. Never invent observation IDs, object IDs, or coordinates.
4. Never output motor commands, PWM values, serial frames, or instructions for `HAWK` (`SCOUT01` is the only active ULTRON vehicle).

---

## 4. Deterministic Validation (`validate_triage_result`)

Every `EventTriageResult`—whether returned by Gemini or by the fallback builder—passes through `validate_triage_result(result, event, world, registry)` before any action is taken:

1. **Forbidden Directive Guard**: Any mention of raw motor/PWM/serial frames or dispatching `HAWK` is rejected and replaced with the deterministic fallback.
2. **Event ID Alignment**: Ensures `result.event_id == event.event_id`.
3. **Evidence Grounding Check**: Strips any `evidence_ids` that do not exist in `collect_valid_evidence_ids(world, include_stale=False)`. If stale IDs were cited, moves them to `unknowns` and downgrades `confidence` from `high` to `low`.
4. **Transform Honesty**: Ensures `unknowns` notes that camera coordinates are camera-relative whenever `world.environment.transform_map_validated` is `False`.
5. **Standing Order Verification**: If `disposition == APPLY_STANDING_ORDER`, verifies that `recommended_standing_order_id` exists in `StandingOrderRegistry`, is enabled, and satisfies all deterministic preconditions against `world`. If preconditions fail, downgrades to the deterministic fallback disposition.

---

## 5. Timeout, Circuit Breaker & Deterministic Fallback

- **Timeout Budget**: Controlled by `SHADOW_LLM_TRIAGE_TIMEOUT_SEC` (default `3.0` seconds), enforced via a non-blocking `ThreadPoolExecutor` timeout (`shutdown(wait=False, cancel_futures=True)`).
- **Shared Circuit Breaker**: Reuses `is_breaker_open()` and `_trip_breaker()` from `ultron/planner/engine.py`. If Gemini is already circuit-broken or `GEMINI_API_KEY` is unset, `invoke_event_triage()` returns immediately in `< 1 ms` without making a network request.
- **Deterministic Fallback (`build_deterministic_triage_fallback`)**:
  - `HAZARD_DETECTED`, `ROUTE_BLOCKED`, `VEHICLE_CRITICAL_BATTERY` -> `REQUEST_REPLAN` (if mission active) or `ESCALATE_TO_OPERATOR`.
  - `SENSOR_CONFLICT`, `SENSOR_DEGRADED`, `VEHICLE_CONNECTION_DEGRADED` -> `REQUEST_REPLAN` (for `CRITICAL`/`HIGH` during active mission) or `ESCALATE_TO_OPERATOR`.
  - `OBJECT_DETECTED` -> `REQUEST_REPLAN` if obstacle/close-range (`<= 2.5m`)/high-priority during an active mission; otherwise `LOG_ONLY`.
  - `MISSION_LIFECYCLE` -> `LOG_ONLY`.
