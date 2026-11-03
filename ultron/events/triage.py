"""LLM Event Triage Layer for Operation Shadow Phase 2 (§Workstream D).

Evaluates ambiguous or context-dependent system events using a bounded Gemini call
(or deterministic evidence-grounded fallback when Gemini is unavailable, disabled,
timed out, or circuit-broken) without changing the `MissionPlan` schema.

Strict Safety & Grounding Invariants:
- Separate structured schema (`EventTriageResult`) — never alters `MissionPlan`.
- Never outputs motor commands, PWM values, serial frames, or unvalidated vehicle IDs.
- Never invents observation IDs, object IDs, or coordinates not present in `WorldModel`.
- Never overrides a deterministic safety rule or Standing Order precondition.
- Never promotes `HAWK` to an active ULTRON vehicle (`SCOUT01` only).
- Enforces `SHADOW_LLM_TRIAGE_TIMEOUT_SEC` (default 3.0s) and shares the Phase 1
  Gemini circuit breaker in `ultron.planner.engine`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from enum import Enum
import logging
import os
import time
from typing import Any, List, Literal, Optional, Set, Tuple
from pydantic import BaseModel, Field

from ultron.config import config as ultron_config
from ultron.events.schemas import (
    EventPriorityEnum,
    EventTypeEnum,
    UltronEvent,
)
from ultron.events.standing_orders import (
    StandingOrderDefinition,
    StandingOrderRegistry,
)
from ultron.planner.engine import (
    BREAKER_TRIP_CLASSES,
    _get_breaker_cooldown_sec,
    _get_connect_timeout_sec,
    _read_positive_float_env,
    _redact_secret,
    _trip_breaker,
    classify_gemini_error,
    is_breaker_open,
)
from ultron.schemas.observations import EpistemicStatusEnum
from ultron.schemas.world import WorldModel
from ultron.world_model.tools import WorldModelTools

logger = logging.getLogger("operation_shadow.ultron.events.triage")


class TriageDispositionEnum(str, Enum):
    """Allowed triage dispositions for ambiguous events (§Workstream D)."""

    IGNORE = "IGNORE"
    LOG_ONLY = "LOG_ONLY"
    APPLY_STANDING_ORDER = "APPLY_STANDING_ORDER"
    REQUEST_REPLAN = "REQUEST_REPLAN"
    ESCALATE_TO_OPERATOR = "ESCALATE_TO_OPERATOR"


class TriageSeverityAssessmentEnum(str, Enum):
    """Assessed operational severity of the triaged event."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class TriageConfidenceEnum(str, Enum):
    """Epistemic confidence of the triage assessment."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EventTriageResult(BaseModel):
    """Structured output schema for LLM Event Triage (§Workstream D).

    Separate from `MissionPlan` so the planner contract remains untouched.
    """

    event_id: str = Field(..., min_length=1, max_length=64)
    disposition: TriageDispositionEnum
    recommended_standing_order_id: Optional[str] = Field(default=None, max_length=64)
    severity_assessment: TriageSeverityAssessmentEnum = Field(
        default=TriageSeverityAssessmentEnum.MEDIUM
    )
    confidence: TriageConfidenceEnum = Field(default=TriageConfidenceEnum.MEDIUM)
    rationale: str = Field(..., min_length=1, max_length=1000)
    facts_used: List[str] = Field(default_factory=list)
    inferences: List[str] = Field(default_factory=list)
    unknowns: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    requires_replan: bool = False
    requires_operator_attention: bool = False


@dataclass(frozen=True)
class TriageOutcome:
    """Metadata describing how an `EventTriageResult` was produced."""

    source: Literal["llm", "fallback"]
    reason: str
    elapsed_ms: float
    validation_repairs: Tuple[str, ...] = ()


TRIAGE_SYSTEM_PROMPT = """You are the ULTRON Event Triage Intelligence Layer for Operation Shadow.
Your role is to evaluate a single ambiguous or context-dependent operational event against the verified WorldModel and recommend one structured disposition.

STRICT OPERATIONAL & EPISTEMIC RULES:
1. OUTPUT SCHEMA: Return a single JSON object conforming strictly to `EventTriageResult`.
2. ALLOWED DISPOSITIONS:
   - `IGNORE`: Event is redundant, already resolved, or has zero operational relevance.
   - `LOG_ONLY`: Event is noteworthy for mission telemetry/audit logs but does not alter current tasks.
   - `APPLY_STANDING_ORDER`: Event matches an existing Standing Order in the provided Standing Order catalog AND its preconditions are satisfied. Set `recommended_standing_order_id` to that exact ID.
   - `REQUEST_REPLAN`: Event materially affects mission execution, route safety, or target inspection and requires the ULTRON Mission Planner to replan. Set `requires_replan=true`.
   - `ESCALATE_TO_OPERATOR`: Event represents an unresolved sensor conflict, persistent degraded state, or ambiguous finding that requires human operator review. Set `requires_operator_attention=true`.
3. EVIDENCE GROUNDING:
   - Distinguish strictly between:
     * Direct observations (`OBSERVED` / `SINGLE_SENSOR` facts)
     * Corroborated multi-sensor observations (`CORROBORATED` facts)
     * Inferences (`INFERRED` conclusions)
     * Stale observations (`STALE` — must be listed in `unknowns`, never treated as current facts)
     * Conflicted observations (`CONFLICTED` — must never be treated as clear path)
     * Unknowns / unverified transforms (`transform_map_validated=false` means camera coordinates are camera-relative, NEVER global map coordinates)
   - `evidence_ids` MUST ONLY contain IDs explicitly present in the provided `VALID_EVIDENCE_IDS` list. Never invent observation or object IDs.
4. SAFETY & VEHICLE CONSTRAINTS:
   - Never output motor commands, PWM values, serial frames, or raw movement instructions.
   - `SCOUT01` is the ONLY active ULTRON vehicle. Never recommend actions for `HAWK` (simulated/roadmap only).
   - Never recommend overriding a deterministic safety rule or Standing Order precondition.
"""

_default_so_registry: Optional[StandingOrderRegistry] = None


def _get_default_standing_order_registry() -> StandingOrderRegistry:
    global _default_so_registry
    if _default_so_registry is None:
        _default_so_registry = StandingOrderRegistry.from_json_file()
    return _default_so_registry


def _collect_valid_world_evidence_ids(
    world: WorldModel,
    *,
    include_stale: bool = False,
    current_time: Optional[float] = None,
) -> Set[str]:
    """Collect valid observation IDs and tracked object IDs from `WorldModel`."""
    now = current_time if current_time is not None else time.time()
    stale_thresh = ultron_config.camera_stale_after_sec
    valid_ids: Set[str] = set()

    for obs in world.environment.observation_history:
        is_stale = (now - obs.sensor_timestamp) > stale_thresh
        if include_stale or not is_stale:
            valid_ids.add(obs.observation_id)

    for obj_id, tobj in world.environment.tracked_objects.items():
        is_stale = (
            tobj.epistemic_status == EpistemicStatusEnum.STALE
            or (now - tobj.last_observed_timestamp) > stale_thresh
        )
        if include_stale or not is_stale:
            valid_ids.add(obj_id)
            for oid in tobj.contributing_observation_ids:
                valid_ids.add(oid)

    for haz in world.environment.hazards:
        if haz.observation_id:
            is_stale = haz.epistemic_status == EpistemicStatusEnum.STALE
            if include_stale or not is_stale:
                valid_ids.add(haz.observation_id)

    for leg_obj in world.environment.objects:
        if leg_obj.observation_id:
            is_stale = leg_obj.epistemic_status == EpistemicStatusEnum.STALE
            if include_stale or not is_stale:
                valid_ids.add(leg_obj.observation_id)

    return valid_ids


def _get_triage_timeout_sec() -> float:
    return _read_positive_float_env("SHADOW_LLM_TRIAGE_TIMEOUT_SEC", 3.0)


def _call_gemini_triage_sdk(
    *,
    api_key: str,
    model_name: str,
    user_prompt: str,
    timeout_sec: float,
    connect_timeout_sec: float,
) -> Any:
    """Execute the synchronous Google GenAI SDK `generate_content` call for `EventTriageResult`.

    Isolated so unit tests can mock or patch it deterministically.
    """
    from google import genai
    from google.genai import types as genai_types
    import httpx

    eff_timeout = max(0.1, float(timeout_sec))
    eff_connect = max(0.1, min(float(connect_timeout_sec), eff_timeout))
    httpx_timeout = httpx.Timeout(eff_timeout, connect=eff_connect)

    client = genai.Client(
        api_key=api_key,
        http_options={
            "timeout": max(1, int(eff_timeout * 1000)),
            "client_args": {"timeout": httpx_timeout},
        },
    )
    return client.models.generate_content(
        model=model_name,
        contents=user_prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=TRIAGE_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=EventTriageResult,
            temperature=0.1,
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        ),
    )


def build_triage_prompt(
    event: UltronEvent,
    world: WorldModel,
    registry: Optional[StandingOrderRegistry] = None,
) -> str:
    """Build a scoped, evidence-grounded prompt for `invoke_event_triage`."""
    tools = WorldModelTools(world, current_time=event.received_at)
    snapshot = tools.get_environment_snapshot()
    valid_ids = sorted(
        _collect_valid_world_evidence_ids(
            world, include_stale=False, current_time=event.received_at
        )
    )
    so_registry = registry or _get_default_standing_order_registry()
    available_orders: List[dict[str, Any]] = [
        {
            "id": o.id,
            "name": o.name,
            "supported_event_types": [et.value for et in o.supported_event_types],
            "priority": o.priority,
            "description": o.description,
        }
        for o in so_registry.orders
    ]

    return (
        f"EVENT TO TRIAGE:\n{event.model_dump_json(indent=2)}\n\n"
        f"ACTIVE MISSION STATE:\n{world.mission.model_dump_json(indent=2)}\n\n"
        f"VERIFIED ENVIRONMENT SNAPSHOT:\n{snapshot.model_dump_json(indent=2)}\n\n"
        f"VALID_EVIDENCE_IDS (ONLY cite IDs from this list):\n{valid_ids}\n\n"
        f"AVAILABLE STANDING ORDERS:\n{available_orders}\n"
    )


def build_deterministic_triage_fallback(
    event: UltronEvent,
    world: WorldModel,
    *,
    fallback_reason: str = "deterministic_fallback",
) -> EventTriageResult:
    """Construct a deterministic, conservative `EventTriageResult` when Gemini is unavailable or invalid."""
    tools = WorldModelTools(world, current_time=event.received_at)
    snapshot = tools.get_environment_snapshot()
    valid_ids = _collect_valid_world_evidence_ids(
        world, include_stale=False, current_time=event.received_at
    )

    verified_evidence_ids = [
        eid
        for eid in list(event.observation_ids) + list(event.tracked_object_ids)
        if eid in valid_ids
    ]

    prio_val = event.priority.value if event.priority is not None else "MEDIUM"
    facts: List[str] = [
        f"Event {event.event_id} ({event.event_type.value}, priority={prio_val}): {event.summary}"
    ]
    inferences: List[str] = []
    unknowns: List[str] = []

    if not snapshot.sensor_health.lidar_available:
        unknowns.append(
            "LiDAR is UNAVAILABLE; single-camera observations cannot be geometrically corroborated across 360 degrees."
        )
    if not snapshot.transform_map_validated:
        unknowns.append(
            "Global map transform (map -> odom -> oak_parent_frame) is unconfirmed; camera coordinates remain frame-relative."
        )

    mission_status_str = getattr(
        world.mission.status, "value", str(world.mission.status)
    ).upper()
    mission_active = bool(world.mission.id) and mission_status_str in (
        "IN_PROGRESS",
        "ACTIVE",
        "PLANNING",
    )

    # Classify disposition deterministically by event type & priority
    if event.event_type in (
        EventTypeEnum.HAZARD_DETECTED,
        EventTypeEnum.VEHICLE_LOW_BATTERY,
        EventTypeEnum.VEHICLE_FAULT,
    ):
        disposition = (
            TriageDispositionEnum.REQUEST_REPLAN
            if mission_active
            else TriageDispositionEnum.ESCALATE_TO_OPERATOR
        )
        severity = (
            TriageSeverityAssessmentEnum.CRITICAL
            if event.priority == EventPriorityEnum.CRITICAL
            else TriageSeverityAssessmentEnum.HIGH
        )
        requires_replan = mission_active
        requires_operator = True
        inferences.append(
            f"Safety-relevant event ({event.event_type.value}) requires immediate mission adaptation or operator review."
        )
    elif event.event_type in (
        EventTypeEnum.SENSOR_CONFLICT,
        EventTypeEnum.SENSOR_DEGRADED,
        EventTypeEnum.VEHICLE_DISCONNECTED,
    ):
        disposition = (
            TriageDispositionEnum.REQUEST_REPLAN
            if mission_active
            and event.priority in (EventPriorityEnum.CRITICAL, EventPriorityEnum.HIGH)
            else TriageDispositionEnum.ESCALATE_TO_OPERATOR
        )
        severity = (
            TriageSeverityAssessmentEnum.HIGH
            if event.priority in (EventPriorityEnum.CRITICAL, EventPriorityEnum.HIGH)
            else TriageSeverityAssessmentEnum.MEDIUM
        )
        requires_replan = disposition == TriageDispositionEnum.REQUEST_REPLAN
        requires_operator = True
        inferences.append(
            f"Degraded or conflicting telemetry ({event.event_type.value}) lowers situational certainty and requires conservative handling."
        )
    elif event.event_type == EventTypeEnum.OBSTACLE_DETECTED:
        blocks_route = bool(event.evidence_payload.get("blocks_route", False))
        range_m = event.evidence_payload.get("range_m")
        close_range = isinstance(range_m, (int, float)) and float(range_m) <= 2.5
        if mission_active and (
            blocks_route
            or close_range
            or event.priority in (EventPriorityEnum.CRITICAL, EventPriorityEnum.HIGH)
        ):
            disposition = TriageDispositionEnum.REQUEST_REPLAN
            severity = TriageSeverityAssessmentEnum.HIGH
            requires_replan = True
            requires_operator = False
            inferences.append(
                "Detected obstacle may obstruct active mission route; requesting evidence-grounded replan."
            )
        else:
            disposition = TriageDispositionEnum.LOG_ONLY
            severity = TriageSeverityAssessmentEnum.LOW
            requires_replan = False
            requires_operator = False
            inferences.append(
                "Peripheral obstacle recorded for mission audit without interrupting current task execution."
            )
    elif event.event_type == EventTypeEnum.TARGET_DETECTED:
        matches_obj = bool(
            event.evidence_payload.get("matches_mission_objective", False)
        )
        if mission_active and (
            matches_obj
            or event.priority in (EventPriorityEnum.CRITICAL, EventPriorityEnum.HIGH, EventPriorityEnum.MEDIUM)
        ):
            disposition = TriageDispositionEnum.REQUEST_REPLAN
            severity = TriageSeverityAssessmentEnum.MEDIUM
            requires_replan = True
            requires_operator = False
            inferences.append(
                "Detected target is relevant to active mission objective; requesting evidence-grounded replan."
            )
        else:
            disposition = TriageDispositionEnum.LOG_ONLY
            severity = TriageSeverityAssessmentEnum.LOW
            requires_replan = False
            requires_operator = False
            inferences.append(
                "Non-target object detection recorded in mission audit log."
            )
    elif event.event_type == EventTypeEnum.OBSERVATION_STALE:
        disposition = TriageDispositionEnum.LOG_ONLY
        severity = TriageSeverityAssessmentEnum.LOW
        requires_replan = False
        requires_operator = False
        unknowns.append(
            f"Observation in event {event.event_id} is STALE and cannot be treated as current geometry."
        )
        inferences.append("Stale observation transition logged without triggering speculative replan.")
    elif event.event_type == EventTypeEnum.MISSION_STATE_CHANGED:
        disposition = TriageDispositionEnum.LOG_ONLY
        severity = TriageSeverityAssessmentEnum.INFO
        requires_replan = False
        requires_operator = False
        inferences.append("Mission lifecycle transition recorded in audit log.")
    else:
        disposition = TriageDispositionEnum.LOG_ONLY
        severity = TriageSeverityAssessmentEnum.LOW
        requires_replan = False
        requires_operator = False
        inferences.append(
            "Event logged deterministically under conservative fallback policy."
        )

    confidence = (
        TriageConfidenceEnum.MEDIUM
        if verified_evidence_ids and snapshot.sensor_health.any_sensor_available
        else TriageConfidenceEnum.LOW
    )

    return EventTriageResult(
        event_id=event.event_id,
        disposition=disposition,
        recommended_standing_order_id=None,
        severity_assessment=severity,
        confidence=confidence,
        rationale=(
            f"DETERMINISTIC TRIAGE FALLBACK ({fallback_reason}) — "
            f"Evaluated {event.event_type.value} (priority={prio_val}) -> {disposition.value}."
        ),
        facts_used=facts,
        inferences=inferences,
        unknowns=unknowns,
        evidence_ids=verified_evidence_ids,
        requires_replan=requires_replan,
        requires_operator_attention=requires_operator,
    )


def validate_triage_result(
    result: EventTriageResult,
    event: UltronEvent,
    world: WorldModel,
    registry: Optional[StandingOrderRegistry] = None,
) -> Tuple[EventTriageResult, List[str]]:
    """Deterministically validate and sanitize an `EventTriageResult` against `WorldModel` and `StandingOrderRegistry`.

    Enforces:
    1. `event_id` matches the triaged `event.event_id`.
    2. `evidence_ids` only reference non-stale observation/object IDs present in `WorldModel`.
    3. `HAWK` or raw motor/PWM/serial directives in rationale/inferences are rejected.
    4. If `disposition == APPLY_STANDING_ORDER`, `recommended_standing_order_id` must exist,
       match `event.event_type`, and satisfy preconditions against `world`.
    5. Boolean flags (`requires_replan`, `requires_operator_attention`) match `disposition`.
    """
    repairs: List[str] = []
    so_registry = registry or _get_default_standing_order_registry()

    # Check for forbidden raw actuation or HAWK promotion in LLM output
    combined_text = " ".join(
        [result.rationale] + list(result.facts_used) + list(result.inferences)
    ).lower()
    forbidden_tokens = (
        "pwm",
        "serial_frame",
        "motor_cmd",
        "assign hawk",
        "dispatch hawk",
    )
    if any(tok in combined_text for tok in forbidden_tokens):
        repairs.append(
            "Rejected triage output containing forbidden actuation or HAWK dispatch directive; falling back deterministically."
        )
        fallback = build_deterministic_triage_fallback(
            event, world, fallback_reason="forbidden_directive_rejected"
        )
        return fallback, repairs

    if result.event_id != event.event_id:
        repairs.append(
            f"Corrected mismatched event_id '{result.event_id}' -> '{event.event_id}'."
        )
        result.event_id = event.event_id

    valid_ids = _collect_valid_world_evidence_ids(
        world, include_stale=False, current_time=event.received_at
    )
    all_ids_with_stale = _collect_valid_world_evidence_ids(
        world, include_stale=True, current_time=event.received_at
    )
    stale_only_ids = all_ids_with_stale - valid_ids

    cleaned_evidence: List[str] = []
    for eid in result.evidence_ids:
        if eid in valid_ids:
            if eid not in cleaned_evidence:
                cleaned_evidence.append(eid)
        elif eid in stale_only_ids:
            repairs.append(f"Removed stale evidence_id '{eid}' from triage result.")
            stale_note = (
                f"Evidence '{eid}' is STALE and cannot support active triage claims."
            )
            if stale_note not in result.unknowns:
                result.unknowns.append(stale_note)
        else:
            repairs.append(
                f"Removed unverified/invented evidence_id '{eid}' from triage result."
            )

    if len(cleaned_evidence) != len(result.evidence_ids):
        result.evidence_ids = cleaned_evidence
        if result.confidence == TriageConfidenceEnum.HIGH:
            result.confidence = TriageConfidenceEnum.LOW

    # Ensure camera-frame warning is preserved when transform is unconfirmed
    if not world.environment.transform_map_validated:
        tf_note = (
            "Global map transform (map -> odom -> oak_parent_frame) is unconfirmed; "
            "camera coordinates are camera-relative."
        )
        if not any(
            "transform" in u.lower() or "camera-relative" in u.lower()
            for u in result.unknowns
        ):
            result.unknowns.append(tf_note)

    # Validate Standing Order recommendation if present
    if result.disposition == TriageDispositionEnum.APPLY_STANDING_ORDER:
        order_id = (result.recommended_standing_order_id or "").strip()
        order: Optional[StandingOrderDefinition] = (
            so_registry.get_order(order_id) if order_id else None
        )
        if order is None or event.event_type not in order.supported_event_types:
            repairs.append(
                f"Triage recommended unknown or incompatible Standing Order '{order_id}'; downgrading to deterministic disposition."
            )
            fallback = build_deterministic_triage_fallback(
                event, world, fallback_reason="invalid_standing_order_id"
            )
            return fallback, repairs

        # Verify preconditions and required evidence without mutating cooldown history yet
        pre_ok, pre_reason = so_registry._check_preconditions(order, event, world)
        ev_ok, _, ev_reason = so_registry._verify_required_world_evidence(
            order, event, world
        )
        if not pre_ok or not ev_ok:
            fail_detail = pre_reason if not pre_ok else ev_reason
            repairs.append(
                f"Standing Order '{order.id}' preconditions/evidence not satisfied ({fail_detail}); "
                "downgrading to deterministic disposition."
            )
            fallback = build_deterministic_triage_fallback(
                event, world, fallback_reason="standing_order_precondition_failed"
            )
            return fallback, repairs
    else:
        result.recommended_standing_order_id = None

    # Synchronize boolean action flags with disposition
    if result.disposition == TriageDispositionEnum.REQUEST_REPLAN:
        result.requires_replan = True
    elif result.disposition in (
        TriageDispositionEnum.IGNORE,
        TriageDispositionEnum.LOG_ONLY,
        TriageDispositionEnum.APPLY_STANDING_ORDER,
    ):
        result.requires_replan = False

    if result.disposition == TriageDispositionEnum.ESCALATE_TO_OPERATOR:
        result.requires_operator_attention = True

    return result, repairs


def invoke_event_triage(
    event: UltronEvent,
    world: WorldModel,
    *,
    registry: Optional[StandingOrderRegistry] = None,
) -> Tuple[EventTriageResult, TriageOutcome]:
    """Evaluate an ambiguous `UltronEvent` using bounded Gemini triage with deterministic fallback.

    Reuses the Phase 1 Gemini circuit breaker (`is_breaker_open`, `_trip_breaker`) and
    enforces `SHADOW_LLM_TRIAGE_TIMEOUT_SEC` (default 3.0s).
    """
    t_start = time.monotonic()
    so_registry = registry or _get_default_standing_order_registry()

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        fallback = build_deterministic_triage_fallback(
            event, world, fallback_reason="api_key_unset"
        )
        validated, repairs = validate_triage_result(
            fallback, event, world, registry=so_registry
        )
        outcome = TriageOutcome(
            source="fallback",
            reason="api_key_unset",
            elapsed_ms=round((time.monotonic() - t_start) * 1000.0, 2),
            validation_repairs=tuple(repairs),
        )
        return validated, outcome

    if is_breaker_open():
        logger.warning(
            "Gemini circuit breaker is OPEN; skipping LLM triage for event %s and using deterministic fallback.",
            event.event_id,
        )
        fallback = build_deterministic_triage_fallback(
            event, world, fallback_reason="circuit_breaker_open"
        )
        validated, repairs = validate_triage_result(
            fallback, event, world, registry=so_registry
        )
        outcome = TriageOutcome(
            source="fallback",
            reason="circuit_breaker_open",
            elapsed_ms=round((time.monotonic() - t_start) * 1000.0, 2),
            validation_repairs=tuple(repairs),
        )
        return validated, outcome

    model_name = (
        os.getenv("SHADOW_LLM_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    )
    budget_sec = _get_triage_timeout_sec()
    connect_timeout_sec = min(_get_connect_timeout_sec(), budget_sec)
    cooldown_sec = _get_breaker_cooldown_sec()
    user_prompt = build_triage_prompt(event, world, registry=so_registry)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ultron-triage")
    future = executor.submit(
        _call_gemini_triage_sdk,
        api_key=api_key,
        model_name=model_name,
        user_prompt=user_prompt,
        timeout_sec=budget_sec,
        connect_timeout_sec=connect_timeout_sec,
    )

    last_error_class = "other"
    try:
        response = future.result(timeout=budget_sec)
        executor.shutdown(wait=False, cancel_futures=True)

        parsed_obj = getattr(response, "parsed", None)
        if parsed_obj is not None:
            raw_result = EventTriageResult.model_validate(parsed_obj)
            validated, repairs = validate_triage_result(
                raw_result, event, world, registry=so_registry
            )
            used_fallback = validated.rationale.startswith(
                "DETERMINISTIC TRIAGE FALLBACK"
            )
            outcome = TriageOutcome(
                source="fallback" if used_fallback else "llm",
                reason="validation_fallback" if used_fallback else "ok",
                elapsed_ms=round((time.monotonic() - t_start) * 1000.0, 2),
                validation_repairs=tuple(repairs),
            )
            return validated, outcome

        last_error_class = "empty_response"
        logger.warning(
            "Gemini event triage returned empty/unparsed response for event %s (model=%s).",
            event.event_id,
            model_name,
        )
    except FuturesTimeoutError as timeout_exc:
        executor.shutdown(wait=False, cancel_futures=True)
        last_error_class = classify_gemini_error(timeout_exc)
        logger.warning(
            "Gemini event triage timed out after %.2fs budget for event %s (class=%s).",
            budget_sec,
            event.event_id,
            last_error_class,
        )
        _trip_breaker(last_error_class, cooldown_sec)
    except Exception as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        last_error_class = classify_gemini_error(exc)
        safe_err = _redact_secret(str(exc), api_key)
        logger.warning(
            "Gemini event triage failed for event %s (class=%s): %s",
            event.event_id,
            last_error_class,
            safe_err,
        )
        if last_error_class in BREAKER_TRIP_CLASSES:
            _trip_breaker(last_error_class, cooldown_sec)

    fallback = build_deterministic_triage_fallback(
        event, world, fallback_reason=last_error_class
    )
    validated, repairs = validate_triage_result(
        fallback, event, world, registry=so_registry
    )
    outcome = TriageOutcome(
        source="fallback",
        reason=last_error_class,
        elapsed_ms=round((time.monotonic() - t_start) * 1000.0, 2),
        validation_repairs=tuple(repairs),
    )
    return validated, outcome
